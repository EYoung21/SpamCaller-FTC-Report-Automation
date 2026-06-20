"""Playwright scraper for Google Voice call history.

Usage (via CLI):

    python -m ftc_automation ingest --calls
    python -m ftc_automation ingest --calls --since-days 14
    python -m ftc_automation ingest --calls --limit 50   # smoke test

Walks https://voice.google.com/u/0/calls, scrolls the virtualised list,
and ingests:

1. Rows Google Voice labeled as suspected spam (auto-approved).
2. Missed/answered calls from numbers already proven spam via voicemail
   classification (number cross-match; auto-approved, no transcript needed).

Non-flagged calls from unknown numbers are skipped — no LLM guesswork.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from ..classify.ftc_mapping import map_scam_category
from ..config import AppConfig
from ..db import (
    RECORD_CALL,
    RECORD_VOICEMAIL,
    SOURCE_PLAYWRIGHT,
    STATUS_APPROVED,
    STATUS_REJECTED,
    Voicemail,
    init_db,
    session_scope,
    upsert_voicemail,
)
from .gv_playwright import (
    _normalize_phone,
    _open_gv_browser,
    _parse_duration,
    _parse_timestamp,
    _scrape_thread_list,
    _try_text,
)


log = logging.getLogger(__name__)

CALLS_URL = "https://voice.google.com/u/0/calls"


def _caller_digits(raw: Optional[str]) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _load_proven_spam_digits(session) -> set[str]:
    """Digits for numbers established as spam via classified voicemails."""
    digits: set[str] = set()
    rows = session.execute(
        select(Voicemail.caller_number, Voicemail.callback_number).where(
            Voicemail.record_type == RECORD_VOICEMAIL,
            Voicemail.is_spam.is_(True),
            Voicemail.status != STATUS_REJECTED,
        )
    ).all()
    for caller, callback in rows:
        for num in (caller, callback):
            d = _caller_digits(num)
            if len(d) >= 10:
                digits.add(d)
    return digits

CALL_THREAD_SELECTORS = [
    "gv-call-thread-list-item",
    "gv-text-thread-item",
    "[data-e2e-thread-list-item]",
    "a[href*='/calls/']",
    "div[role='listitem'] a[href*='voice.google.com']",
]

_ARIA_DATE_RE = re.compile(
    r"(?:Sunday|Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|"
    r"Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
    r"[^.]*\d{4}[^.]*\d{1,2}:\d{2}\s*[AP]M",
    re.IGNORECASE,
)


def _parse_call_type(item) -> Optional[str]:
    """Read direction/outcome from the ``mat-icon.gv-call`` class list."""
    try:
        icon = item.locator("mat-icon.gv-call").first
        if icon.count() == 0:
            return None
        classes = (icon.get_attribute("class") or "").split()
        if "gv-call" not in classes:
            return None
        idx = classes.index("gv-call")
        tail = classes[idx + 1 : idx + 4]
        return "_".join(tail) if tail else "unknown"
    except Exception:
        return None


def _is_suspected_spam_row(item) -> bool:
    try:
        if item.locator(".suspected-spam").count() > 0:
            return True
    except Exception:
        pass
    try:
        if "suspected spam" in item.inner_text(timeout=1500).lower():
            return True
    except Exception:
        pass
    return False


def _parse_aria_description(aria: str) -> tuple[Optional[str], Optional[datetime]]:
    """Parse GV's screen-reader string for participant hint + timestamp."""
    if not aria:
        return None, None

    received_at: Optional[datetime] = None
    m = _ARIA_DATE_RE.search(aria)
    if m:
        received_at = _parse_timestamp(m.group(0))

    participant_hint = aria
    for prefix in (
        "Suspected spam · ",
        "Suspected spam. ",
        "Suspected spam call from ",
        "Missed call from ",
        "Incoming call from ",
        "Outgoing call to ",
        "Answered call from ",
    ):
        if participant_hint.lower().startswith(prefix.lower()):
            participant_hint = participant_hint[len(prefix) :]
            break

    if "." in participant_hint:
        participant_hint = participant_hint.split(".", 1)[0].strip()

    return participant_hint.strip() or None, received_at


