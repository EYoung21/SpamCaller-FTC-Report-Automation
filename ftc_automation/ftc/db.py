"""SQLite schema and ORM model for voicemails.

A single ``voicemails`` table tracks each voicemail across the entire
pipeline: ingestion -> classification -> human review -> FTC submission.
``source_msg_id`` is UNIQUE so re-running ingest jobs is a no-op for
duplicates and each stage can be re-run idempotently.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)


# ---------------------------------------------------------------------------
# Workflow status constants. Strings so we can read them straight from
# SQLite in ad-hoc queries.
# ---------------------------------------------------------------------------

STATUS_NEW = "new"                    # ingested, not classified yet
STATUS_CLASSIFIED = "classified"      # LLM has run, awaiting review
STATUS_APPROVED = "approved"          # human approved, queued to submit
STATUS_REJECTED = "rejected"          # human said "not actually spam"
STATUS_SKIPPED = "skipped"            # human deferred for later
STATUS_SUBMITTED = "submitted"        # successfully filed with FTC
STATUS_SUBMIT_FAILED = "submit_failed"
STATUS_DEDUPLICATED = "deduplicated"  # skipped — same caller already reported

ALL_STATUSES = {
    STATUS_NEW,
    STATUS_CLASSIFIED,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    STATUS_SUBMITTED,
    STATUS_SUBMIT_FAILED,
    STATUS_DEDUPLICATED,
}

SOURCE_PLAYWRIGHT = "playwright"
SOURCE_GMAIL = "gmail"


class Base(DeclarativeBase):
    pass


class Voicemail(Base):
    __tablename__ = "voicemails"
    __table_args__ = (
        UniqueConstraint("source", "source_msg_id", name="uq_voicemails_source_msg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Provenance
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_msg_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Raw voicemail facts
    caller_number: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    caller_display_name: Mapped[Optional[str]] = mapped_column(String(255))
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer)
    transcript: Mapped[Optional[str]] = mapped_column(Text)
    audio_url: Mapped[Optional[str]] = mapped_column(Text)

    # LLM output (filled by classify step)
    is_spam: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    callback_number: Mapped[Optional[str]] = mapped_column(String(32))
    claimed_company: Mapped[Optional[str]] = mapped_column(String(255))
    scam_category: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    ftc_subject_id: Mapped[Optional[int]] = mapped_column(Integer)
    ftc_subject_text: Mapped[Optional[str]] = mapped_column(String(255))
    summary: Mapped[Optional[str]] = mapped_column(Text)
    comment_text: Mapped[Optional[str]] = mapped_column(Text)
    should_report: Mapped[Optional[bool]] = mapped_column(Boolean)

    # Workflow
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_NEW, index=True)
    classified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    submit_error: Mapped[Optional[str]] = mapped_column(Text)
    submit_screenshot: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<Voicemail id={self.id} caller={self.caller_number} "
            f"status={self.status} is_spam={self.is_spam}>"
        )


# ---------------------------------------------------------------------------
# Engine + session helpers
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal: Optional[sessionmaker[Session]] = None
_current_db_path: Optional[Path] = None


def _build_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True, echo=False)

    # Sensible SQLite pragmas: WAL for concurrent reads, FK enforcement.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(db_path: Path | str) -> None:
    """Initialise engine + create tables for the given SQLite path."""
    global _engine, _SessionLocal, _current_db_path

    db_path = Path(db_path)
    if _engine is not None and _current_db_path == db_path:
        return

    _engine = _build_engine(db_path)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    _current_db_path = db_path
    Base.metadata.create_all(_engine)


def get_session() -> Session:
    """Return a new SQLAlchemy session. Call :func:`init_db` first."""
    if _SessionLocal is None:
        raise RuntimeError("init_db() must be called before get_session()")
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager that commits on success and rolls back on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Convenience helpers used by ingest jobs
# ---------------------------------------------------------------------------

def upsert_voicemail(
    session: Session,
    *,
    source: str,
    source_msg_id: str,
    defaults: Optional[dict] = None,
) -> tuple[Voicemail, bool]:
    """Insert a voicemail keyed on ``(source, source_msg_id)``.

    Returns ``(voicemail, created)``. If a row already exists, missing
    fields on it are filled in from ``defaults`` but existing values are
    preserved (so reclassification or re-review aren't clobbered by a
    repeat ingest).

    Also matches existing rows by ``(source, caller_number, received_at)``
    as a fallback — this handles the case where the source_msg_id scheme
    changed between ingest runs (e.g. fixing a bug) but the underlying
    voicemail is the same. Without this, the second ingest would create
    a phantom duplicate.
    """
    existing = (
        session.query(Voicemail)
        .filter(Voicemail.source == source, Voicemail.source_msg_id == source_msg_id)
        .one_or_none()
    )

    # Fallback dedupe: same source + same caller + same received_at.
    if existing is None and defaults:
        nat_number = defaults.get("caller_number")
        nat_received = defaults.get("received_at")
        nat_name = defaults.get("caller_display_name")
        nat_transcript = (defaults.get("transcript") or "")[:40]
        if nat_received is not None:
            q = session.query(Voicemail).filter(
                Voicemail.source == source,
                Voicemail.received_at == nat_received,
            )
            if nat_number:
                q = q.filter(Voicemail.caller_number == nat_number)
            elif nat_name:
                q = q.filter(Voicemail.caller_display_name == nat_name)
            elif nat_transcript:
                # Anonymous/unknown caller: fall back to first-40-char snippet
                # so we still catch duplicates of the same recording.
                pass

            candidates = q.all()
            if nat_transcript:
                # Among rows with the same received_at + caller, prefer one
                # whose transcript starts the same way.
                for c in candidates:
                    if (c.transcript or "")[:40] == nat_transcript:
                        existing = c
                        break
                else:
                    existing = candidates[0] if candidates else None
            else:
                existing = candidates[0] if candidates else None

            # Migrate the legacy source_msg_id over so future ingests match
            # on the primary key.
            if existing is not None and existing.source_msg_id != source_msg_id:
                existing.source_msg_id = source_msg_id

    if existing is not None:
        if defaults:
            changed = False
            for key, value in defaults.items():
                if value is None:
                    continue
                if getattr(existing, key, None) in (None, ""):
                    setattr(existing, key, value)
                    changed = True
            if changed:
                existing.updated_at = datetime.utcnow()
        return existing, False

    vm = Voicemail(source=source, source_msg_id=source_msg_id, **(defaults or {}))
    session.add(vm)
    session.flush()
    return vm, True
