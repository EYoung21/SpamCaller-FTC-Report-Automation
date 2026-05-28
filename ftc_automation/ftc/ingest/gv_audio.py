"""One-time audio re-scraper for Google Voice voicemails.

For each voicemail row in the DB that still has ``audio_url IS NULL``,
this module walks the GV inbox and, for each matched row, clicks the
row to navigate to its thread view. The thread view auto-fetches the
voicemail audio from ``https://voice.google.com/u/0/a/v/<itemId>``
with ``Content-Type: audio/mpeg`` as part of the page load — we
capture that response, save the bytes to ``audio/vm-<id>.mp3``, and
go back to the list to continue. The relative path is stored in
``audio_url`` so the Flask review UI can serve it inline.

This is heavier than the metadata scrape (we have to navigate into
every voicemail) but it only ever needs to run once. By default only
spam-flagged classified rows are processed; pass ``--all`` to do the
full backlog.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable, Optional

from ..config import AppConfig, load_config
from ..db import (
    SOURCE_PLAYWRIGHT,
    STATUS_CLASSIFIED,
    Voicemail,
    init_db,
    session_scope,
)

# Re-use helpers from the metadata scraper so the row-key heuristic stays
# in sync between ingest and audio re-fetch.
from .gv_playwright import (
    THREAD_SELECTORS,
    VOICE_URL,
    _extract_visible_rows,
    _normalize_phone,
    _parse_duration,
    _pick_thread_selector,
    _resolve_chrome_executable,
    _row_key,
)

log = logging.getLogger(__name__)


def _match_key(transcript: Optional[str], duration_sec: Optional[int]) -> Optional[tuple]:
    """Stable key for matching a freshly scraped DOM row back to a DB row.

    Uses ``(transcript[:80], duration_sec)`` — both are stable across the
    metadata ingest and the audio re-scrape:
      * transcript prefix doesn't drift (unlike the relative timestamp
        strings GV displays, which roll from "5 mins ago" to "1 hr ago"
        as time passes)
      * duration in seconds is a strong disambiguator when many spam
        rows share the same templated opening words.
    """
    if not transcript:
        return None
    snippet = transcript[:80].strip()
    if not snippet:
        return None
    return (snippet, duration_sec)


def _build_targets(
    cfg: AppConfig, *, only_spam: bool
) -> tuple[dict, dict, set]:
    """Return ``(by_transcript, by_caller, all_vm_ids)``.

    * ``by_transcript[(snippet, duration_sec)]`` -> list of vm_ids
        Primary matcher: works when the GV list-view text matches what
        was scraped at ingest time.
    * ``by_caller[caller_number]`` -> list of (duration_sec, vm_id)
        Fallback matcher: stable when the list-view transcript starts
        at a different word boundary than at ingest time (a common GV
        quirk for long voicemails).
    * ``all_vm_ids`` -> set of every vm_id we need audio for
        Authoritative pending set (decoupled from how the row was keyed)
        so the loop knows when it's actually done.
    """
    init_db(cfg.resolve_path(cfg.database.path))
    by_transcript: dict[tuple, list[int]] = {}
    by_caller: dict[str, list[tuple[Optional[int], int]]] = {}
    all_vm_ids: set[int] = set()
    with session_scope() as session:
        q = session.query(Voicemail).filter(
            Voicemail.source == SOURCE_PLAYWRIGHT,
            Voicemail.audio_url.is_(None),
        )
        if only_spam:
            q = q.filter(
                Voicemail.status == STATUS_CLASSIFIED,
                Voicemail.is_spam.is_(True),
            )
        for vm in q.all():
            all_vm_ids.add(vm.id)
            tkey = _match_key(vm.transcript, vm.duration_sec)
            if tkey is not None:
                by_transcript.setdefault(tkey, []).append(vm.id)
            if vm.caller_number:
                by_caller.setdefault(vm.caller_number, []).append(
                    (vm.duration_sec, vm.id)
                )
    log.info(
        "%d voicemail(s) need audio fetched (%d indexed by transcript-snippet, "
        "%d indexed by caller-number).",
        len(all_vm_ids),
        sum(len(v) for v in by_transcript.values()),
        sum(len(v) for v in by_caller.values()),
    )
    return by_transcript, by_caller, all_vm_ids


def _persist_audio(
    cfg: AppConfig, vm_id: int, audio_bytes: bytes, content_type: str
) -> str:
    """Write ``audio/vm-<id>.<ext>`` and return the relative path."""
    audio_dir = cfg.resolve_path("audio")
    audio_dir.mkdir(parents=True, exist_ok=True)
    # Pick an extension from the content type. GV serves .mp3 in practice.
    ext = "mp3"
    if "wav" in content_type:
        ext = "wav"
    elif "ogg" in content_type:
        ext = "ogg"
    elif "mp4" in content_type or "aac" in content_type:
        ext = "m4a"

    rel = f"audio/vm-{vm_id}.{ext}"
    out_path = cfg.resolve_path(rel)
    out_path.write_bytes(audio_bytes)
    return rel


def _set_audio_url(cfg: AppConfig, vm_id: int, rel_path: str) -> None:
    init_db(cfg.resolve_path(cfg.database.path))
    with session_scope() as session:
        vm = session.get(Voicemail, vm_id)
        if vm is not None:
            vm.audio_url = rel_path


def _try_fetch_one(page, row_locator, vm_id: int) -> Optional[tuple[bytes, str]]:
    """Click the row to navigate into its thread; capture the audio response
    that GV auto-fetches on page load. Returns ``(bytes, content_type)`` or
    None.
    """
    try:
        with page.expect_response(
            lambda r: "audio/" in (r.headers.get("content-type", "") or "").lower()
                or "/a/v/" in r.url,
            timeout=15000,
        ) as resp_info:
            row_locator.click(timeout=5000)
        response = resp_info.value
    except Exception as exc:
        log.warning("VM %s: no audio response after click (%s)", vm_id, exc)
        return None

    try:
        body = response.body()
        ctype = response.headers.get("content-type", "audio/mpeg")
    except Exception as exc:
        log.warning("VM %s: failed to read response body (%s)", vm_id, exc)
        return None

    return body, ctype


def rescrape_audio(
    cfg: AppConfig,
    *,
    only_spam: bool = True,
    limit: Optional[int] = None,
) -> int:
    """Walk GV and fetch audio for every matching voicemail.

    Returns the number of rows whose ``audio_url`` was newly populated.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Playwright is not installed. Run `pip install -r requirements.txt`."
        ) from exc

    storage_state_path = cfg.resolve_path(cfg.google_voice.storage_state_path)
    if not storage_state_path.exists():
        raise SystemExit(
            f"No storage_state found at {storage_state_path}. Run "
            f"`python -m ftc_automation login` first."
        )

    by_transcript, by_caller, all_vm_ids = _build_targets(
        cfg, only_spam=only_spam
    )
    if not all_vm_ids:
        log.info("Nothing to fetch — every targeted row already has audio.")
        return 0

    if limit is not None:
        keep = set(list(all_vm_ids)[:limit])
        all_vm_ids = keep
        by_transcript = {
            k: [vm for vm in v if vm in keep] for k, v in by_transcript.items()
        }
        by_transcript = {k: v for k, v in by_transcript.items() if v}
        by_caller = {
            c: [(d, vm) for d, vm in v if vm in keep] for c, v in by_caller.items()
        }
        by_caller = {c: v for c, v in by_caller.items() if v}
        log.info("Capped to first %d row(s).", limit)

    # Mutable working copies.
    pending_ids: set[int] = set(all_vm_ids)
    pending_by_transcript: dict[tuple, list[int]] = {
        k: list(v) for k, v in by_transcript.items()
    }
    pending_by_caller: dict[str, list[tuple[Optional[int], int]]] = {
        c: list(v) for c, v in by_caller.items()
    }
    fetched = 0
    chrome_exe = _resolve_chrome_executable()

    def _pending_total() -> int:
        return len(pending_ids)

    def _consume_vm(vm_id: int) -> None:
        """Remove ``vm_id`` from every index so we don't fetch it twice."""
        pending_ids.discard(vm_id)
        for k, vms in list(pending_by_transcript.items()):
            if vm_id in vms:
                vms.remove(vm_id)
            if not vms:
                pending_by_transcript.pop(k, None)
        for c, entries in list(pending_by_caller.items()):
            new_entries = [(d, v) for (d, v) in entries if v != vm_id]
            if new_entries:
                pending_by_caller[c] = new_entries
            else:
                pending_by_caller.pop(c, None)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=chrome_exe,
            channel="chrome" if not chrome_exe else None,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            storage_state=str(storage_state_path),
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        page.goto(VOICE_URL, wait_until="domcontentloaded", timeout=60000)
        # GV can be slow to render the list after a fresh launch (and
        # extra-slow if it's mildly rate-limiting us). Give it longer
        # before failing with the "no selector matched" error.
        page.wait_for_timeout(5000)

        try:
            sel = _pick_thread_selector(page)
        except Exception:
            debug_dir = cfg.resolve_path("submissions")
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_path = debug_dir / "gv_audio_debug.png"
            try:
                page.screenshot(path=str(debug_path), full_page=True)
                log.error(
                    "Could not find voicemail rows. URL=%s. Debug screenshot at %s",
                    page.url,
                    debug_path,
                )
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            raise

        viewport_handle = None
        for vs in [
            "cdk-virtual-scroll-viewport",
            "[role='list']",
            "[class*='thread-list']",
        ]:
            try:
                loc = page.locator(vs).first
                if loc.count() > 0:
                    viewport_handle = loc
                    break
            except Exception:
                continue

        no_new_iters = 0
        last_pending_size = _pending_total()
        # Track which exact DOM rows we've already clicked (by their
        # full transcript) so we never re-process the same row even when
        # multiple VMs share a key.
        seen_row_ids: set[str] = set()

        while pending_ids:
            # 1. Look at currently visible rows for any matches against
            #    pending VMs that we haven't yet seen.
            rows_dump = _extract_visible_rows(page, sel)
            items = page.locator(sel)
            matched_index: Optional[int] = None
            matched_vm_id: Optional[int] = None
            matched_snippet: Optional[str] = None
            matched_via: str = ""

            for i, r in enumerate(rows_dump):
                full_transcript = (r.get("transcription") or "").strip()
                participant = (r.get("participant") or "").strip()
                row_dedupe = "|".join([
                    participant,
                    (r.get("timestamp_text") or "").strip(),
                    full_transcript[:120],
                ])
                if row_dedupe in seen_row_ids:
                    continue
                duration_sec = _parse_duration(r.get("duration_text"))

                # --- Match attempt 1: (transcript[:80], duration_sec)
                vm_id: Optional[int] = None
                via = ""
                tkey = _match_key(full_transcript, duration_sec)
                if tkey is not None:
                    candidates = pending_by_transcript.get(tkey, [])
                    if candidates:
                        vm_id = candidates[0]
                        via = "transcript+duration"

                # --- Match attempt 2: same snippet, ±1s duration
                if vm_id is None and duration_sec is not None and tkey is not None:
                    for delta in (-1, 1):
                        alt = (tkey[0], duration_sec + delta)
                        candidates = pending_by_transcript.get(alt, [])
                        if candidates:
                            vm_id = candidates[0]
                            via = f"transcript+duration±1"
                            break

                # --- Match attempt 3: caller_number + duration (±2s)
                if vm_id is None:
                    phone = _normalize_phone(participant)
                    if phone is not None:
                        bucket = pending_by_caller.get(phone, [])
                        if bucket:
                            if duration_sec is not None:
                                for d, v in bucket:
                                    if d is not None and abs(d - duration_sec) <= 2:
                                        vm_id = v
                                        via = "caller+duration±2"
                                        break
                            if vm_id is None:
                                # Last-ditch: any pending VM with this
                                # caller number. FIFO so collisions are
                                # handed out deterministically.
                                vm_id = bucket[0][1]
                                via = "caller-only"

                if vm_id is None:
                    continue

                matched_index = i
                matched_vm_id = vm_id
                matched_snippet = full_transcript[:50] or participant[:50]
                matched_via = via
                seen_row_ids.add(row_dedupe)
                break

            if matched_index is not None:
                log.info(
                    "Fetching audio for VM %s via %s (snippet=%r)…",
                    matched_vm_id,
                    matched_via,
                    matched_snippet,
                )
                row = items.nth(matched_index)
                result = _try_fetch_one(page, row, matched_vm_id or -1)
                _consume_vm(matched_vm_id)  # type: ignore[arg-type]

                if result is not None:
                    audio_bytes, ctype = result
                    rel = _persist_audio(cfg, matched_vm_id, audio_bytes, ctype)
                    _set_audio_url(cfg, matched_vm_id, rel)
                    fetched += 1
                    log.info(
                        "VM %s -> saved %s (%.1f KB, ctype=%s). "
                        "%d still pending; %d fetched so far.",
                        matched_vm_id,
                        rel,
                        len(audio_bytes) / 1024,
                        ctype,
                        _pending_total(),
                        fetched,
                    )

                # Navigate back to the list. ``page.go_back`` preserves
                # scroll position in some browsers; otherwise we just keep
                # scrolling and ``seen_keys`` prevents re-work.
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(1200)
                except Exception:
                    page.goto(VOICE_URL, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                # Politeness pause between voicemail fetches. The first
                # full run hit ~10 navigations/min which got Google to
                # silently sign us out at the end. Throttle to ~5/min.
                time.sleep(2.5)
                no_new_iters = 0
                last_pending_size = _pending_total()
                continue

            # 2. No matches visible right now — scroll one page-worth.
            scrolled = False
            if viewport_handle is not None:
                try:
                    viewport_handle.evaluate(
                        "el => { el.scrollTop = el.scrollTop + el.clientHeight * 0.9; }"
                    )
                    scrolled = True
                except Exception:
                    scrolled = False
            if not scrolled:
                try:
                    items = page.locator(sel)
                    if items.count() > 0:
                        items.nth(items.count() - 1).scroll_into_view_if_needed(
                            timeout=3000
                        )
                        scrolled = True
                except Exception:
                    pass
            if not scrolled:
                try:
                    page.keyboard.press("PageDown")
                except Exception:
                    pass

            page.wait_for_timeout(900)

            cur_pending = _pending_total()
            if cur_pending == last_pending_size:
                no_new_iters += 1
            else:
                no_new_iters = 0
            last_pending_size = cur_pending

            if no_new_iters >= 30:
                log.info(
                    "Scrolling stopped yielding matches for 30 iters; "
                    "stopping with %d row(s) still unfetched.",
                    cur_pending,
                )
                break

        try:
            context.close()
        except Exception:
            pass

    log.info(
        "Audio re-scrape complete. Fetched %d new clip(s); %d still missing.",
        fetched,
        _pending_total(),
    )
    return fetched


def run(argv: Optional[Iterable[str]] = None) -> int:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    rescrape_audio(cfg)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
