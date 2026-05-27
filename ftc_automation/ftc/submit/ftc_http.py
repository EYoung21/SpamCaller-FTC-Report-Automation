"""Stub for a pure-HTTP (requests-based) FTC submitter.

This is intentionally left unimplemented until we've watched several
Playwright submissions in DevTools and confirmed the POST body shape
(form fields, hidden tokens, ASP.NET viewstate, etc.). When ready,
implement :meth:`FtcHttpSubmitter.submit` and switch ``cli.py`` over
via a config flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from ..db import Voicemail
from .base import FtcSubmitter, SubmissionResult


@dataclass
class FtcHttpSubmitter(FtcSubmitter):
    cfg: AppConfig

    def submit(self, vm: Voicemail) -> SubmissionResult:  # pragma: no cover
        raise NotImplementedError(
            "FtcHttpSubmitter is a v2 placeholder. Use FtcPlaywrightSubmitter."
        )
