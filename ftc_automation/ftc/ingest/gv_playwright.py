"""Playwright-based scraper for the Google Voice voicemail backlog.

Usage (via CLI):

    python -m ftc_automation login          # interactive login, saves storage_state
    python -m ftc_automation ingest --backlog

The login command opens a real browser window and waits for you to sign
in to https://voice.google.com manually. After you reach the inbox the
script saves an authenticated Playwright ``storage_state.json``. Future
runs use that file headlessly.

The scraper walks the voicemail tab, scrolling the virtualised list to
load every thread, and pulls out the caller number/name, transcription,
timestamp, and duration. For threads whose participant is a contact name
(not a phone number), it opens the thread to read the real number off
the thread header.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from dateutil import parser as dateparser

from ..config import AppConfig, load_config
from ..db import (
    SOURCE_PLAYWRIGHT,
    STATUS_NEW,
    init_db,
    session_scope,
    upsert_voicemail,
)


log = logging.getLogger(__name__)


VOICE_URL = "https://voice.google.com/u/0/voicemail"
# Force Google's actual sign-in flow instead of relying on
# https://voice.google.com/ , which (when no session is present) sometimes
# redirects to https://workspace.google.com/products/voice/ — the public
# marketing page with no login button.
LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin"
    "?service=grandcentral"
    "&continue=https%3A%2F%2Fvoice.google.com%2Fu%2F0%2Fvoicemail"
)

# Google Voice doesn't expose a stable element tag for voicemail rows, so we
# try a list of selectors and use whichever one matches.
THREAD_SELECTORS = [
    "gv-voicemail-thread-list-item",
    "gv-text-thread-item",
    "[data-e2e-thread-list-item]",
    "a[href*='/voicemail/']",
    "a[href*='/messages/']",
    "div[role='listitem'] a[href*='voice.google.com']",
]


# ---------------------------------------------------------------------------
# Interactive login: produces storage_state.json
# ---------------------------------------------------------------------------

def _resolve_chrome_executable() -> Optional[str]:
    """Find a real Google Chrome install. Returns None if not found."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    return None


