"""One-shot debug helper: open GV and dump the DOM of the first voicemail
row so we can see what tag/aria-label the play button actually uses.

Usage:
    python scripts/inspect_gv_row.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ftc_automation.ftc.config import load_config  # noqa: E402
from ftc_automation.ftc.ingest.gv_playwright import (  # noqa: E402
    VOICE_URL,
    _pick_thread_selector,
    _resolve_chrome_executable,
)


def main() -> int:
    cfg = load_config()
    storage_state_path = cfg.resolve_path(cfg.google_voice.storage_state_path)
    if not storage_state_path.exists():
        print(f"No storage_state at {storage_state_path}. Run `python -m ftc_automation login` first.")
        return 1

    from playwright.sync_api import sync_playwright

    chrome_exe = _resolve_chrome_executable()
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
        page.wait_for_timeout(2500)

        sel = _pick_thread_selector(page)
        rows = page.locator(sel)
        n = rows.count()
        print(f"Found {n} row(s) in viewport. Inspecting first 2.\n")

        for i in range(min(2, n)):
            row = rows.nth(i)
            print(f"=== ROW {i} ===")
            # Dump all buttons & their aria-labels inside the row.
            buttons = row.locator("button, [role='button']")
            bn = buttons.count()
            print(f"  buttons in row: {bn}")
            for j in range(bn):
                b = buttons.nth(j)
                try:
                    al = b.get_attribute("aria-label") or ""
                    txt = b.inner_text(timeout=500).strip()[:40]
                    visible = b.is_visible()
                    classes = b.get_attribute("class") or ""
                    print(
                        f"    [{j}] aria-label={al!r:40s}  text={txt!r:25s}  "
                        f"visible={visible}  class={classes[:50]!r}"
                    )
                except Exception as exc:
                    print(f"    [{j}] (error inspecting: {exc})")

            print("\n  Outer HTML (first 1500 chars):")
            try:
                html = row.evaluate("el => el.outerHTML")
                print("    " + html[:1500].replace("\n", "\n    "))
            except Exception as exc:
                print(f"    (error: {exc})")
            print()

        # After hover, repeat
        if n > 0:
            print("\n=== HOVERING row 0 then re-listing buttons ===")
            try:
                rows.nth(0).hover(timeout=2000)
                page.wait_for_timeout(500)
                buttons = rows.nth(0).locator("button, [role='button']")
                bn = buttons.count()
                for j in range(bn):
                    b = buttons.nth(j)
                    al = b.get_attribute("aria-label") or ""
                    visible = b.is_visible()
                    print(f"  [{j}] aria-label={al!r}  visible={visible}")
            except Exception as exc:
                print(f"  (hover error: {exc})")

        # Also list any audio-related elements anywhere on the page.
        print("\n=== Page-level audio elements ===")
        for sel_audio in ["audio", "[class*='audio']", "[id*='audio']", "gv-voicemail-audio-player"]:
            cnt = page.locator(sel_audio).count()
            if cnt:
                print(f"  {sel_audio}: {cnt}")

        # NEW: click row 0 to open the thread view and inspect the audio player there.
        if n > 0:
            print("\n=== Clicking row 0 to inspect thread view ===")
            # Attach listener BEFORE the click so we capture nav-time responses too.
            captured_all = []
            def on_resp_all(r):
                ct = (r.headers.get("content-type", "") or "").lower()
                cl = r.headers.get("content-length", "0")
                try:
                    cl_int = int(cl)
                except Exception:
                    cl_int = 0
                if cl_int > 20000 or "audio" in ct or ".mp3" in r.url:
                    captured_all.append((r.url, ct, cl_int))
            page.on("response", on_resp_all)

            url_before = page.url
            try:
                rows.nth(0).click(timeout=5000)
            except Exception as exc:
                print(f"  click failed: {exc}")
            page.wait_for_timeout(4500)
            print(f"\n  After navigation, {len(captured_all)} non-trivial response(s):")
            for u, ct, cl in captured_all[:40]:
                print(f"    [{cl:>10} bytes  ctype={ct[:30]:30s}]  {u[:160]}")
            captured_all.clear()
            print(f"  url before: {url_before}")
            print(f"  url after : {page.url}")

            for sel_audio in [
                "audio",
                "gv-voicemail-audio-player",
                "[class*='audio-player']",
                "button[aria-label*='Play' i]",
                "button[aria-label*='play voicemail' i]",
                "[role='button'][aria-label*='play' i]",
                "mat-icon[fonticon='play_arrow']",
            ]:
                cnt = page.locator(sel_audio).count()
                if cnt:
                    print(f"  {sel_audio}: {cnt}")
                    try:
                        first = page.locator(sel_audio).first
                        al = first.get_attribute("aria-label")
                        cls = first.get_attribute("class")
                        src = first.get_attribute("src")
                        print(f"    aria-label={al!r}  src={src!r}")
                        print(f"    class={cls!r}")
                    except Exception:
                        pass

            print("\n  All buttons with aria-label containing 'play' or 'pause' or 'voicemail':")
            all_buttons = page.locator("button, [role='button']")
            bn = all_buttons.count()
            for j in range(bn):
                b = all_buttons.nth(j)
                try:
                    al = (b.get_attribute("aria-label") or "").lower()
                    if "play" in al or "pause" in al or "voicemail" in al:
                        print(f"    [{j}] aria-label={b.get_attribute('aria-label')!r}  visible={b.is_visible()}")
                except Exception:
                    pass

            # Listen for ALL responses with non-trivial bodies after clicking play.
            print("\n  Listening for ALL responses for 10s after clicking play...")
            for sel_play in [
                "button[aria-label='Play voicemail player']",
                "button[aria-label*='Play voicemail' i]",
                "button[aria-label*='Play' i]",
            ]:
                try:
                    play = page.locator(sel_play).first
                    if play.count() > 0:
                        print(f"  clicking play via {sel_play}")
                        play.click(timeout=3000)
                        break
                except Exception as exc:
                    print(f"  {sel_play} click error: {exc}")
            page.wait_for_timeout(10000)
            print(f"  captured {len(captured_all)} non-trivial response(s) after play:")
            for u, ct, cl in captured_all[:40]:
                print(f"    [{cl:>10} bytes  ctype={ct[:30]:30s}]  {u[:160]}")

            # Also check if an <audio> element appeared after play.
            print("\n  After play, audio elements on page:")
            for sel_audio in ["audio", "audio[src]", "[class*='audio-player']"]:
                cnt = page.locator(sel_audio).count()
                if cnt:
                    print(f"  {sel_audio}: {cnt}")
                    try:
                        first = page.locator(sel_audio).first
                        src = first.get_attribute("src")
                        print(f"    src={src!r}")
                    except Exception:
                        pass

        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
