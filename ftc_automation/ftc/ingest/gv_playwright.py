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

import logging
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


VOICE_URL = "https://voice.google.com/u/0/messages?itemId=voicemail"
LOGIN_URL = "https://voice.google.com/"


# ---------------------------------------------------------------------------
# Interactive login: produces storage_state.json
# ---------------------------------------------------------------------------

def interactive_login(storage_state_path: Path) -> None:
    """Open a real browser, wait for the user to log in, then save state."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is not installed. Run `pip install -r requirements.txt` "
            "and then `playwright install chromium`."
        ) from exc

    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Opening browser for Google Voice login...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)

        print(
            "\n--- Google Voice login ---\n"
            "1. Sign in to your Google account in the browser window.\n"
            "2. Wait until you can see your voicemail inbox.\n"
            "3. Come back to this terminal and press <Enter> to save the session.\n",
            flush=True,
        )
        try:
            input()
        except EOFError:
            pass

        context.storage_state(path=str(storage_state_path))
        browser.close()

    log.info("Saved storage_state to %s", storage_state_path)


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
        return dateparser.parse(raw, fuzzy=True)
    except (ValueError, OverflowError):
        return None


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


def _scrape_thread_list(page) -> list[dict]:
    """Scroll the voicemail list and return per-thread raw data."""
    page.wait_for_selector("gv-voicemail-thread-list-item", timeout=30000)

    # Scroll until count stops growing.
    last_count = -1
    stable_iters = 0
    while stable_iters < 3:
        items = page.locator("gv-voicemail-thread-list-item")
        count = items.count()
        if count == last_count:
            stable_iters += 1
        else:
            stable_iters = 0
        last_count = count
        if count:
            try:
                items.nth(count - 1).scroll_into_view_if_needed(timeout=5000)
            except Exception:  # pragma: no cover - defensive
                pass
        page.wait_for_timeout(800)

    log.info("Found %d voicemail thread(s) in the list.", last_count)

    out: list[dict] = []
    items = page.locator("gv-voicemail-thread-list-item")
    for i in range(items.count()):
        item = items.nth(i)
        try:
            participant = item.locator("gv-annotation.participant").first.inner_text(timeout=2000).strip()
        except Exception:
            participant = ""
        try:
            transcription = item.locator("gv-annotation.transcription").first.inner_text(timeout=2000).strip()
        except Exception:
            transcription = ""
        try:
            timestamp_text = item.locator("[aria-label*=20], time, .timestamp").first.inner_text(timeout=1000).strip()
        except Exception:
            timestamp_text = ""
        try:
            duration_text = item.locator(".duration, [class*=duration]").first.inner_text(timeout=1000).strip()
        except Exception:
            duration_text = ""
        try:
            data_id = item.get_attribute("data-id") or item.get_attribute("id") or ""
        except Exception:
            data_id = ""

        out.append(
            {
                "index": i,
                "data_id": data_id,
                "participant": participant,
                "transcription": transcription,
                "timestamp_text": timestamp_text,
                "duration_text": duration_text,
            }
        )
    return out


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


def scrape_backlog(cfg: AppConfig) -> int:
    """Scrape the voicemail backlog into the database. Returns # new rows."""
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

    inserted = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(storage_state_path))
        page = context.new_page()
        page.goto(VOICE_URL, wait_until="networkidle")

        threads = _scrape_thread_list(page)
        threads = threads[: cfg.google_voice.max_threads]

        for i, t in enumerate(threads):
            number = _normalize_phone(t["participant"])
            display_name = None if number else (t["participant"] or None)
            if number is None:
                items = page.locator("gv-voicemail-thread-list-item")
                if i < items.count():
                    number = _resolve_caller_via_thread(page, items.nth(i))
                    page.go_back(wait_until="networkidle")

            received_at = _parse_timestamp(t["timestamp_text"])
            duration = _parse_duration(t["duration_text"])

            source_msg_id = t["data_id"] or (
                f"idx-{i}-{(number or '')}-{received_at.isoformat() if received_at else ''}"
            )

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
