"""Pydantic configuration loader for the FTC voicemail reporter.

The single entry point is :func:`load_config`, which reads ``config.yaml``
from the package root (overridable via ``FTC_CONFIG_PATH``) and returns a
fully validated :class:`AppConfig`. Secrets that are sensitive (currently
just the OpenAI API key) may also be supplied via environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config.yaml"


class PersonalConfig(BaseModel):
    first_name: str
    last_name: str
    street_address: str
    city: str
    state: str
    zip_code: str

    @field_validator("state")
    @classmethod
    def _two_letter_state(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 2:
            raise ValueError("state must be a two-letter USPS code")
        return v


class OpenAIConfig(BaseModel):
    api_key: str = ""
    model: str = "gpt-4o-mini"

    def resolved_api_key(self) -> str:
        env_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if env_key:
            return env_key
        return self.api_key.strip()


class GmailConfig(BaseModel):
    client_secret_path: str = "secrets/gmail_client_secret.json"
    token_path: str = "secrets/gmail_token.json"
    query: str = "from:voice-noreply@google.com newer_than:30d"


class GoogleVoiceConfig(BaseModel):
    storage_state_path: str = "secrets/gv_storage_state.json"
    max_threads: int = 5000


class DatabaseConfig(BaseModel):
    path: str = "voicemails.db"


class FtcSubmitterConfig(BaseModel):
    url: str = "https://www.donotcall.gov/report.html"
    headless: bool = False
    submit_interval_sec: float = 20.0
    screenshot_dir: str = "submissions"


class ReviewConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5000
    bulk_approve_min_confidence: float = 0.9


class AppConfig(BaseModel):
    gv_number: str
    personal: PersonalConfig
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    google_voice: GoogleVoiceConfig = Field(default_factory=GoogleVoiceConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    ftc: FtcSubmitterConfig = Field(default_factory=FtcSubmitterConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    @field_validator("gv_number")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) < 10:
            raise ValueError("gv_number must contain at least 10 digits")
        return digits

    def resolve_path(self, relative: str) -> Path:
        """Resolve a path that is either absolute or relative to the
        package root."""
        p = Path(relative)
        if p.is_absolute():
            return p
        return PACKAGE_ROOT / p


_cached: Optional[AppConfig] = None


def load_config(path: Optional[Path | str] = None, *, force: bool = False) -> AppConfig:
    """Load and cache the :class:`AppConfig`.

    ``path`` may be supplied explicitly (handy for tests); otherwise the
    ``FTC_CONFIG_PATH`` env var or :data:`DEFAULT_CONFIG_PATH` is used.
    """
    global _cached
    if _cached is not None and not force and path is None:
        return _cached

    if path is None:
        env = os.environ.get("FTC_CONFIG_PATH")
        path = Path(env) if env else DEFAULT_CONFIG_PATH
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Copy config.example.yaml to "
            f"config.yaml and edit it, or set FTC_CONFIG_PATH."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = AppConfig.model_validate(raw)
    if path == DEFAULT_CONFIG_PATH or path == Path(os.environ.get("FTC_CONFIG_PATH", "")):
        _cached = cfg
    return cfg
