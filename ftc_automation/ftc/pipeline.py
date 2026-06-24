"""End-to-end ingest → classify → submit pipeline."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .config import AppConfig
from .ingest.gv_call_history import scrape_call_history
from .ingest.gv_playwright import scrape_backlog


log = logging.getLogger(__name__)

LAST_RUN_REL = "logs/last_pipeline_run.txt"


def days_since_last_run(cfg: AppConfig, *, default: int = 14) -> int:
    """Days to pass to ``--since-days`` based on the last pipeline timestamp."""
    path = cfg.resolve_path(LAST_RUN_REL)
    if not path.exists():
        return default
    try:
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return default
    return max(1, (datetime.now() - last).days + 1)


def touch_last_run(cfg: AppConfig) -> None:
    path = cfg.resolve_path(LAST_RUN_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now().isoformat(), encoding="utf-8")


def run_pipeline(
    cfg: AppConfig,
    *,
    since_days: Optional[int] = None,
    limit: Optional[int] = None,
    voicemail: bool = True,
    calls: bool = True,
    submit: bool = True,
) -> dict[str, int]:
    """Ingest new items, classify voicemails, and submit approved spam."""
    window = since_days if since_days is not None else days_since_last_run(cfg)
    log.info("Pipeline window: last %d day(s) since previous run.", window)

    stats: dict[str, int] = {
        "voicemails_ingested": 0,
        "calls_ingested": 0,
        "classified": 0,
        "submitted": 0,
    }

    if voicemail:
        stats["voicemails_ingested"] = scrape_backlog(
            cfg, limit=limit, since_days=window
        )
    if calls:
        stats["calls_ingested"] = scrape_call_history(
            cfg, limit=limit, since_days=window
        )

    from .classify.openai_classifier import classify_pending

    stats["classified"] = classify_pending(cfg, limit=limit, submit=False)

    if submit and cfg.review.auto_submit:
        from .submit.ftc_playwright import submit_approved

        submitted = submit_approved(cfg, once=True, limit=limit)
        stats["submitted"] = max(0, submitted)

    touch_last_run(cfg)
    log.info(
        "Pipeline complete: %d VM(s) ingested, %d call(s) ingested, "
        "%d classified, %d submitted.",
        stats["voicemails_ingested"],
        stats["calls_ingested"],
        stats["classified"],
        stats["submitted"],
    )
    return stats
