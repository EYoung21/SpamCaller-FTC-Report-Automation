"""Print 15 spam voicemails that still lack audio, to help diagnose
why the rescraper's matcher missed them."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ftc_automation.ftc.config import load_config  # noqa: E402
from ftc_automation.ftc.db import init_db, get_session, Voicemail  # noqa: E402


def main() -> int:
    cfg = load_config()
    init_db(cfg.resolve_path(cfg.database.path))
    s = get_session()
    rows = (
        s.query(Voicemail)
        .filter(
            Voicemail.status == "classified",
            Voicemail.is_spam.is_(True),
            Voicemail.audio_url.is_(None),
        )
        .order_by(Voicemail.id)
        .limit(15)
        .all()
    )
    for r in rows:
        t = (r.transcript or "").replace("\n", " ").strip()
        print(
            f"VM {r.id:>5} | dur={r.duration_sec!s:>5} | "
            f"caller={(r.caller_number or '?'):>14} | "
            f"trans[:70]={t[:70]!r}"
        )
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
