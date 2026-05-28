"""Print a detailed snapshot of the review queue."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func  # noqa: E402

from ftc_automation.ftc.config import load_config  # noqa: E402
from ftc_automation.ftc.db import (  # noqa: E402
    Voicemail,
    init_db,
    session_scope,
)


def main() -> int:
    cfg = load_config()
    init_db(cfg.resolve_path(cfg.database.path))
    with session_scope() as s:
        print("=== Full status x is_spam matrix ===")
        rows = (
            s.query(Voicemail.status, Voicemail.is_spam, func.count(Voicemail.id))
            .group_by(Voicemail.status, Voicemail.is_spam)
            .order_by(Voicemail.status)
            .all()
        )
        for status, is_spam, count in rows:
            print(f"  status={status:<14} is_spam={str(is_spam):<5} : {count}")
        print()
        print("=== Review-queue summary ===")
        ready = (
            s.query(func.count(Voicemail.id))
            .filter(Voicemail.status == "classified", Voicemail.is_spam.is_(True))
            .scalar()
        )
        print(f"  Ready to triage (LLM said spam, untouched) : {ready}")
        approved = (
            s.query(func.count(Voicemail.id))
            .filter(Voicemail.status == "approved")
            .scalar()
        )
        print(f"  Approved & queued for FTC submission        : {approved}")
        submitted = (
            s.query(func.count(Voicemail.id))
            .filter(Voicemail.status == "submitted")
            .scalar()
        )
        print(f"  Already submitted to FTC                    : {submitted}")
        deduped = (
            s.query(func.count(Voicemail.id))
            .filter(Voicemail.status == "deduplicated")
            .scalar()
        )
        print(f"  Deduplicated (same caller already reported)   : {deduped}")
        print()
        print("=== Audio coverage of the ready-to-triage queue ===")
        with_audio = (
            s.query(func.count(Voicemail.id))
            .filter(
                Voicemail.status == "classified",
                Voicemail.is_spam.is_(True),
                Voicemail.audio_url.isnot(None),
            )
            .scalar()
        )
        without_audio = (
            s.query(func.count(Voicemail.id))
            .filter(
                Voicemail.status == "classified",
                Voicemail.is_spam.is_(True),
                Voicemail.audio_url.is_(None),
            )
            .scalar()
        )
        print(f"  with audio    : {with_audio}")
        print(f"  without audio : {without_audio}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
