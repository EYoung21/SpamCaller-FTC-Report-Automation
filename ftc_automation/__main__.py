"""Module entrypoint so ``python -m ftc_automation ...`` works."""

from .cli import main

raise SystemExit(main())
