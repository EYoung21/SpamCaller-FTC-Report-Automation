"""Programmatic bulk approval for the review queue."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, select

from ..config import AppConfig
from ..db import (
    STATUS_APPROVED,
    STATUS_CLASSIFIED,
    STATUS_SUBMIT_FAILED,
    Voicemail,
    init_db,
    session_scope,
)


def approve_pending(
    cfg: AppConfig,
    *,
    min_confidence: float = 0.0,
    retry_failed: bool = False,
    limit: int | None = None,
) -> int:
    """Move classified spam rows (and optionally failed rows) to ``approved``.

    Returns the number of voicemails approved this run.
    """
    init_db(cfg.resolve_path(cfg.database.path))
    now = datetime.utcnow()
    approved = 0

    with session_scope() as session:
        q = select(Voicemail).where(
            and_(
                Voicemail.status == STATUS_CLASSIFIED,
                Voicemail.is_spam.is_(True),
                Voicemail.should_report.is_(True),
                Voicemail.confidence >= min_confidence,
            )
        ).order_by(Voicemail.received_at.asc())
        if limit is not None:
            q = q.limit(limit)

        for vm in session.execute(q).scalars():
            vm.status = STATUS_APPROVED
            vm.reviewed_at = now
            approved += 1

        if retry_failed:
            remaining = None if limit is None else max(0, limit - approved)
            fq = (
                select(Voicemail)
                .where(
                    Voicemail.status == STATUS_SUBMIT_FAILED,
                    Voicemail.is_spam.is_(True),
                    Voicemail.caller_number.isnot(None),
                    Voicemail.caller_number != "",
                )
                .order_by(Voicemail.received_at.asc())
            )
            if remaining is not None:
                fq = fq.limit(remaining)
            for vm in session.execute(fq).scalars():
                vm.status = STATUS_APPROVED
                vm.reviewed_at = now
                approved += 1

    return approved
