"""Command-line entry point.

Run as a module from the project root:

    python -m ftc_automation login
    python -m ftc_automation ingest --backlog
    python -m ftc_automation ingest --calls
    python -m ftc_automation ingest --gmail
    python -m ftc_automation classify [--limit N] [--reclassify]
    python -m ftc_automation pipeline [--since-days N]
    python -m ftc_automation approve [--min-confidence 0] [--retry-failed]
    python -m ftc_automation review
    python -m ftc_automation submit [--once] [--limit N]
    python -m ftc_automation status
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Iterable, Optional

from .ftc.config import AppConfig, load_config


log = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _cmd_login(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.ingest.gv_playwright import interactive_login

    interactive_login(cfg.resolve_path(cfg.google_voice.storage_state_path))
    return 0


def _cmd_ingest(cfg: AppConfig, args: argparse.Namespace) -> int:
    did_anything = False
    if args.backlog:
        from .ftc.ingest.gv_playwright import scrape_backlog

        scrape_backlog(cfg, limit=args.limit, since_days=args.since_days)
        did_anything = True
    if args.calls:
        from .ftc.ingest.gv_call_history import scrape_call_history

        scrape_call_history(cfg, limit=args.limit, since_days=args.since_days)
        did_anything = True
    if args.gmail:
        from .ftc.ingest.gmail_watcher import ingest_gmail

        ingest_gmail(cfg, max_messages=args.limit or 500)
        did_anything = True
    if not did_anything:
        log.error("ingest: choose at least one of --backlog, --calls, or --gmail")
        return 2
    return 0


def _cmd_classify(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.classify.openai_classifier import classify_pending

    classify_pending(
        cfg,
        limit=args.limit,
        reclassify=args.reclassify,
        submit=False if args.no_submit else None,
    )
    return 0


def _cmd_pipeline(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.pipeline import run_pipeline

    if args.no_submit:
        cfg.review.auto_submit = False
    run_pipeline(
        cfg,
        since_days=args.since_days,
        limit=args.limit,
        voicemail=not args.calls_only,
        calls=not args.voicemail_only,
        submit=not args.no_submit,
    )
    return 0


def _cmd_approve(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.review.approve import approve_pending

    count = approve_pending(
        cfg,
        min_confidence=args.min_confidence,
        retry_failed=args.retry_failed,
        limit=args.limit,
    )
    log.info("Approved %d voicemail(s) for FTC submission.", count)
    return 0


def _cmd_review(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.review.app import create_app

    app = create_app(cfg)
    host = args.host or cfg.review.host
    port = args.port or cfg.review.port
    log.info("Starting review UI on http://%s:%s", host, port)
    app.run(host=host, port=port, debug=False)
    return 0


def _cmd_submit(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.submit.ftc_playwright import submit_approved

    count = submit_approved(cfg, once=args.once, limit=args.limit)
    if count < 0:
        return 1
    return 0


def _cmd_rescrape_audio(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .ftc.ingest.gv_audio import rescrape_audio

    rescrape_audio(cfg, only_spam=not args.all, limit=args.limit)
    return 0


def _cmd_status(cfg: AppConfig, args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from .ftc.db import Voicemail, init_db, session_scope

    init_db(cfg.resolve_path(cfg.database.path))
    with session_scope() as session:
        rows = session.execute(
            select(Voicemail.status, func.count())
            .group_by(Voicemail.status)
            .order_by(Voicemail.status)
        ).all()
        total = sum(c for _, c in rows)
        print(f"{'STATUS':<20} {'COUNT':>8}")
        print("-" * 30)
        for status, count in rows:
            print(f"{status:<20} {count:>8}")
        print("-" * 30)
        print(f"{'TOTAL':<20} {total:>8}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ftc_automation")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="Interactive Google Voice login (saves storage_state.json)")

    p_ing = sub.add_parser("ingest", help="Ingest voicemails / calls into the DB")
    p_ing.add_argument("--backlog", action="store_true", help="Playwright sweep of GV voicemail tab")
    p_ing.add_argument(
        "--calls",
        action="store_true",
        help="Playwright sweep of GV call history (GV spam + cross-matched known spam callers)",
    )
    p_ing.add_argument("--gmail", action="store_true", help="Pull forwarded VM emails from Gmail")
    p_ing.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap how many threads/emails to ingest this run (good for smoke tests).",
    )
    p_ing.add_argument(
        "--since-days",
        type=int,
        default=None,
        metavar="N",
        help="Only ingest items from the last N days (GV Playwright scrapes).",
    )

    p_cls = sub.add_parser("classify", help="Run Bedrock (Nova Micro) classifier over pending voicemails")
    p_cls.add_argument("--limit", type=int, default=None)
    p_cls.add_argument("--reclassify", action="store_true", help="Also re-run on already-classified rows")
    p_cls.add_argument(
        "--no-submit",
        action="store_true",
        help="Skip auto-submit after classification (default: submit approved spam)",
    )

    p_pipe = sub.add_parser(
        "pipeline",
        help="Ingest since last run, classify, and auto-submit spam (voicemail + call history)",
    )
    p_pipe.add_argument(
        "--since-days",
        type=int,
        default=None,
        metavar="N",
        help="Ingest window in days (default: since last pipeline run)",
    )
    p_pipe.add_argument("--limit", type=int, default=None)
    p_pipe.add_argument("--no-submit", action="store_true", help="Classify only; do not file with FTC")
    p_pipe.add_argument("--voicemail-only", action="store_true", help="Skip call-history ingest")
    p_pipe.add_argument("--calls-only", action="store_true", help="Skip voicemail backlog ingest")

    p_apr = sub.add_parser(
        "approve",
        help="Bulk-approve classified spam voicemails for FTC submission",
    )
    p_apr.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Only approve rows at or above this confidence (default: 0 = all spam)",
    )
    p_apr.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also re-queue submit_failed rows that have a caller number",
    )
    p_apr.add_argument("--limit", type=int, default=None)

    p_rev = sub.add_parser("review", help="Launch Flask review UI")
    p_rev.add_argument("--host", default=None)
    p_rev.add_argument("--port", type=int, default=None)

    p_sub = sub.add_parser("submit", help="Submit approved voicemails to donotcall.gov")
    p_sub.add_argument("--once", action="store_true", help="Drain queue once and exit (default loops)")
    p_sub.add_argument("--limit", type=int, default=None)

    p_aud = sub.add_parser(
        "rescrape-audio",
        help="One-time pass to download voicemail audio files via Playwright",
    )
    p_aud.add_argument(
        "--all",
        action="store_true",
        help="Fetch audio for every row missing it (default: only spam-flagged classified rows)",
    )
    p_aud.add_argument("--limit", type=int, default=None, help="Cap how many rows to process")

    sub.add_parser("status", help="Print pipeline counts")

    return parser


_DISPATCH = {
    "login": _cmd_login,
    "ingest": _cmd_ingest,
    "classify": _cmd_classify,
    "pipeline": _cmd_pipeline,
    "approve": _cmd_approve,
    "review": _cmd_review,
    "submit": _cmd_submit,
    "rescrape-audio": _cmd_rescrape_audio,
    "status": _cmd_status,
}


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _configure_logging(args.verbose)
    cfg = load_config()
    handler = _DISPATCH[args.cmd]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
