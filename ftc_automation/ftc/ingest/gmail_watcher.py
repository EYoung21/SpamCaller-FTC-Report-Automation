"""Gmail-based ingestion for Google Voice voicemails.

Prerequisites:

1. Enable Google Voice -> Settings -> Voicemail -> "Get email notifications"
   with "Include transcript". Google will then forward every new voicemail
   to your Gmail inbox as a message from ``voice-noreply@google.com``.

2. Create an OAuth client (Desktop app) in Google Cloud Console, download
   ``client_secret.json``, and put it at the path configured in
   ``gmail.client_secret_path``.

3. First run will pop a browser to perform the OAuth dance; subsequent
   runs reuse the cached token.

Run via the CLI:

    python -m ftc_automation ingest --gmail
"""

from __future__ import annotations

import base64
import logging
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Optional

from ..config import AppConfig, load_config
from ..db import (
    SOURCE_GMAIL,
    STATUS_NEW,
    init_db,
    session_scope,
    upsert_voicemail,
)


log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ---------------------------------------------------------------------------
# OAuth + service helpers
# ---------------------------------------------------------------------------

def _build_gmail_service(cfg: AppConfig):
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Google API libs missing. Run `pip install -r requirements.txt`."
        ) from exc

    token_path = cfg.resolve_path(cfg.gmail.token_path)
    client_secret_path = cfg.resolve_path(cfg.gmail.client_secret_path)

    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secret_path.exists():
                raise SystemExit(
                    f"Missing Gmail OAuth client secret at {client_secret_path}.\n"
                    f"Download a Desktop-app OAuth client JSON from Google Cloud "
                    f"Console and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret_path), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\(?(\d{3})\)?[\s\.\-]?(\d{3})[\s\.\-]?(\d{4})")
_TRANSCRIPT_RE = re.compile(
    r"Transcript[:\s]*\n+(.+?)\n\n", re.IGNORECASE | re.DOTALL
)
_TRANSCRIPT_FALLBACK_RE = re.compile(
    r"(?:said|message[:\s])(.+?)(?:\n\n|Listen to the message)",
    re.IGNORECASE | re.DOTALL,
)
_DURATION_RE = re.compile(r"\((\d+)\s*(?:seconds|secs?|min(?:utes?)?)\)", re.IGNORECASE)
_AUDIO_URL_RE = re.compile(
    r"https?://voice\.google\.com/[^\s\">]+", re.IGNORECASE
)


def _decode_part_body(part: dict) -> str:
    body = part.get("body", {})
    data = body.get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data.encode("utf-8") + b"==")
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return ""


def _collect_plain_text(payload: dict) -> str:
    """Walk a Gmail payload tree, concatenating any text/plain parts."""
    chunks: list[str] = []
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        chunks.append(_decode_part_body(payload))
    for sub in payload.get("parts", []) or []:
        chunks.append(_collect_plain_text(sub))
    if not chunks and mime.startswith("text/"):
        chunks.append(_decode_part_body(payload))
    return "\n".join(c for c in chunks if c)


def _normalize_phone(match: re.Match) -> str:
    return "+1" + match.group(1) + match.group(2) + match.group(3)


def parse_voicemail_email(body: str, subject: str) -> dict:
    """Extract structured voicemail fields from a forwarded GV email."""
    result: dict = {
        "caller_number": None,
        "caller_display_name": None,
        "transcript": None,
        "duration_sec": None,
        "audio_url": None,
    }

    # Subject lines look like: "New voicemail from John Doe at (858) 555-1234"
    #                       or: "New voicemail from (858) 555-1234"
    subj = subject or ""
    m = _NUMBER_RE.search(subj)
    if m:
        result["caller_number"] = _normalize_phone(m)
        before = subj[: m.start()].lower()
        name_m = re.search(r"from\s+(.+?)\s+at\s*$", before)
        if name_m:
            display = name_m.group(1).strip()
            if display and not _NUMBER_RE.fullmatch(display):
                result["caller_display_name"] = display.title()
    else:
        name_m = re.search(r"from\s+(.+?)$", subj, re.IGNORECASE)
        if name_m:
            display = name_m.group(1).strip()
            if display:
                result["caller_display_name"] = display.title()

    if not result["caller_number"]:
        m = _NUMBER_RE.search(body)
        if m:
            result["caller_number"] = _normalize_phone(m)

    tm = _TRANSCRIPT_RE.search(body)
    transcript = None
    if tm:
        transcript = tm.group(1).strip()
    else:
        tm = _TRANSCRIPT_FALLBACK_RE.search(body)
        if tm:
            transcript = tm.group(1).strip()
    if transcript:
        transcript = re.sub(r"\s+", " ", transcript).strip()
        result["transcript"] = transcript

    dm = _DURATION_RE.search(body)
    if dm:
        n = int(dm.group(1))
        unit = dm.group(0).lower()
        result["duration_sec"] = n * 60 if "min" in unit else n

    am = _AUDIO_URL_RE.search(body)
    if am:
        result["audio_url"] = am.group(0)

    return result


# ---------------------------------------------------------------------------
# Main ingest loop
# ---------------------------------------------------------------------------

def _list_message_ids(service, query: str, max_results: int = 500) -> list[str]:
    ids: list[str] = []
    page_token: Optional[str] = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=100)
            .execute()
        )
        for m in resp.get("messages", []) or []:
            ids.append(m["id"])
            if len(ids) >= max_results:
                return ids
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _fetch_message(service, msg_id: str) -> dict:
    return (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )


def _header(headers: list, name: str) -> str:
    name_l = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_l:
            return h.get("value", "")
    return ""


def ingest_gmail(cfg: AppConfig, *, max_messages: int = 500) -> int:
    init_db(cfg.resolve_path(cfg.database.path))
    service = _build_gmail_service(cfg)

    log.info("Querying Gmail: %s", cfg.gmail.query)
    msg_ids = _list_message_ids(service, cfg.gmail.query, max_results=max_messages)
    log.info("Found %d candidate message(s).", len(msg_ids))

    inserted = 0
    for msg_id in msg_ids:
        msg = _fetch_message(service, msg_id)
        payload = msg.get("payload", {})
        headers = payload.get("headers", []) or []
        subject = _header(headers, "Subject")
        date_hdr = _header(headers, "Date")

        body = _collect_plain_text(payload)
        parsed = parse_voicemail_email(body, subject)

        received_at: Optional[datetime] = None
        if date_hdr:
            try:
                received_at = parsedate_to_datetime(date_hdr)
                if received_at.tzinfo is not None:
                    received_at = received_at.astimezone(timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError):
                received_at = None

        defaults = {
            "caller_number": parsed["caller_number"],
            "caller_display_name": parsed["caller_display_name"],
            "received_at": received_at,
            "duration_sec": parsed["duration_sec"],
            "transcript": parsed["transcript"],
            "audio_url": parsed["audio_url"],
            "status": STATUS_NEW,
        }

        with session_scope() as session:
            _, created = upsert_voicemail(
                session,
                source=SOURCE_GMAIL,
                source_msg_id=msg_id,
                defaults=defaults,
            )
            if created:
                inserted += 1

    log.info("Gmail ingest complete. %d new voicemail(s).", inserted)
    return inserted


def run(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    ingest_gmail(cfg)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
