"""Audit: unreported spam, dedup coverage, ingest gaps."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from ftc_automation.ftc.config import load_config  # noqa: E402
from ftc_automation.ftc.db import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_CLASSIFIED,
    STATUS_DEDUPLICATED,
    STATUS_SKIPPED,
    STATUS_SUBMIT_FAILED,
    STATUS_SUBMITTED,
    Voicemail,
    init_db,
    session_scope,
)


def _digits(num: str | None) -> str:
    if not num:
        return ""
    return "".join(ch for ch in num if ch.isdigit())


def main() -> int:
    cfg = load_config()
    init_db(cfg.resolve_path(cfg.database.path))

    with session_scope() as s:
        print("=== YES: ALL SUBMITTED REPORTS ARE LOGGED IN SQLite ===")
        submitted = s.query(func.count(Voicemail.id)).filter(
            Voicemail.status == STATUS_SUBMITTED
        ).scalar()
        with_screenshot = s.query(func.count(Voicemail.id)).filter(
            Voicemail.status == STATUS_SUBMITTED,
            Voicemail.submit_screenshot.isnot(None),
        ).scalar()
        with_submitted_at = s.query(func.count(Voicemail.id)).filter(
            Voicemail.status == STATUS_SUBMITTED,
            Voicemail.submitted_at.isnot(None),
        ).scalar()
        print(f"  status=submitted rows:        {submitted}")
        print(f"  with submitted_at timestamp:  {with_submitted_at}")
        print(f"  with submit_screenshot:       {with_screenshot}")
        print(
            "  Dedup at submit time loads ALL status=submitted caller numbers "
            "from this DB (_load_reported_caller_ids)."
        )
        print()

        print("=== SPAM IN DB — NOT YET FILED WITH FTC ===")
        for status, cnt in (
            s.query(Voicemail.status, func.count(Voicemail.id))
            .filter(Voicemail.is_spam.is_(True))
            .filter(Voicemail.status.notin_([STATUS_SUBMITTED, STATUS_DEDUPLICATED]))
            .group_by(Voicemail.status)
            .order_by(func.count(Voicemail.id).desc())
            .all()
        ):
            print(f"  {status:<15} {cnt}")

        print()
        print("=== UNIQUE SPAM PHONE NUMBERS ===")
        all_spam_nums = {
            _digits(r[0])
            for r in s.query(Voicemail.caller_number)
            .filter(Voicemail.is_spam.is_(True))
            .filter(Voicemail.caller_number.isnot(None))
            .all()
            if _digits(r[0])
        }
        submitted_nums = {
            _digits(r[0])
            for r in s.query(Voicemail.caller_number)
            .filter(Voicemail.status == STATUS_SUBMITTED)
            .filter(Voicemail.caller_number.isnot(None))
            .all()
            if _digits(r[0])
        }
        never_submitted = all_spam_nums - submitted_nums
        print(f"  Unique spam numbers in DB:              {len(all_spam_nums)}")
        print(f"  Unique numbers with submitted report:   {len(submitted_nums)}")
        print(f"  Unique spam numbers NEVER submitted:    {len(never_submitted)}")

        if never_submitted:
            print()
            print("--- Never-submitted spam numbers (one row each) ---")
            for digits in sorted(never_submitted):
                vm = (
                    s.query(Voicemail)
                    .filter(Voicemail.is_spam.is_(True))
                    .filter(Voicemail.caller_number.like(f"%{digits[-10:]}%"))
                    .order_by(Voicemail.received_at.desc())
                    .first()
                )
                if vm:
                    print(
                        f"  VM {vm.id:4d}  {vm.caller_number or digits:16s}  "
                        f"status={vm.status:<12} conf={vm.confidence}"
                    )

        print()
        print("=== SUBMIT_FAILED (retries blocked if same number submitted) ===")
        for vm in s.query(Voicemail).filter(
            Voicemail.status == STATUS_SUBMIT_FAILED
        ).order_by(Voicemail.id):
            prior = (
                s.query(Voicemail.id)
                .filter(
                    Voicemail.caller_number == vm.caller_number,
                    Voicemail.status == STATUS_SUBMITTED,
                )
                .first()
            )
            print(
                f"  VM {vm.id}  {vm.caller_number}  "
                f"already_submitted_as={prior[0] if prior else 'NO'}"
            )

        print()
        print("=== APPROVED (queued for next submit) ===")
        approved = s.query(Voicemail).filter(
            Voicemail.status == STATUS_APPROVED
        ).all()
        if not approved:
            print("  (none)")
        for vm in approved:
            prior = (
                s.query(Voicemail.id)
                .filter(
                    Voicemail.caller_number == vm.caller_number,
                    Voicemail.status == STATUS_SUBMITTED,
                )
                .first()
            )
            print(
                f"  VM {vm.id}  {vm.caller_number}  "
                f"dup_of_submitted={prior[0] if prior else 'NO — will file'}"
            )

        print()
        high_conf_unapproved = (
            s.query(func.count(Voicemail.id))
            .filter(
                Voicemail.status == STATUS_CLASSIFIED,
                Voicemail.is_spam.is_(True),
                Voicemail.confidence >= 0.85,
            )
            .scalar()
        )
        print(
            f"=== HIGH-CONFIDENCE SPAM still classified (not approved): "
            f"{high_conf_unapproved} ==="
        )

        print()
        print("=== INGEST vs REPORT GAP (in DB only — not GV all-time) ===")
        total = s.query(func.count(Voicemail.id)).scalar()
        not_spam = s.query(func.count(Voicemail.id)).filter(
            Voicemail.is_spam.is_(False)
        ).scalar()
        spam_total = s.query(func.count(Voicemail.id)).filter(
            Voicemail.is_spam.is_(True)
        ).scalar()
        print(f"  Total voicemails ingested into DB:  {total}")
        print(f"  Classified as NOT spam:             {not_spam}")
        print(f"  Classified as spam:                 {spam_total}")
        print(f"  Submitted to FTC:                   {submitted}")
        print(f"  Deduped (repeat caller):            "
              f"{s.query(func.count(Voicemail.id)).filter(Voicemail.status == STATUS_DEDUPLICATED).scalar()}")
        print(f"  Skipped (manual defer):             "
              f"{s.query(func.count(Voicemail.id)).filter(Voicemail.status == STATUS_SKIPPED).scalar()}")

        oldest = s.query(func.min(Voicemail.received_at)).scalar()
        newest = s.query(func.max(Voicemail.received_at)).scalar()
        unclassified = (
            s.query(func.count(Voicemail.id))
            .filter(Voicemail.status == "new")
            .scalar()
        )
        no_num_spam = (
            s.query(func.count(Voicemail.id))
            .filter(Voicemail.is_spam.is_(True))
            .filter(
                (Voicemail.caller_number.is_(None)) | (Voicemail.caller_number == "")
            )
            .scalar()
        )
        print()
        print("=== DB DATE RANGE ===")
        print(f"  Oldest voicemail: {oldest}")
        print(f"  Newest voicemail: {newest}")
        print(f"  Unclassified (status=new): {unclassified}")
        print(f"  Spam rows with no caller number: {no_num_spam}")

        print()
        print("=== TO FIND MORE IN GOOGLE VOICE ALL-TIME ===")
        print(
            "  Run full backlog ingest (no --since-days). Compare 'X new added' "
            "to see if GV has threads not yet in DB. Command:"
        )
        print("    python -m ftc_automation ingest --backlog")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
