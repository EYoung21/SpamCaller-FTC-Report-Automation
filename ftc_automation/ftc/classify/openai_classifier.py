"""OpenAI classifier for voicemails.

For every voicemail in status='new' we call the OpenAI API once with
structured outputs (JSON schema mode), receive a typed dict, and write
the result back to the database with status='classified'.

Run via CLI:

    python -m ftc_automation classify
    python -m ftc_automation classify --limit 50
    python -m ftc_automation classify --reclassify
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select

from ..config import AppConfig, load_config
from ..db import (
    STATUS_CLASSIFIED,
    STATUS_NEW,
    Voicemail,
    init_db,
    session_scope,
)
from .ftc_mapping import map_scam_category
from .prompts import (
    FEW_SHOT_EXAMPLES,
    JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_user_prompt,
)


log = logging.getLogger(__name__)


def _build_messages(caller_number: Optional[str], transcript: str) -> list[dict]:
    """Construct the chat messages list, including few-shot examples."""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ex in FEW_SHOT_EXAMPLES:
        messages.append(
            {
                "role": "user",
                "content": build_user_prompt(ex["caller_number"], ex["transcript"]),
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(ex["expected"], ensure_ascii=False),
            }
        )
    messages.append(
        {
            "role": "user",
            "content": build_user_prompt(caller_number, transcript),
        }
    )
    return messages


def _call_openai(
    client,
    *,
    model: str,
    caller_number: Optional[str],
    transcript: str,
) -> dict:
    messages = _build_messages(caller_number, transcript)

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": JSON_SCHEMA,
        },
    )
    content = resp.choices[0].message.content
    if not content:
        raise RuntimeError("OpenAI returned empty content")
    return json.loads(content)


def _apply_classification(vm: Voicemail, result: dict) -> None:
    vm.is_spam = bool(result.get("is_spam"))
    vm.confidence = float(result.get("confidence") or 0.0)
    vm.callback_number = result.get("callback_number") or None
    vm.claimed_company = result.get("claimed_company") or None
    vm.scam_category = result.get("scam_category") or "unknown"
    vm.summary = result.get("summary") or None
    vm.comment_text = result.get("ftc_comment") or None
    vm.should_report = bool(result.get("should_report"))

    subject_id, free_text = map_scam_category(vm.scam_category)
    vm.ftc_subject_id = subject_id
    vm.ftc_subject_text = free_text

    vm.status = STATUS_CLASSIFIED
    vm.classified_at = datetime.utcnow()


def classify_pending(
    cfg: AppConfig,
    *,
    limit: Optional[int] = None,
    reclassify: bool = False,
) -> int:
    """Classify pending voicemails. Returns number classified."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "openai package missing. Run `pip install -r requirements.txt`."
        ) from exc

    api_key = cfg.openai.resolved_api_key()
    if not api_key:
        raise SystemExit(
            "No OpenAI API key found. Set OPENAI_API_KEY or fill openai.api_key in config.yaml."
        )

    init_db(cfg.resolve_path(cfg.database.path))
    client = OpenAI(api_key=api_key)

    statuses = [STATUS_NEW]
    if reclassify:
        statuses.append(STATUS_CLASSIFIED)

    classified = 0
    with session_scope() as session:
        stmt = (
            select(Voicemail)
            .where(Voicemail.status.in_(statuses))
            .order_by(Voicemail.received_at.is_(None), Voicemail.received_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = list(session.execute(stmt).scalars())

    log.info("Classifying %d voicemail(s)...", len(rows))

    for row_id_tuple in [(r.id,) for r in rows]:
        vm_id = row_id_tuple[0]
        with session_scope() as session:
            vm = session.get(Voicemail, vm_id)
            if vm is None:
                continue
            transcript = (vm.transcript or "").strip()
            if not transcript:
                log.info("VM %s has no transcript; marking classified with unknown.", vm.id)
                vm.is_spam = False
                vm.confidence = 0.0
                vm.scam_category = "unknown"
                vm.ftc_subject_id, vm.ftc_subject_text = map_scam_category("unknown")
                vm.summary = "(no transcript)"
                vm.comment_text = ""
                vm.should_report = False
                vm.status = STATUS_CLASSIFIED
                vm.classified_at = datetime.utcnow()
                classified += 1
                continue

            try:
                result = _call_openai(
                    client,
                    model=cfg.openai.model,
                    caller_number=vm.caller_number,
                    transcript=transcript,
                )
            except Exception as exc:  # pragma: no cover - network errors
                log.warning("Classification failed for VM %s: %s", vm.id, exc)
                continue

            _apply_classification(vm, result)
            classified += 1
            log.info(
                "VM %s -> is_spam=%s conf=%.2f cat=%s",
                vm.id,
                vm.is_spam,
                vm.confidence or 0.0,
                vm.scam_category,
            )

        # Cheap politeness sleep so we don't slam the API.
        time.sleep(0.2)

    log.info("Done. Classified %d voicemail(s).", classified)
    return classified


def run(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    classify_pending(cfg)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
