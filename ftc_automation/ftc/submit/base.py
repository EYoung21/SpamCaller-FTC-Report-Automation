"""Abstract base for FTC submitters.

The Playwright implementation is the default. A pure-HTTP implementation
is sketched in ``ftc_http.py`` for v2.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from ..db import Voicemail


@dataclass
class SubmissionResult:
    success: bool
    error: Optional[str] = None
    screenshot_path: Optional[str] = None
    captcha_detected: bool = False
    throttled: bool = False


class FtcSubmitter(abc.ABC):
    @abc.abstractmethod
    def submit(self, vm: Voicemail) -> SubmissionResult:
        ...