def _extract_call_rows(page, sel: str) -> list[dict]:
    """Snapshot every call row in the viewport (spam filter happens later)."""
    out: list[dict] = []
    items = page.locator(sel)
    for i in range(items.count()):
        item = items.nth(i)
        suspected = _is_suspected_spam_row(item)

        participant = _try_text(item, [
            "gv-annotation.participants",
            "gv-annotation.participant",
            "[class*='participant']",
        ])
        aria = _try_text(item, [
            "span.cdk-visually-hidden",
            ".cdk-visually-hidden",
        ])
        aria_participant, aria_ts = _parse_aria_description(aria)

        if not participant and aria_participant:
            participant = aria_participant

        timestamp_text = _try_text(item, [
            ".timestamp",
            "[class*='timestamp']",
            "time",
        ])
        duration_text = _try_text(item, [
            ".duration",
            "[class*='duration']",
        ])

        received_at = _parse_timestamp(timestamp_text) or aria_ts
        if received_at is None and aria:
            received_at = _parse_timestamp(aria)

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
                "timestamp_text": timestamp_text or aria,
                "received_at": received_at,
                "duration_text": duration_text,
                "call_type": _parse_call_type(item),
                "aria_description": aria or None,
                "gv_suspected_spam": suspected,
            }
        )
    return out


def _call_row_key(row: dict) -> str:
    if row.get("data_id"):
        return f"call:id:{row['data_id']}"
    return "|".join([
        "call",
        (row.get("participant") or "").strip(),
        (row.get("timestamp_text") or "").strip(),
        (row.get("call_type") or "").strip(),
    ])


def _call_type_label(call_type: Optional[str]) -> str:
    if not call_type:
        return "phone call"
    return call_type.replace("_", " ")


def _build_suspected_spam_defaults(row: dict, *, now: datetime) -> dict:
    number = _normalize_phone(row.get("participant")) or _normalize_phone(
        row.get("aria_description")
    )
    display_name = None if number else (row.get("participant") or None)
    call_label = _call_type_label(row.get("call_type"))
    received_at = row.get("received_at")
    when = (
        received_at.strftime("%Y-%m-%d %I:%M %p")
        if received_at
        else "an unknown date/time"
    )

    if number:
        caller_bit = f"from {number}"
    elif display_name:
        caller_bit = f"from {display_name} (contact name; number unknown)"
    else:
        caller_bit = "from an unknown number"

    summary = f"Google Voice suspected spam — {call_label} {caller_bit.strip()}."
    comment = (
        f"I received a {call_label} {caller_bit} on {when}. "
        "Google Voice flagged this caller as suspected spam. "
        "There is no voicemail transcript for this call."
    )
    subject_id, subject_text = map_scam_category("other")

    return {
        "record_type": RECORD_CALL,
        "gv_suspected_spam": True,
        "call_type": row.get("call_type"),
        "caller_number": number,
        "caller_display_name": display_name,
        "received_at": received_at,
        "duration_sec": _parse_duration(row.get("duration_text")),
        "transcript": None,
        "is_spam": True,
        "confidence": 1.0,
        "scam_category": "other",
        "ftc_subject_id": subject_id,
        "ftc_subject_text": subject_text,
        "summary": summary,
        "comment_text": comment[:1000],
        "should_report": True,
        "status": STATUS_APPROVED,
        "classified_at": now,
        "reviewed_at": now,
    }


def _build_crossmatch_defaults(row: dict, *, now: datetime) -> dict:
    """Defaults for a call whose number matches a proven-spam voicemail."""
    number = _normalize_phone(row.get("participant")) or _normalize_phone(
        row.get("aria_description")
    )
    display_name = None if number else (row.get("participant") or None)
    call_label = _call_type_label(row.get("call_type"))
    received_at = row.get("received_at")
    when = (
        received_at.strftime("%Y-%m-%d %I:%M %p")
        if received_at
        else "an unknown date/time"
    )

    if number:
        caller_bit = f"from {number}"
    elif display_name:
        caller_bit = f"from {display_name} (contact name; number unknown)"
    else:
        caller_bit = "from an unknown number"

    summary = (
        f"Cross-matched known spam caller — {call_label} {caller_bit.strip()}."
    )
    comment = (
        f"I received a {call_label} {caller_bit} on {when}. "
        "This caller was previously identified as spam from an earlier "
        "voicemail on my Google Voice line. Google Voice did not flag this "
        "specific call as suspected spam. There is no voicemail transcript "
        "for this call."
    )
    subject_id, subject_text = map_scam_category("other")

    return {
        "record_type": RECORD_CALL,
        "gv_suspected_spam": False,
        "call_type": row.get("call_type"),
        "caller_number": number,
        "caller_display_name": display_name,
        "received_at": received_at,
        "duration_sec": _parse_duration(row.get("duration_text")),
        "transcript": None,
        "is_spam": True,
        "confidence": 0.95,
        "scam_category": "other",
        "ftc_subject_id": subject_id,
        "ftc_subject_text": subject_text,
        "summary": summary,
        "comment_text": comment[:1000],
        "should_report": True,
        "status": STATUS_APPROVED,
        "classified_at": now,
        "reviewed_at": now,
    }


