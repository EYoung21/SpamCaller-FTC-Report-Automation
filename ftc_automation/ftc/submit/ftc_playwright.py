"""Playwright-based submitter for the donotcall.gov complaint form.

The form selectors used here come straight from the donotcall.gov HTML
that the user supplied:

Step 1
  #PhoneTextBox                  — your phone number that received the call
  #DateOfCallTextBox             — MM/DD/YYYY
  #TimeOfCallDropDownList        — HH (00..23)
  #ddlMinutes                    — MM (00..59)
  #PrerecordMessageYESRadioButton
  #PhoneCallRadioButton
  #ddlSubjectMatter              — 0..16
  #txtSubjectMatter              — only shown for value=1 ("Other")
  #StepOneContinueButton

Step 2
  #CallerPhoneNumberTextBox      — the spammer's number
  #CallerNameTextBox             — company / person who called
  #HaveBusinessNoRadioButton     — "no business relationship"
  #StopCallingNoRadioButton      — "no, I didn't ask them to stop"
  #FirstNameTextBox, #LastNameTextBox
  #StreetAddressTextBox, #CityTextBox, #StateDropDownList, #ZipCodeTextBox
  #CommentTextBox                — narrative
  #StepTwoSubmitButton

Success page
  #StepTwoAcceptedPanel
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from sqlalchemy import select

from ..config import AppConfig, load_config
from ..db import (
    STATUS_APPROVED,
    STATUS_SUBMITTED,
    STATUS_SUBMIT_FAILED,
    Voicemail,
    init_db,
    session_scope,
)
from .base import FtcSubmitter, SubmissionResult

# Re-use the Chrome discovery helper from the GV scraper.
from ..ingest.gv_playwright import _resolve_chrome_executable


log = logging.getLogger(__name__)


def _parse_proxy_url(url: str) -> dict:
    """Turn ``http://user:pass@host:port`` into Playwright's proxy dict."""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"Invalid proxy URL: {url!r}")
    port = parsed.port
    if port is None:
        port = 1080 if parsed.scheme.startswith("socks") else 8080
    server = f"{parsed.scheme}://{parsed.hostname}:{port}"
    out: dict = {"server": server}
    if parsed.username is not None:
        out["username"] = parsed.username
        out["password"] = parsed.password or ""
    return out


