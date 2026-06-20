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
REPO_ROOT = PACKAGE_ROOT.parent
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config.yaml"


def _load_dotenv_once() -> None:
    """Load .env from the repo root (and the package root, as a fallback).

    Uses python-dotenv when available; falls back to a tiny built-in
    parser so the app still works if the dep isn't installed.
    """
    candidates = [REPO_ROOT / ".env", PACKAGE_ROOT / ".env"]
    try:
        from dotenv import load_dotenv  # type: ignore

        for p in candidates:
            if p.exists():
                load_dotenv(dotenv_path=p, override=False)
        return
    except ImportError:
        pass

    for p in candidates:
        if not p.exists():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[len("export ") :]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


_load_dotenv_once()


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


class BedrockConfig(BaseModel):
    """Amazon Bedrock (Nova Micro) — default voicemail spam classifier."""

    region: str = "us-east-1"
    model_id: str = "amazon.nova-micro-v1:0"
    timeout_sec: int = 120

    def resolved_region(self) -> str:
        for key in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            val = os.environ.get(key, "").strip()
            if val:
                return val
        return self.region.strip()

    def resolved_model_id(self) -> str:
        return os.environ.get("BEDROCK_MODEL_ID", self.model_id).strip()

    def resolved_timeout_sec(self) -> int:
        raw = os.environ.get("BEDROCK_TIMEOUT", "").strip()
        if raw.isdigit():
            return int(raw)
        return self.timeout_sec


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
    # Optional HTTP(S) proxies so Playwright exits via a different IP.
    # Examples:
    #   - "http://user:pass@proxy.example.com:8080"
    #   - "socks5://127.0.0.1:1080"   (local VPN client)
    # Comma-separated list also accepted via FTC_PROXIES env var.
    proxies: list[str] = Field(default_factory=list)
    # Optional file with one proxy URL per line (gitignored). Merged with
    # ``proxies`` above. Run ``python scripts/fetch_proxies.py`` to populate.
    proxy_file: str = "secrets/proxies.txt"
    # When to pick the next proxy from ``proxies``:
    #   on_throttle — after a throttle response (default; good for scheduled retries)
    #   each_run    — at the start of every ``submit --once`` invocation
    #   each_submit — between every complaint in a batch
    proxy_rotate: str = "on_throttle"

    @field_validator("proxy_rotate")
    @classmethod
    def _validate_proxy_rotate(cls, v: str) -> str:
        allowed = {"on_throttle", "each_run", "each_submit"}
        v = (v or "on_throttle").strip().lower()
        if v not in allowed:
            raise ValueError(f"proxy_rotate must be one of {sorted(allowed)}")
        return v


class ReviewConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5000
    bulk_approve_min_confidence: float = 0.9


class AppConfig(BaseModel):
    gv_number: str
    personal: PersonalConfig
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)
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

    # Optional comma-separated proxy list from the environment (secrets stay
    # out of config.yaml).
    env_proxies = os.environ.get("FTC_PROXIES", "").strip()
    if env_proxies:
        from_env = [p.strip() for p in env_proxies.split(",") if p.strip()]
        ftc = raw.setdefault("ftc", {})
        existing = ftc.get("proxies") or []
        ftc["proxies"] = list(dict.fromkeys([*existing, *from_env]))

    cfg = AppConfig.model_validate(raw)

    # Merge proxies from optional file (one URL per line, # comments ok).
    proxy_file = os.environ.get("FTC_PROXY_FILE") or cfg.ftc.proxy_file
    if proxy_file:
        pf = Path(proxy_file)
        if not pf.is_absolute():
            pf = PACKAGE_ROOT / pf
        if pf.exists():
            from_file = [
                ln.strip()
                for ln in pf.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            if from_file:
                merged = list(dict.fromkeys([*cfg.ftc.proxies, *from_file]))
                cfg.ftc.proxies = merged

    if path == DEFAULT_CONFIG_PATH or path == Path(os.environ.get("FTC_CONFIG_PATH", "")):
        _cached = cfg
    return cfg