def _source_msg_id_for_call(row: dict) -> str:
    if row.get("data_id"):
        return f"gv:call:{row['data_id']}"
    key_basis = "|".join([
        (row.get("participant") or "").strip(),
        (row.get("timestamp_text") or "").strip(),
        (row.get("call_type") or "").strip(),
    ])
    digest = hashlib.sha1(key_basis.encode("utf-8")).hexdigest()[:16]
    return f"gv:call:hash:{digest}"


def _ingest_call_rows(
    rows: list[dict],
    *,
    build_defaults,
    now: datetime,
    since_cutoff: Optional[datetime],
) -> tuple[int, int]:
    """Insert call rows; returns (inserted, skipped_old)."""
    inserted = 0
    skipped_old = 0
    for row in rows:
        received_at = row.get("received_at")
        if since_cutoff is not None and received_at is not None and received_at < since_cutoff:
            skipped_old += 1
            continue

        defaults = build_defaults(row, now=now)
        with session_scope() as session:
            _, created = upsert_voicemail(
                session,
                source=SOURCE_PLAYWRIGHT,
                source_msg_id=_source_msg_id_for_call(row),
                defaults=defaults,
            )
            if created:
                inserted += 1
    return inserted, skipped_old


def scrape_call_history(
    cfg: AppConfig,
    *,
    limit: Optional[int] = None,
    since_days: Optional[int] = None,
) -> int:
    """Scrape GV call history for suspected-spam + cross-matched calls."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is not installed. Run `pip install -r requirements.txt`."
        ) from exc

    storage_state_path = cfg.resolve_path(cfg.google_voice.storage_state_path)
    init_db(cfg.resolve_path(cfg.database.path))

    with session_scope() as session:
        proven_digits = _load_proven_spam_digits(session)
    log.info("Loaded %d proven-spam number(s) for cross-match.", len(proven_digits))

    cap = limit
    since_cutoff: Optional[datetime] = None
    if since_days is not None and since_days > 0:
        since_cutoff = datetime.now() - timedelta(days=since_days)

    inserted = 0
    skipped_old = 0
    gv_inserted = 0
    cm_inserted = 0
    now = datetime.utcnow()

    with sync_playwright() as pw:
        context, _browser = _open_gv_browser(pw, storage_state_path)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(CALLS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        try:
            threads, _sel = _scrape_thread_list(
                page,
                max_threads=None,
                since_days=since_days,
                thread_selectors=CALL_THREAD_SELECTORS,
                extract_rows=_extract_call_rows,
                row_key_fn=_call_row_key,
                list_label="call",
            )
        except Exception as exc:
            log.error("Call-history scrape failed: %s. Saving debug screenshot.", exc)
            debug_path = cfg.resolve_path("submissions") / "gv_calls_debug.png"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(debug_path), full_page=True)
                log.error("Debug screenshot: %s", debug_path)
                log.error("Current URL: %s", page.url)
            except Exception:
                pass
            context.close()
            raise

        gv_spam_threads: list[dict] = []
        crossmatch_threads: list[dict] = []
        for t in threads:
            if t.get("gv_suspected_spam"):
                gv_spam_threads.append(t)
                continue
            number = _normalize_phone(t.get("participant")) or _normalize_phone(
                t.get("aria_description")
            )
            digits = _caller_digits(number)
            if digits and digits in proven_digits:
                crossmatch_threads.append(t)

        if cap is not None:
            gv_spam_threads = gv_spam_threads[:cap]
            crossmatch_threads = crossmatch_threads[:cap]

        log.info(
            "Scrolled %d total call(s); %d GV suspected spam, "
            "%d cross-matched known-spam caller(s).",
            len(threads),
            len(gv_spam_threads),
            len(crossmatch_threads),
        )

        gv_inserted, gv_skipped = _ingest_call_rows(
            gv_spam_threads,
            build_defaults=_build_suspected_spam_defaults,
            now=now,
            since_cutoff=since_cutoff,
        )
        cm_inserted, cm_skipped = _ingest_call_rows(
            crossmatch_threads,
            build_defaults=_build_crossmatch_defaults,
            now=now,
            since_cutoff=since_cutoff,
        )
        inserted = gv_inserted + cm_inserted
        skipped_old = gv_skipped + cm_skipped

        context.close()

    log.info(
        "Call-history ingest complete. %d GV spam + %d cross-match new row(s)%s.",
        gv_inserted,
        cm_inserted,
        f", {skipped_old} skipped (older than {since_days} days)" if skipped_old else "",
    )
    return inserted