def interactive_login(storage_state_path: Path) -> None:
    """Open a real browser, wait for the user to log in, then save state.

    Uses real Google Chrome (with the AutomationControlled flag stripped)
    via a persistent context, because Google rejects sign-ins from
    Playwright's bundled Chromium with "this browser may not be secure".

    The session is auto-saved as soon as either condition is met:
      * the URL contains "voice.google.com/.../messages" (voicemail inbox), OR
      * the user closes the browser window.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is not installed. Run `pip install -r requirements.txt` "
            "and then `playwright install chromium`."
        ) from exc

    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    user_data_dir = storage_state_path.parent / "chrome_profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    chrome_exe = _resolve_chrome_executable()
    log.info(
        "Opening browser for Google Voice login (chrome=%s)...",
        chrome_exe or "playwright-bundled chromium",
    )

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-default-browser-check",
        "--no-first-run",
    ]

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            executable_path=chrome_exe,           # None falls back to bundled Chromium
            channel="chrome" if not chrome_exe else None,
            headless=False,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
        )

        # Make navigator.webdriver evaluate to undefined so Google's check passes.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        closed = {"value": False}

        def _on_close(_):
            closed["value"] = True

        context.on("close", _on_close)

        page = context.pages[0] if context.pages else context.new_page()
        page.on("close", _on_close)
        page.goto(LOGIN_URL)

        print(
            "\n--- Google Voice login ---\n"
            "1. Sign in to your Google account in the browser window.\n"
            "   (If the page won't load or shows the marketing site,\n"
            "    open https://accounts.google.com in the same window\n"
            "    and sign in there first, then go to voice.google.com.)\n"
            "2. Wait until you can see your voicemail inbox.\n"
            "   (Session auto-saves as soon as you reach the inbox.)\n"
            "3. Or just close the browser window when you're done.\n",
            flush=True,
        )

        saved = False
        last_log = 0.0
        import time as _time

        while not closed["value"]:
            urls: list[str] = []
            for p in list(context.pages):
                try:
                    urls.append(p.url)
                except Exception:
                    continue

            # Periodic visibility into what URLs we're seeing.
            now = _time.time()
            if now - last_log > 5:
                log.info("Watching %d tab(s): %s", len(urls), urls)
                last_log = now

            # Any authenticated voice.google.com page (calls, messages,
            # voicemail) means sign-in succeeded.
            matched = any(
                "voice.google.com/u/" in u
                or ("voice.google.com" in u and "/messages" in u)
                or ("voice.google.com" in u and "/calls" in u)
                for u in urls
            )

            if matched:
                try:
                    context.storage_state(path=str(storage_state_path))
                    saved = True
                    log.info("Signed in to Google Voice — session saved.")
                    print(
                        "\nLogin captured. You can close the browser window now.\n",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    log.warning("Could not save storage state yet: %s", exc)

            try:
                _time.sleep(1)
            except Exception:
                break

        if not saved:
            try:
                context.storage_state(path=str(storage_state_path))
                saved = True
                log.info("Saved storage_state on browser close.")
            except Exception as exc:
                log.error("Failed to save storage state: %s", exc)

        try:
            context.close()
        except Exception:
            pass

    if saved:
        log.info("Saved storage_state to %s", storage_state_path)
    else:
        raise SystemExit("Login flow ended without saving a session.")


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"\+?\d[\d\-\.\s()]{6,}\d")


def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Pull a phone number out of arbitrary text and reduce to digits."""
    if not raw:
        return None
    m = _PHONE_RE.search(raw)
    if not m:
        return None
    digits = "".join(ch for ch in m.group(0) if ch.isdigit() or ch == "+")
    if not digits:
        return None
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = dateparser.parse(raw, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if dt is None:
        return None
    now = datetime.now()
    if dt > now:
        log.warning("GV timestamp %r parsed as future %s — clamping to now.", raw, dt)
        return now
    return dt


def _parse_duration(raw: Optional[str]) -> Optional[int]:
    """Parse a duration like '0:34' or '1:02:55' into seconds."""
    if not raw:
        return None
    parts = [p for p in raw.strip().split(":") if p.strip()]
    if not parts or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def _pick_thread_selector(page) -> str:
    """Wait for and return whichever known selector actually matches."""
    deadline = 60.0
    import time as _time

    start = _time.time()
    while _time.time() - start < deadline:
        for sel in THREAD_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    log.info("Using thread selector: %s", sel)
                    return sel
            except Exception:
                continue
        page.wait_for_timeout(1000)
    raise RuntimeError(
        "None of the known thread selectors matched. Google Voice's DOM may "
        "have changed. Try opening voice.google.com/u/0/voicemail and use "
        "DevTools to find the current row tag."
    )


def _extract_visible_rows(page, sel: str) -> list[dict]:
    """Snapshot whatever rows are currently rendered in the viewport."""
    out: list[dict] = []
    items = page.locator(sel)
    for i in range(items.count()):
        item = items.nth(i)
        participant = _try_text(item, [
            "gv-annotation.participant",
            "[data-e2e-thread-list-item-participant]",
            "[class*='participant']",
        ])
        transcription = _try_text(item, [
            "gv-annotation.transcription",
            "[data-e2e-thread-list-item-text]",
            "[class*='snippet']",
            "[class*='preview']",
        ])
        timestamp_text = _try_text(item, [
            "[aria-label*='20']",
            "time",
            "[class*='timestamp']",
            "[class*='time']",
        ])
        duration_text = _try_text(item, [
            ".duration",
            "[class*='duration']",
        ])

        if not participant and not transcription:
            try:
                full = item.inner_text(timeout=2000).strip()
                lines = [l.strip() for l in full.split("\n") if l.strip()]
                if lines:
                    participant = lines[0]
                    if len(lines) > 1:
                        transcription = " ".join(lines[1:])
            except Exception:
                pass

        try:
            data_id = (
                item.get_attribute("data-id")
                or item.get_attribute("data-e2e-thread-id")
                or item.get_attribute("href")
                or item.get_attribute("id")
                or ""
            )
        except Exception:
            data_id = ""

        out.append(
            {
                "data_id": data_id,
                "participant": participant,
                "transcription": transcription,
                "timestamp_text": timestamp_text,
                "duration_text": duration_text,
            }
        )
    return out


def _row_key(row: dict) -> str:
    """Stable per-voicemail identifier derived from intrinsic content."""
    if row.get("data_id"):
        return f"id:{row['data_id']}"
    # (participant, timestamp, first 40 chars of transcript) is unique enough
    # for the voicemail tab — same caller can't leave two voicemails at the
    # exact same minute with the same first snippet.
    transcript = (row.get("transcription") or "")[:40].strip()
    return "|".join([
        (row.get("participant") or "").strip(),
        (row.get("timestamp_text") or "").strip(),
        transcript,
    ])


def _scrape_thread_list(
    page, *, max_threads: Optional[int] = None
) -> tuple[list[dict], str]:
    """Scroll the (virtualised) voicemail list, deduping across scroll
    positions. Returns ``(rows, selector_used)``.

    Google Voice uses a CDK virtual scroll viewport — off-screen rows are
    destroyed from the DOM, so we have to extract incrementally as we
    scroll instead of waiting for the full list to be present.
    """
    sel = _pick_thread_selector(page)

    collected: dict[str, dict] = {}
    stable_iters = 0
    last_size = -1
    no_new_iters = 0

    # Try to find the actual scrollable viewport so we can scroll it
    # directly rather than relying on the last row being in view.
    viewport_selectors = [
        "cdk-virtual-scroll-viewport",
        "[role='list']",
        "[class*='thread-list']",
    ]
    viewport_handle = None
    for vs in viewport_selectors:
        try:
            loc = page.locator(vs).first
            if loc.count() > 0:
                viewport_handle = loc
                break
        except Exception:
            continue

    while True:
        # 1. Grab everything currently visible.
        new_rows = _extract_visible_rows(page, sel)
        added = 0
        for r in new_rows:
            k = _row_key(r)
            if not k or k == "||":
                continue
            if k not in collected:
                collected[k] = r
                added += 1

        size = len(collected)
        if max_threads is not None and size >= max_threads:
            log.info(
                "Reached requested cap of %d thread(s); stopping scroll.",
                max_threads,
            )
            break

        if size != last_size:
            log.info(
                "Scraped %d unique thread(s) so far (+%d this iter).",
                size,
                added,
            )
            last_size = size
            no_new_iters = 0
        else:
            no_new_iters += 1

        # 2. Scroll the viewport down a page-worth.
        scrolled = False
        if viewport_handle is not None:
            try:
                viewport_handle.evaluate(
                    "el => { el.scrollTop = el.scrollTop + el.clientHeight * 0.9; }"
                )
                scrolled = True
            except Exception:
                scrolled = False
        if not scrolled:
            try:
                items = page.locator(sel)
                if items.count() > 0:
                    items.nth(items.count() - 1).scroll_into_view_if_needed(
                        timeout=3000
                    )
                    scrolled = True
            except Exception:
                pass
        if not scrolled:
            try:
                page.keyboard.press("PageDown")
            except Exception:
                pass

        page.wait_for_timeout(900)

        # 3. Stop when scrolling has stopped revealing new rows for several
        #    iterations in a row.
        if no_new_iters >= 6:
            log.info(
                "No new threads after %d scroll iterations; stopping.",
                no_new_iters,
            )
            break

    log.info("Found %d unique voicemail thread(s).", len(collected))
    rows = list(collected.values())
    return rows, sel


def _try_text(locator, selectors: list[str]) -> str:
    for s in selectors:
        try:
            sub = locator.locator(s).first
            if sub.count() > 0:
                t = sub.inner_text(timeout=1000).strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def _resolve_caller_via_thread(page, item_locator) -> Optional[str]:
    """Open a thread and read the real caller number from the header."""
    try:
        item_locator.click(timeout=5000)
    except Exception:
        return None

    try:
        page.wait_for_selector("gv-thread-details-header", timeout=8000)
    except Exception:
        return None

    candidates: list[str] = []
    try:
        candidates.append(page.locator("gv-thread-details-header").inner_text(timeout=2000))
    except Exception:
        pass

    number = None
    for text in candidates:
        number = _normalize_phone(text)
        if number:
            break
    return number


def scrape_backlog(cfg: AppConfig, *, limit: Optional[int] = None) -> int:
    """Scrape the voicemail backlog into the database. Returns # new rows.

    ``limit`` (if set) overrides ``cfg.google_voice.max_threads`` for this run.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is not installed. Run `pip install -r requirements.txt`."
        ) from exc

    storage_state_path = cfg.resolve_path(cfg.google_voice.storage_state_path)
    if not storage_state_path.exists():
        raise SystemExit(
            f"No storage_state found at {storage_state_path}. Run "
            f"`python -m ftc_automation login` first."
        )

    init_db(cfg.resolve_path(cfg.database.path))
    cap = limit if limit is not None else cfg.google_voice.max_threads

    chrome_exe = _resolve_chrome_executable()

    inserted = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=chrome_exe,
            channel="chrome" if not chrome_exe else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            storage_state=str(storage_state_path),
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        page.goto(VOICE_URL, wait_until="domcontentloaded", timeout=60000)
        # Give the SPA a beat to render after DOM ready.
        page.wait_for_timeout(2500)

        try:
            threads, sel_used = _scrape_thread_list(page, max_threads=cap)
        except Exception as exc:
            log.error("Scrape failed: %s. Saving debug screenshot.", exc)
            debug_path = cfg.resolve_path("submissions") / "gv_debug.png"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(debug_path), full_page=True)
                log.error("Debug screenshot: %s", debug_path)
                log.error("Current URL: %s", page.url)
            except Exception:
                pass
            browser.close()
            raise

        threads = threads[:cap]

        for i, t in enumerate(threads):
            number = _normalize_phone(t["participant"])
            display_name = None if number else (t["participant"] or None)
            # Per-thread caller-number resolution (clicking each row to read
            # the phone off the header) is incompatible with virtualized
            # scrolling — once we've scrolled past a row, the DOM no longer
            # contains it. The review UI lets the user fill in the number
            # for any contact-named row before submission.

            received_at = _parse_timestamp(t["timestamp_text"])
            duration = _parse_duration(t["duration_text"])

            # Build a stable, content-derived ID so re-running the ingest
            # doesn't create duplicate rows.
            if t["data_id"]:
                source_msg_id = f"gv:{t['data_id']}"
            else:
                key_basis = "|".join([
                    (t.get("participant") or "").strip(),
                    (t.get("timestamp_text") or "").strip(),
                    (t.get("transcription") or "")[:80].strip(),
                ])
                digest = hashlib.sha1(key_basis.encode("utf-8")).hexdigest()[:16]
                source_msg_id = f"gv:hash:{digest}"

            defaults = {
                "caller_number": number,
                "caller_display_name": display_name,
                "received_at": received_at,
                "duration_sec": duration,
                "transcript": t["transcription"] or None,
                "status": STATUS_NEW,
            }

            with session_scope() as session:
                _, created = upsert_voicemail(
                    session,
                    source=SOURCE_PLAYWRIGHT,
                    source_msg_id=source_msg_id,
                    defaults=defaults,
                )
                if created:
                    inserted += 1

        browser.close()

    log.info("Backlog ingest complete. %d new voicemail(s) added.", inserted)
    return inserted


def run(argv: Optional[Iterable[str]] = None) -> int:
    """Module entry point used by ``python -m ftc_automation.ftc.ingest.gv_playwright``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    scrape_backlog(cfg)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
