"""Flask web app for human review of classified voicemails.

Routes:
- ``GET  /``                  -> redirect to /queue
- ``GET  /queue``             -> list of classified spam voicemails to review
- ``GET  /vm/<id>``           -> single-VM review page (with edit form)
- ``POST /vm/<id>``           -> save edits + transition status
- ``POST /queue/bulk_approve`` -> approve all high-confidence rows
- ``GET  /audio/<id>``        -> 302 redirect to audio_url (if any)

Keyboard shortcuts on /vm/<id>: J (next), K (prev), A (approve), R (reject), S (skip).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Optional

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import and_, asc, desc, select

from ..config import AppConfig, load_config
from ..db import (
    STATUS_APPROVED,
    STATUS_CLASSIFIED,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    Voicemail,
    init_db,
    session_scope,
)
from ..classify.ftc_mapping import (
    SCAM_CATEGORIES,
    SUBJECT_ID_LABELS,
    map_scam_category,
)


log = logging.getLogger(__name__)


def _queue_query():
    return (
        select(Voicemail)
        .where(
            Voicemail.status == STATUS_CLASSIFIED,
            Voicemail.is_spam.is_(True),
        )
        .order_by(desc(Voicemail.confidence), desc(Voicemail.received_at))
    )


def _neighbors(session, vm_id: int) -> tuple[Optional[int], Optional[int]]:
    """Return (prev_id, next_id) within the current review queue."""
    ids = [row.id for row in session.execute(_queue_query()).scalars()]
    if vm_id not in ids:
        return None, None
    idx = ids.index(vm_id)
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx + 1 < len(ids) else None
    return prev_id, next_id


def create_app(cfg: Optional[AppConfig] = None) -> Flask:
    cfg = cfg or load_config()
    init_db(cfg.resolve_path(cfg.database.path))

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "ftc-review-local"  # local-only UI
    app.config["APP_CFG"] = cfg

    @app.route("/")
    def index():
        return redirect(url_for("queue"))

    @app.route("/queue")
    def queue():
        with session_scope() as session:
            rows = list(session.execute(_queue_query()).scalars())

            # Counters for the header strip
            counts = {}
            for status in [
                STATUS_CLASSIFIED,
                STATUS_APPROVED,
                STATUS_REJECTED,
                STATUS_SKIPPED,
            ]:
                counts[status] = session.execute(
                    select(Voicemail.id).where(Voicemail.status == status)
                ).all().__len__()

            return render_template(
                "queue.html",
                voicemails=rows,
                counts=counts,
                bulk_min=cfg.review.bulk_approve_min_confidence,
            )

    @app.route("/vm/<int:vm_id>", methods=["GET"])
    def view_vm(vm_id: int):
        with session_scope() as session:
            vm = session.get(Voicemail, vm_id)
            if vm is None:
                abort(404)
            prev_id, next_id = _neighbors(session, vm_id)
            return render_template(
                "review.html",
                vm=vm,
                prev_id=prev_id,
                next_id=next_id,
                scam_categories=SCAM_CATEGORIES,
                subject_labels=SUBJECT_ID_LABELS,
            )

    @app.route("/vm/<int:vm_id>", methods=["POST"])
    def update_vm(vm_id: int):
        action = request.form.get("action", "save")
        with session_scope() as session:
            vm = session.get(Voicemail, vm_id)
            if vm is None:
                abort(404)

            # Persist edits regardless of action.
            vm.caller_number = (request.form.get("caller_number") or "").strip() or None
            vm.callback_number = (request.form.get("callback_number") or "").strip() or None
            vm.claimed_company = (request.form.get("claimed_company") or "").strip() or None
            new_cat = (request.form.get("scam_category") or "unknown").strip()
            vm.scam_category = new_cat
            subject_id, free_text = map_scam_category(new_cat)
            vm.ftc_subject_id = subject_id
            vm.ftc_subject_text = (
                request.form.get("ftc_subject_text", "").strip() or free_text
            )
            vm.comment_text = (request.form.get("comment_text") or "").strip() or None

            now = datetime.utcnow()
            if action == "approve":
                vm.status = STATUS_APPROVED
                vm.reviewed_at = now
                flash(f"VM #{vm.id} approved.", "success")
            elif action == "reject":
                vm.status = STATUS_REJECTED
                vm.reviewed_at = now
                flash(f"VM #{vm.id} rejected (not spam).", "info")
            elif action == "skip":
                vm.status = STATUS_SKIPPED
                vm.reviewed_at = now
                flash(f"VM #{vm.id} skipped.", "info")
            else:
                flash(f"Saved edits on VM #{vm.id}.", "success")

            _, next_id = _neighbors(session, vm_id)

        if action in {"approve", "reject", "skip"} and next_id:
            return redirect(url_for("view_vm", vm_id=next_id))
        return redirect(url_for("queue"))

    @app.route("/queue/bulk_approve", methods=["POST"])
    def bulk_approve():
        min_conf = float(
            request.form.get("min_confidence", cfg.review.bulk_approve_min_confidence)
        )
        approved = 0
        with session_scope() as session:
            rows = session.execute(
                select(Voicemail).where(
                    and_(
                        Voicemail.status == STATUS_CLASSIFIED,
                        Voicemail.is_spam.is_(True),
                        Voicemail.should_report.is_(True),
                        Voicemail.confidence >= min_conf,
                    )
                )
            ).scalars()
            now = datetime.utcnow()
            for vm in rows:
                vm.status = STATUS_APPROVED
                vm.reviewed_at = now
                approved += 1
        flash(f"Bulk-approved {approved} high-confidence voicemail(s).", "success")
        return redirect(url_for("queue"))

    @app.route("/audio/<int:vm_id>")
    def audio(vm_id: int):
        with session_scope() as session:
            vm = session.get(Voicemail, vm_id)
            if vm is None or not vm.audio_url:
                abort(404)
            return redirect(vm.audio_url)

    @app.template_filter("fmt_dt")
    def fmt_dt(value):
        if not value:
            return "—"
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M")
        return str(value)

    @app.template_filter("fmt_dur")
    def fmt_dur(value):
        if value is None:
            return "—"
        try:
            secs = int(value)
        except (TypeError, ValueError):
            return str(value)
        m, s = divmod(secs, 60)
        return f"{m}:{s:02d}"

    return app


def run(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    app = create_app(cfg)
    app.run(host=cfg.review.host, port=cfg.review.port, debug=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(run())
