"""One-off diagnostic: capture exactly what donotcall.gov returns after submit."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from ftc_automation.ftc.config import load_config
from ftc_automation.ftc.db import STATUS_APPROVED, Voicemail, init_db, session_scope
from ftc_automation.ftc.submit.ftc_playwright import FtcPlaywrightSubmitter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    cfg = load_config()
    init_db(cfg.resolve_path(cfg.database.path))

    with session_scope() as session:
        vm = session.execute(
            select(Voicemail)
            .where(Voicemail.status == STATUS_APPROVED)
            .order_by(Voicemail.received_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if vm is None:
            print("No approved voicemails in queue.")
            return 1
        vm_id = vm.id
        caller = vm.caller_number

    out_dir = cfg.resolve_path("submissions/debug")
    out_dir.mkdir(parents=True, exist_ok=True)

    submitter = FtcPlaywrightSubmitter(cfg=cfg)
    submitter.start()
    page = submitter._page
    assert page is not None

    try:
        page.goto(cfg.ftc.url, wait_until="domcontentloaded")
        try:
            btn = page.locator("#MainContinueButton")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
                page.wait_for_selector("#PhoneTextBox", timeout=20000)
        except Exception:
            pass

        page.wait_for_selector("#PhoneTextBox", timeout=30000)
        page.screenshot(path=str(out_dir / "01_step1_loaded.png"), full_page=True)

        with session_scope() as session:
            vm = session.get(Voicemail, vm_id)
            assert vm is not None
            submitter._fill_step_one(page, vm)
            page.screenshot(path=str(out_dir / "02_step1_filled.png"), full_page=True)
            page.click("#StepOneContinueButton")
            page.wait_for_selector("#CallerPhoneNumberTextBox", timeout=30000)
            page.screenshot(path=str(out_dir / "03_step2_loaded.png"), full_page=True)
            submitter._fill_step_two(page, vm)
            page.screenshot(path=str(out_dir / "04_step2_filled.png"), full_page=True)
            page.click("#StepTwoSubmitButton")

        page.wait_for_timeout(8000)
        page.screenshot(path=str(out_dir / "05_after_submit.png"), full_page=True)

        body = page.locator("body").inner_text(timeout=5000)
        html = page.content()
        (out_dir / "05_after_submit.txt").write_text(body, encoding="utf-8")
        (out_dir / "05_after_submit.html").write_text(html[:50000], encoding="utf-8")

        checks = {
            "StepTwoAcceptedPanel": page.locator("#StepTwoAcceptedPanel").count(),
            "system difficulties": "system difficulties" in body.lower(),
            "unable to process": "unable to process your request" in body.lower(),
            "nothing submitted": "nothing" in body.lower() and "submit" in body.lower(),
            "recaptcha iframe": page.locator("iframe[src*='recaptcha']").count(),
            "url": page.url,
        }
        print(f"VM {vm_id} ({caller})")
        for k, v in checks.items():
            print(f"  {k}: {v}")
        print(f"Artifacts: {out_dir}")
        print("--- body preview ---")
        print(body[:2000])
        return 0
    finally:
        submitter.stop()


if __name__ == "__main__":
    raise SystemExit(main())