class ProxyRotator:
    """Round-robin proxy picker with a small persisted index."""

    def __init__(
        self,
        proxies: list[str],
        *,
        mode: str,
        state_path: Path,
    ) -> None:
        self.proxies = proxies
        self.mode = mode
        self.state_path = state_path
        self._index = self._load_index()

    def _load_index(self) -> int:
        if not self.proxies:
            return 0
        try:
            raw = self.state_path.read_text(encoding="utf-8").strip()
            return int(raw) % len(self.proxies)
        except (OSError, ValueError):
            return 0

    def _save_index(self) -> None:
        if not self.proxies:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(str(self._index), encoding="utf-8")

    def current_playwright_proxy(self) -> Optional[dict]:
        if not self.proxies:
            return None
        url = self.proxies[self._index % len(self.proxies)]
        return _parse_proxy_url(url)

    def current_label(self) -> str:
        if not self.proxies:
            return "direct"
        url = self.proxies[self._index % len(self.proxies)]
        parsed = urlparse(url)
        host = parsed.hostname or url
        return f"{parsed.scheme}://{host}"

    def advance(self) -> Optional[dict]:
        if not self.proxies:
            return None
        self._index = (self._index + 1) % len(self.proxies)
        self._save_index()
        log.info(
            "Rotated to proxy %d/%d (%s).",
            self._index + 1,
            len(self.proxies),
            self.current_label(),
        )
        return self.current_playwright_proxy()

    def advance_for_run_start(self) -> Optional[dict]:
        if self.mode == "each_run" and len(self.proxies) > 1:
            return self.advance()
        return self.current_playwright_proxy()

    def advance_on_throttle(self) -> Optional[dict]:
        if self.mode == "on_throttle" and len(self.proxies) > 1:
            return self.advance()
        return self.current_playwright_proxy()

    def advance_after_submit(self) -> Optional[dict]:
        if self.mode == "each_submit" and len(self.proxies) > 1:
            return self.advance()
        return self.current_playwright_proxy()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _digits(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def _format_time_dropdown(hour: int) -> str:
    return f"{hour:02d}"


def _human_pause(page, min_ms: int = 120, max_ms: int = 420) -> None:
    page.wait_for_timeout(random.randint(min_ms, max_ms))


def _human_fill(page, selector: str, value: str) -> None:
    """Type like a human instead of instant ``fill()`` (bot-detection evasion)."""
    loc = page.locator(selector)
    loc.click(timeout=5000)
    _human_pause(page, 80, 200)
    loc.fill("")
    # Long comments through slow free proxies exceed Playwright's 30s type timeout.
    if len(value) > 120:
        loc.fill(value, timeout=15000)
    else:
        loc.type(value, delay=random.randint(35, 95), timeout=45000)
    _human_pause(page, 100, 250)


def _human_select(page, selector: str, value: str) -> None:
    page.locator(selector).click(timeout=5000)
    _human_pause(page, 80, 180)
    page.select_option(selector, value)
    _human_pause(page, 100, 220)


def _human_check(page, selector: str) -> None:
    page.locator(selector).click(timeout=5000, force=True)
    _human_pause(page, 120, 280)


def _classify_post_submit(page) -> tuple[bool, bool, str]:
    """Return ``(success, throttled, detail)`` from the current page."""
    url = page.url
    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        body_text = ""

    if page.locator("#StepTwoAcceptedPanel").count() > 0:
        return True, False, "accepted panel visible"

    # Mobile / alternate success layout — shows "Submit Another Complaint".
    if page.locator("#SubmitOtherComplaint_B").count() > 0:
        return True, False, "submit-another-complaint button visible"

    if "error.html" in url.lower():
        return False, True, f"redirected to {url}"

    blocked_phrases = (
        "system difficulties",
        "unable to process your request",
        "was not processed",
    )
    if any(p in body_text for p in blocked_phrases):
        return False, True, "FTC error page (system difficulties / not processed)"

    return False, False, f"success panel missing (url={url})"


def _is_proxy_network_error(error: Optional[str]) -> bool:
    if not error:
        return False
    err = error.lower()
    return any(
        token in err
        for token in (
            "err_timed_out",
            "err_proxy",
            "err_tunnel",
            "err_connection",
            "net::err_",
            "econnrefused",
            "econnreset",
            "timed out",
            "timeouterror",
            "timeout 30000ms exceeded",
        )
    )


def _format_minutes_dropdown(minute: int) -> str:
    # Round to nearest 5 since dropdown options are typically 00/05/10/.../55.
    snapped = (minute // 5) * 5
    return f"{snapped:02d}"


@dataclass
class FtcPlaywrightSubmitter(FtcSubmitter):
    cfg: AppConfig
    _proxy_rotator: Optional[ProxyRotator] = field(default=None, repr=False)
    _on_success_page: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        if self.cfg.ftc.proxies:
            self._proxy_rotator = ProxyRotator(
                self.cfg.ftc.proxies,
                mode=self.cfg.ftc.proxy_rotate,
                state_path=self.cfg.resolve_path("logs/proxy_index.txt"),
            )

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "FtcPlaywrightSubmitter":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self, *, proxy: Optional[dict] = None) -> None:
        """Launch a single browser to be reused across submissions."""
        if self._browser is not None and self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"Playwright not installed: {exc}") from exc

        if proxy is None and self._proxy_rotator is not None:
            proxy = self._proxy_rotator.advance_for_run_start()

        self._pw = sync_playwright().start()
        chrome_exe = _resolve_chrome_executable()
        launch_kwargs: dict = {
            "headless": self.cfg.ftc.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe
        else:
            launch_kwargs["channel"] = "chrome"
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._open_context(proxy)

    def _open_context(self, proxy: Optional[dict]) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        kwargs: dict = {
            "viewport": {"width": 1366, "height": 900},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if proxy:
            kwargs["proxy"] = proxy
            log.info("Playwright using proxy %s", proxy.get("server"))
        else:
            log.info("Playwright using direct connection (no proxy).")
        self._context = self._browser.new_context(**kwargs)
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = self._context.new_page()
        self._on_success_page = False

    def _reset_session(self, *, proxy: Optional[dict] = None) -> None:
        """Fresh browser context — use after throttle/errors or first launch."""
        if self._browser is None:
            self.start(proxy=proxy)
        else:
            self._open_context(proxy)
        self._on_success_page = False

    def _navigate_to_step_one(self, page) -> None:
        """Open step 1 — reuse success page when possible."""
        if self._on_success_page:
            btn = page.locator("#SubmitOtherComplaint_B")
            if btn.count() > 0 and btn.first.is_visible():
                log.info("Continuing via 'Submit Another Complaint'.")
                _human_pause(page, 400, 900)
                btn.first.click()
                page.wait_for_selector("#PhoneTextBox", timeout=30000)
                self._on_success_page = False
                return
            self._on_success_page = False

        page.goto(self.cfg.ftc.url, wait_until="domcontentloaded")

        # The form lives behind a "Continue" landing page. Click through
        # it whenever it appears (it appears for the FIRST submission
        # per browser session; subsequent visits go straight to step 1).
        try:
            main_btn = page.locator("#MainContinueButton")
            if main_btn.count() > 0 and main_btn.first.is_visible():
                log.debug("Clicking landing-page Continue button.")
                main_btn.first.click()
                page.wait_for_selector("#PhoneTextBox", timeout=20000)
        except Exception as exc:
            log.debug("MainContinueButton handling skipped: %s", exc)

        page.wait_for_selector("#PhoneTextBox", timeout=30000)

    def restart_with_next_proxy(self) -> None:
        """Close the browser context and relaunch through the next proxy."""
        proxy = None
        if self._proxy_rotator is not None:
            proxy = self._proxy_rotator.advance_on_throttle()
        if self._browser is None:
            self.start(proxy=proxy)
            return
        self._open_context(proxy)

    def rotate_after_successful_submit(self) -> None:
        """Switch proxy between submissions when ``proxy_rotate=each_submit``."""
        if self._proxy_rotator is None:
            return
        proxy = self._proxy_rotator.advance_after_submit()
        if self._browser is None:
            self.start(proxy=proxy)
        else:
            self._open_context(proxy)

    def stop(self) -> None:
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._page = None

    # -- submit -------------------------------------------------------------

    def submit(self, vm: Voicemail) -> SubmissionResult:
        try:
            from playwright.sync_api import (  # type: ignore
                TimeoutError as PlaywrightTimeoutError,
            )
        except ImportError as exc:  # pragma: no cover
            return SubmissionResult(
                success=False,
                error=f"Playwright not installed: {exc}",
            )

        owns_browser = False
        if not vm.caller_number:
            return SubmissionResult(success=False, error="missing caller_number")
        if not vm.received_at:
            return SubmissionResult(success=False, error="missing received_at")

        if self._page is None:
            self.start()
            owns_browser = True
        page = self._page
        assert page is not None

        screenshot_dir = self.cfg.resolve_path(self.cfg.ftc.screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"vm-{vm.id}.png"

        try:
            self._navigate_to_step_one(page)

            if self._captcha_present(page):
                return SubmissionResult(
                    success=False,
                    error="captcha detected on step 1",
                    captcha_detected=True,
                )

            self._fill_step_one(page, vm)
            _human_pause(page, 300, 700)
            page.locator("#StepOneContinueButton").click()
            _human_pause(page, 400, 900)

            page.wait_for_selector(
                "#CallerPhoneNumberTextBox", timeout=30000
            )

            if self._captcha_present(page):
                return SubmissionResult(
                    success=False,
                    error="captcha detected on step 2",
                    captcha_detected=True,
                    response_url=page.url,
                )

            self._fill_step_two(page, vm)
            _human_pause(page, 400, 900)
            page.locator("#StepTwoSubmitButton").click()

            # Wait for either success or the generic FTC error redirect.
            try:
                page.wait_for_function(
                    """() => {
                        if (document.querySelector('#StepTwoAcceptedPanel')) return true;
                        if (document.querySelector('#SubmitOtherComplaint_B')) return true;
                        const u = location.href.toLowerCase();
                        if (u.includes('error.html')) return true;
                        const t = (document.body && document.body.innerText || '').toLowerCase();
                        return t.includes('system difficulties')
                            || t.includes('was not processed');
                    }""",
                    timeout=45000,
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(1500)
            page.screenshot(path=str(screenshot_path))
            ok, throttled, detail = _classify_post_submit(page)
            if ok:
                self._on_success_page = True
                return SubmissionResult(
                    success=True,
                    screenshot_path=str(screenshot_path),
                    response_url=page.url,
                )
            self._on_success_page = False
            return SubmissionResult(
                success=False,
                error=(
                    f"blocked by donotcall.gov ({detail}) — retry later or "
                    f"submit manually"
                    if throttled
                    else detail
                ),
                screenshot_path=str(screenshot_path),
                throttled=throttled,
                response_url=page.url,
            )

        except Exception as exc:  # pragma: no cover - browser flake
            self._on_success_page = False
            try:
                page.screenshot(path=str(screenshot_path))
            except Exception:
                pass
            return SubmissionResult(
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                screenshot_path=str(screenshot_path),
            )
        finally:
            if owns_browser:
                self.stop()

    # -- step builders ------------------------------------------------------

    def _fill_step_one(self, page, vm: Voicemail) -> None:
        gv = _digits(self.cfg.gv_number)
        ts: datetime = vm.received_at  # type: ignore[assignment]

        _human_fill(page, "#PhoneTextBox", gv)
        _human_fill(page, "#DateOfCallTextBox", ts.strftime("%m/%d/%Y"))

        # The date field is a jQuery UI datepicker that opens a popup on
        # focus and intercepts clicks on every following field. Dismiss it
        # explicitly without clicking anywhere (the page header contains
        # full-width anchor links that would navigate away).
        try:
            page.keyboard.press("Escape")
            page.evaluate(
                "document.activeElement && document.activeElement.blur && document.activeElement.blur();"
            )
            page.evaluate(
                "if (window.jQuery) { jQuery('#ui-datepicker-div').hide(); "
                "jQuery.datepicker && jQuery.datepicker._hideDatepicker && "
                "jQuery.datepicker._hideDatepicker(); }"
            )
            page.evaluate(
                "var d = document.getElementById('ui-datepicker-div'); "
                "if (d) { d.style.display = 'none'; }"
            )
            page.wait_for_timeout(200)
        except Exception:
            pass

        _human_select(
            page, "#TimeOfCallDropDownList", _format_time_dropdown(ts.hour)
        )
        _human_select(page, "#ddlMinutes", _format_minutes_dropdown(ts.minute))

        _human_check(page, "#PrerecordMessageYESRadioButton")
        _human_check(page, "#PhoneCallRadioButton")

        subject_id = vm.ftc_subject_id if vm.ftc_subject_id is not None else 0
        _human_select(page, "#ddlSubjectMatter", str(subject_id))
        if subject_id == 1 and vm.ftc_subject_text:
            _human_fill(page, "#txtSubjectMatter", vm.ftc_subject_text[:120])

    def _fill_step_two(self, page, vm: Voicemail) -> None:
        p = self.cfg.personal
        caller = _digits(vm.caller_number) or vm.caller_number or ""

        _human_fill(page, "#CallerPhoneNumberTextBox", caller)
        _human_fill(
            page,
            "#CallerNameTextBox",
            (vm.claimed_company or vm.caller_display_name or "n/a")[:120],
        )

        _human_check(page, "#HaveBusinessNoRadioButton")
        _human_check(page, "#StopCallingNoRadioButton")

        _human_fill(page, "#FirstNameTextBox", p.first_name)
        _human_fill(page, "#LastNameTextBox", p.last_name)
        _human_fill(page, "#StreetAddressTextBox", p.street_address)
        _human_fill(page, "#CityTextBox", p.city)
        _human_select(page, "#StateDropDownList", p.state)
        _human_fill(page, "#ZipCodeTextBox", p.zip_code)

        comment = (vm.comment_text or "").strip()
        if not comment:
            comment = self._fallback_comment(vm)
        _human_fill(page, "#CommentTextBox", comment[:1000])

    def _fallback_comment(self, vm: Voicemail) -> str:
        bits = []
        if vm.caller_number:
            bits.append(f"Called from {vm.caller_number}.")
        if vm.callback_number:
            bits.append(f"Left callback number {vm.callback_number}.")
        if vm.claimed_company:
            bits.append(f"Caller identified as: {vm.claimed_company}.")
        if vm.summary:
            bits.append(vm.summary)
        if vm.transcript:
            bits.append(f"Transcript: {vm.transcript}")
        return " ".join(bits)[:1000]

    def _captcha_present(self, page) -> bool:
        try:
            return page.locator("iframe[src*='recaptcha']").count() > 0
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

def submit_approved(
    cfg: AppConfig,
    *,
    limit: Optional[int] = None,
    once: bool = False,
) -> int:
    """Drain the approved queue. Returns count of successful submissions.

    ``once=True`` exits after one pass; otherwise loops forever, sleeping
    between cycles. Used as a long-running daemon in normal operation.
    """
    init_db(cfg.resolve_path(cfg.database.path))

    successes = 0
    processed = 0
    submitter = FtcPlaywrightSubmitter(cfg=cfg)
    submitter.start()
    try:
      while True:
        with session_scope() as session:
            stmt = (
                select(Voicemail)
                .where(Voicemail.status == STATUS_APPROVED)
                .order_by(Voicemail.received_at.asc())
            )
            if limit:
                stmt = stmt.limit(limit)
            row_ids = [r.id for r in session.execute(stmt).scalars()]

        if not row_ids:
            log.info("No approved voicemails awaiting submission.")
            if once:
                break
            time.sleep(60)
            continue

        for vm_id in row_ids:
            with session_scope() as session:
                vm = session.get(Voicemail, vm_id)
                if vm is None or vm.status != STATUS_APPROVED:
                    continue

                log.info("Submitting VM %s (%s)...", vm.id, vm.caller_number)
                result = submitter.submit(vm)

                if result.captcha_detected:
                    log.warning(
                        "CAPTCHA detected on VM %s. Leaving status=approved "
                        "for manual intervention.",
                        vm.id,
                    )
                    vm.submit_error = "captcha detected"
                    if result.screenshot_path:
                        vm.submit_screenshot = result.screenshot_path
                elif result.throttled:
                    # Keep row in 'approved' so the next run picks it up
                    # automatically when the throttle window expires.
                    vm.submit_error = result.error
                    vm.submit_screenshot = result.screenshot_path
                    log.warning(
                        "VM %s blocked (%s). Stopping batch so we don't burn "
                        "through the queue with more failures. Re-run later "
                        "or submit manually in a normal browser.",
                        vm.id,
                        result.response_url or result.error,
                    )
                    # If proxies are configured, rotate once and retry
                    # immediately before giving up on this batch.
                    if (
                        submitter._proxy_rotator is not None
                        and len(submitter._proxy_rotator.proxies) > 1
                    ):
                        log.info(
                            "Rotating proxy and retrying VM %s once…", vm.id
                        )
                        submitter.restart_with_next_proxy()
                        retry = submitter.submit(vm)
                        if retry.success:
                            vm.status = STATUS_SUBMITTED
                            vm.submitted_at = datetime.utcnow()
                            vm.submit_error = None
                            vm.submit_screenshot = retry.screenshot_path
                            successes += 1
                            log.info(
                                "VM %s submitted successfully after proxy rotate.",
                                vm.id,
                            )
                            processed += 1
                            if submitter._proxy_rotator.mode == "each_submit":
                                submitter.rotate_after_successful_submit()
                            continue
                        if retry.throttled:
                            vm.submit_error = retry.error
                            vm.submit_screenshot = retry.screenshot_path
                            result = retry
                        elif not retry.success:
                            vm.status = STATUS_SUBMIT_FAILED
                            vm.submit_error = retry.error
                            vm.submit_screenshot = retry.screenshot_path
                            log.error(
                                "VM %s failed after proxy rotate: %s",
                                vm.id,
                                retry.error,
                            )
                            processed += 1
                            continue
                elif result.success:
                    vm.status = STATUS_SUBMITTED
                    vm.submitted_at = datetime.utcnow()
                    vm.submit_error = None
                    vm.submit_screenshot = result.screenshot_path
                    successes += 1
                    log.info("VM %s submitted successfully.", vm.id)
                    if submitter._proxy_rotator and submitter._proxy_rotator.mode == "each_submit":
                        submitter.rotate_after_successful_submit()
                else:
                    if (
                        _is_proxy_network_error(result.error)
                        and submitter._proxy_rotator is not None
                        and len(submitter._proxy_rotator.proxies) > 1
                    ):
                        log.warning(
                            "VM %s proxy/network error (%s). Rotating and retrying once…",
                            vm.id,
                            result.error,
                        )
                        submitter.restart_with_next_proxy()
                        retry = submitter.submit(vm)
                        if retry.success:
                            vm.status = STATUS_SUBMITTED
                            vm.submitted_at = datetime.utcnow()
                            vm.submit_error = None
                            vm.submit_screenshot = retry.screenshot_path
                            successes += 1
                            log.info(
                                "VM %s submitted successfully after proxy rotate.",
                                vm.id,
                            )
                            processed += 1
                            if submitter._proxy_rotator.mode == "each_submit":
                                submitter.rotate_after_successful_submit()
                            continue
                        if retry.throttled:
                            vm.submit_error = retry.error
                            vm.submit_screenshot = retry.screenshot_path
                            result = retry
                        elif _is_proxy_network_error(retry.error):
                            vm.submit_error = retry.error
                            vm.submit_screenshot = retry.screenshot_path
                            log.warning(
                                "VM %s still unreachable after proxy rotate; "
                                "leaving approved for next run.",
                                vm.id,
                            )
                            processed += 1
                            continue
                        else:
                            vm.status = STATUS_SUBMIT_FAILED
                            vm.submit_error = retry.error
                            vm.submit_screenshot = retry.screenshot_path
                            log.error(
                                "VM %s failed after proxy rotate: %s",
                                vm.id,
                                retry.error,
                            )
                            processed += 1
                            continue
                    vm.status = STATUS_SUBMIT_FAILED
                    vm.submit_error = result.error
                    vm.submit_screenshot = result.screenshot_path
                    log.error("VM %s submission failed: %s", vm.id, result.error)

            # If the FTC throttled us, bail out of the batch immediately.
            if result.throttled:
                log.info(
                    "Throttle detected — aborting this batch. "
                    "%d submitted, %d remaining in queue.",
                    successes,
                    len(row_ids) - processed - 1,
                )
                return successes

            processed += 1
            if limit and processed >= limit:
                return successes

            interval = cfg.ftc.submit_interval_sec
            jitter = interval * 0.5 * (random.random() - 0.5) * 2
            sleep_for = max(2.0, interval + jitter)
            log.debug("Sleeping %.1fs before next submission.", sleep_for)
            time.sleep(sleep_for)

        if once:
            break
    finally:
        submitter.stop()

    return successes


def run(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    submit_approved(cfg, once=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(run())
