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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

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


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _digits(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def _format_time_dropdown(hour: int) -> str:
    return f"{hour:02d}"


def _format_minutes_dropdown(minute: int) -> str:
    # Round to nearest 5 since dropdown options are typically 00/05/10/.../55.
    snapped = (minute // 5) * 5
    return f"{snapped:02d}"


@dataclass
class FtcPlaywrightSubmitter(FtcSubmitter):
    cfg: AppConfig

    def __post_init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "FtcPlaywrightSubmitter":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    def start(self) -> None:
        """Launch a single browser to be reused across submissions."""
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"Playwright not installed: {exc}") from exc

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.cfg.ftc.headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()

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

        if not vm.caller_number:
            return SubmissionResult(success=False, error="missing caller_number")
        if not vm.received_at:
            return SubmissionResult(success=False, error="missing received_at")

        # Lazily start the browser if the caller didn't use the context
        # manager (back-compat with one-off callers).
        owns_browser = False
        if self._page is None:
            self.start()
            owns_browser = True
        page = self._page

        screenshot_dir = self.cfg.resolve_path(self.cfg.ftc.screenshot_dir)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"vm-{vm.id}.png"

        try:
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

            if self._captcha_present(page):
                return SubmissionResult(
                    success=False,
                    error="captcha detected on step 1",
                    captcha_detected=True,
                )

            self._fill_step_one(page, vm)
            page.click("#StepOneContinueButton")

            page.wait_for_selector(
                "#CallerPhoneNumberTextBox", timeout=30000
            )

            if self._captcha_present(page):
                return SubmissionResult(
                    success=False,
                    error="captcha detected on step 2",
                    captcha_detected=True,
                )

            self._fill_step_two(page, vm)
            page.click("#StepTwoSubmitButton")

            try:
                page.wait_for_selector(
                    "#StepTwoAcceptedPanel", timeout=45000
                )
            except PlaywrightTimeoutError:
                page.screenshot(path=str(screenshot_path))
                # Detect the FTC's server-side throttle message so the worker
                # can back off instead of burning through the queue.
                throttled = False
                try:
                    body_text = page.locator("body").inner_text(timeout=2000)
                    if "system difficulties" in body_text.lower() or (
                        "unable to process your request" in body_text.lower()
                    ):
                        throttled = True
                except Exception:
                    pass
                return SubmissionResult(
                    success=False,
                    error=(
                        "throttled by donotcall.gov (server returned 'system "
                        "difficulties' page) — retry later"
                        if throttled
                        else "success panel never appeared"
                    ),
                    screenshot_path=str(screenshot_path),
                    throttled=throttled,
                )

            page.screenshot(path=str(screenshot_path))
            return SubmissionResult(
                success=True,
                screenshot_path=str(screenshot_path),
            )

        except Exception as exc:  # pragma: no cover - browser flake
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

        page.fill("#PhoneTextBox", gv)
        page.fill("#DateOfCallTextBox", ts.strftime("%m/%d/%Y"))

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

        page.select_option(
            "#TimeOfCallDropDownList", _format_time_dropdown(ts.hour)
        )
        page.select_option("#ddlMinutes", _format_minutes_dropdown(ts.minute))

        # Use force=True so any decorative label / radio replacement element
        # doesn't block the input click.
        page.check("#PrerecordMessageYESRadioButton", force=True)
        page.check("#PhoneCallRadioButton", force=True)

        subject_id = vm.ftc_subject_id if vm.ftc_subject_id is not None else 0
        page.select_option("#ddlSubjectMatter", str(subject_id))
        if subject_id == 1 and vm.ftc_subject_text:
            page.fill("#txtSubjectMatter", vm.ftc_subject_text[:120])

    def _fill_step_two(self, page, vm: Voicemail) -> None:
        p = self.cfg.personal
        caller = _digits(vm.caller_number) or vm.caller_number or ""

        page.fill("#CallerPhoneNumberTextBox", caller)
        page.fill(
            "#CallerNameTextBox",
            (vm.claimed_company or vm.caller_display_name or "n/a")[:120],
        )

        page.check("#HaveBusinessNoRadioButton", force=True)
        page.check("#StopCallingNoRadioButton", force=True)

        page.fill("#FirstNameTextBox", p.first_name)
        page.fill("#LastNameTextBox", p.last_name)
        page.fill("#StreetAddressTextBox", p.street_address)
        page.fill("#CityTextBox", p.city)
        page.select_option("#StateDropDownList", p.state)
        page.fill("#ZipCodeTextBox", p.zip_code)

        comment = (vm.comment_text or "").strip()
        if not comment:
            comment = self._fallback_comment(vm)
        page.fill("#CommentTextBox", comment[:1000])

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
                        "VM %s throttled. Stopping batch so we don't burn "
                        "through the queue with more failures. Re-run later "
                        "(or `python -m ftc_automation submit` once an hour).",
                        vm.id,
                    )
                elif result.success:
                    vm.status = STATUS_SUBMITTED
                    vm.submitted_at = datetime.utcnow()
                    vm.submit_error = None
                    vm.submit_screenshot = result.screenshot_path
                    successes += 1
                    log.info("VM %s submitted successfully.", vm.id)
                else:
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
