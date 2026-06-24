"""Bedrock (Nova Micro) classifier for voicemails.

For every voicemail in status='new' we call Amazon Bedrock once with
structured JSON output, receive a typed dict, and write the result back
to the database with status='classified'.

Run via CLI:

    python -m ftc_automation classify
    python -m ftc_automation classify --limit 50
    python -m ftc_automation classify --reclassify
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select

from ..config import AppConfig, load_config
from ..db import (
    STATUS_APPROVED,
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


def _bedrock_json_schema() -> dict:
    """JSON schema compatible with Bedrock structured outputs."""
    schema = json.loads(json.dumps(JSON_SCHEMA["schema"]))
    for prop in ("callback_number", "claimed_company"):
        schema["properties"][prop] = {
            "type": "string",
            "description": (
                (schema["properties"][prop].get("description") or "")
                + " Use an empty string if absent."
            ),
        }
    return schema


def _build_bedrock_messages(caller_number: Optional[str], transcript: str) -> list[dict]:
    """Construct Converse API messages, including few-shot examples."""
    messages: list[dict] = []
    for ex in FEW_SHOT_EXAMPLES:
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "text": build_user_prompt(ex["caller_number"], ex["transcript"]),
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"text": json.dumps(ex["expected"], ensure_ascii=False)},
                ],
            }
        )
    messages.append(
        {
            "role": "user",
            "content": [
                {"text": build_user_prompt(caller_number, transcript)},
            ],
        }
    )
    return messages


def _call_bedrock(
    client,
    *,
    model_id: str,
    caller_number: Optional[str],
    transcript: str,
) -> dict:
    messages = _build_bedrock_messages(caller_number, transcript)
    schema = _bedrock_json_schema()

    kwargs = {
        "modelId": model_id,
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": messages,
        "inferenceConfig": {"maxTokens": 2048, "temperature": 0},
    }
    use_structured = "nova" not in model_id.lower()

    if use_structured:
        kwargs["outputConfig"] = {
            "textFormat": {
                "type": "json_schema",
                "structure": {
                    "jsonSchema": {
                        "schema": json.dumps(schema),
                        "name": JSON_SCHEMA["name"],
                        "description": "Voicemail spam classification result",
                    }
                },
            }
        }

    try:
        resp = client.converse(**kwargs)
    except Exception as exc:
        err = str(exc)
        if use_structured and (
            "outputConfig" in err or "textFormat" in err or "UnknownParameter" in err
        ):
            log.warning(
                "Bedrock structured output unavailable (%s); falling back to prompt-only.",
                exc,
            )
            kwargs.pop("outputConfig", None)
            resp = client.converse(**kwargs)
        else:
            raise

    content_blocks = resp.get("output", {}).get("message", {}).get("content") or []
    text = ""
    for block in content_blocks:
        if isinstance(block, dict) and block.get("text"):
            text = block["text"]
            break
    if not text:
        raise RuntimeError("Bedrock returned empty content")

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    result = json.loads(text)
    for key in ("callback_number", "claimed_company"):
        if not result.get(key):
            result[key] = None
    return result


def _build_openai_messages(caller_number: Optional[str], transcript: str) -> list[dict]:
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
    messages = _build_openai_messages(caller_number, transcript)
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


def _apply_classification(
    vm: Voicemail,
    result: dict,
    *,
    auto_approve_spam: bool = True,
) -> None:
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

    now = datetime.utcnow()
    vm.classified_at = now
    vm.status = STATUS_CLASSIFIED
    if auto_approve_spam and vm.is_spam and vm.should_report:
        vm.status = STATUS_APPROVED
        vm.reviewed_at = now


def classify_pending(
    cfg: AppConfig,
    *,
    limit: Optional[int] = None,
    reclassify: bool = False,
    submit: Optional[bool] = None,
) -> int:
    """Classify pending voicemails via Bedrock Nova Micro. Returns count."""
    try:
        import boto3  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "boto3 package missing. Run `pip install -r requirements.txt`."
        ) from exc

    region = cfg.bedrock.resolved_region()
    model_id = cfg.bedrock.resolved_model_id()
    if not model_id:
        raise SystemExit(
            "No Bedrock model configured. Set BEDROCK_MODEL_ID or bedrock.model_id in config.yaml."
        )

    init_db(cfg.resolve_path(cfg.database.path))
    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=region,
    )
    openai_client = None
    backend = "bedrock"

    statuses = [STATUS_NEW]
    if reclassify:
        statuses.append(STATUS_CLASSIFIED)

    classified = 0
    with session_scope() as session:
        stmt = (
            select(Voicemail)
            .where(
                Voicemail.status.in_(statuses),
                Voicemail.gv_suspected_spam.isnot(True),
            )
            .order_by(Voicemail.received_at.is_(None), Voicemail.received_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        rows = list(session.execute(stmt).scalars())

    log.info(
        "Classifying %d voicemail(s) with Bedrock %s (%s)...",
        len(rows),
        model_id,
        region,
    )

    def _classify_transcript(caller_number: Optional[str], transcript: str) -> dict:
        nonlocal backend, openai_client
        if backend == "openai":
            assert openai_client is not None
            return _call_openai(
                openai_client,
                model=cfg.openai.model,
                caller_number=caller_number,
                transcript=transcript,
            )
        try:
            return _call_bedrock(
                bedrock_client,
                model_id=model_id,
                caller_number=caller_number,
                transcript=transcript,
            )
        except Exception as exc:
            if "AccessDenied" not in str(exc):
                raise
            if os.environ.get("PREFER_BEDROCK", "").strip().lower() in ("1", "true", "yes"):
                raise
            api_key = cfg.openai.resolved_api_key()
            if not api_key:
                raise
            from openai import OpenAI  # type: ignore

            log.warning(
                "Bedrock denied (%s); falling back to OpenAI %s for remaining rows.",
                exc,
                cfg.openai.model,
            )
            backend = "openai"
            openai_client = OpenAI(api_key=api_key)
            return _call_openai(
                openai_client,
                model=cfg.openai.model,
                caller_number=caller_number,
                transcript=transcript,
            )

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
                result = _classify_transcript(vm.caller_number, transcript)
            except Exception as exc:  # pragma: no cover - network errors
                log.warning("Classification failed for VM %s: %s", vm.id, exc)
                continue

            _apply_classification(
                vm,
                result,
                auto_approve_spam=cfg.review.auto_approve_spam,
            )
            classified += 1
            log.info(
                "VM %s -> is_spam=%s conf=%.2f cat=%s status=%s",
                vm.id,
                vm.is_spam,
                vm.confidence or 0.0,
                vm.scam_category,
                vm.status,
            )

        time.sleep(0.2)

    log.info("Done. Classified %d voicemail(s).", classified)

    do_submit = cfg.review.auto_submit if submit is None else submit
    if classified and do_submit:
        from ..submit.ftc_playwright import submit_approved

        submitted = submit_approved(cfg, once=True, limit=limit)
        if submitted >= 0:
            log.info("Auto-submitted %d complaint(s) after classification.", submitted)

    return classified


def run(argv: Optional[Iterable[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    classify_pending(cfg)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
