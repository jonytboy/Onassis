"""Configuration loading for ONASSIS.

Precedence (highest wins):

    1. Environment variables (optionally loaded from a local ``.env``)
    2. Values in ``config.yaml``
    3. Hardcoded fallbacks in this module

The rest of the application only ever talks to the :class:`Config`
object returned by :func:`load_config`. Nothing else should read os.environ
or parse YAML directly — that keeps configuration in exactly one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Repo root = parent of the `onassis` package directory.
ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"


@dataclass
class Config:
    """Typed, validated view over merged YAML + environment settings."""

    # app
    app_name: str
    version: str
    environment: str

    # logging
    log_level: str
    log_dir: Path
    log_file: str
    log_max_bytes: int
    log_backup_count: int

    # database
    db_path: Path

    # scheduler
    run_at: str
    run_on_start: bool

    # llm provider (powers content generation)
    llm_model: str = "claude-opus-4-8"
    llm_effort: str = "high"
    llm_max_tokens: int = 8000

    # brand strategy (passed straight to the Content Director)
    brand: dict[str, Any] = field(default_factory=dict)

    # how much content to produce per brief
    content_targets: dict[str, int] = field(default_factory=dict)

    # governance: CEO company policy + Compliance thresholds
    policy: dict[str, Any] = field(default_factory=dict)
    compliance: dict[str, Any] = field(default_factory=dict)
    profit: dict[str, Any] = field(default_factory=dict)
    etsy: dict[str, Any] = field(default_factory=dict)
    optimiser: dict[str, Any] = field(default_factory=dict)
    listing: dict[str, Any] = field(default_factory=dict)
    publishing: dict[str, Any] = field(default_factory=dict)
    pinterest: dict[str, Any] = field(default_factory=dict)

    # secrets / future integrations
    anthropic_api_key: str | None = None

    def __post_init__(self) -> None:
        # Resolve paths relative to the repo root so the app behaves the
        # same regardless of the current working directory.
        self.db_path = (ROOT_DIR / self.db_path).resolve()
        self.log_dir = (ROOT_DIR / self.log_dir).resolve()


def _env(key: str, default: Any = None) -> Any:
    """Read an env var, treating empty strings as 'unset'."""
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load and merge configuration from YAML + environment.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        A fully populated :class:`Config`.
    """
    # Load .env first so os.environ is populated before we read it.
    load_dotenv(ROOT_DIR / ".env")

    config_path = Path(config_path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    app = raw.get("app", {})
    logging_cfg = raw.get("logging", {})
    database = raw.get("database", {})
    scheduler = raw.get("scheduler", {})
    llm = raw.get("llm", {})
    brand = raw.get("brand", {})
    content_targets = raw.get("content_targets", {})
    policy = raw.get("policy", {})
    compliance = raw.get("compliance", {})
    profit = raw.get("profit", {})
    optimiser = raw.get("optimiser", {})
    listing = raw.get("listing", {})
    publishing = raw.get("publishing", {})
    etsy = raw.get("etsy", {})
    pinterest = raw.get("pinterest", {})
    pinterest = {
        **pinterest,
        "access_token": _env("PINTEREST_ACCESS_TOKEN", pinterest.get("access_token")),
        "ad_account_id": _env("PINTEREST_AD_ACCOUNT_ID", pinterest.get("ad_account_id")),
    }
    # Etsy credentials come from the environment. In Etsy's Open API v3 the
    # "keystring" is both the OAuth client_id and the x-api-key, so ETSY_CLIENT_ID
    # and ETSY_API_KEY are interchangeable — each falls back to the other.
    etsy_client_id = _env("ETSY_CLIENT_ID", _env("ETSY_API_KEY", etsy.get("client_id")))
    etsy = {
        **etsy,
        "client_id": etsy_client_id,
        "client_secret": _env("ETSY_CLIENT_SECRET", etsy.get("client_secret")),
        # x-api-key is the keystring; accept either env var name.
        "api_key": _env("ETSY_API_KEY", etsy_client_id),
        # A pre-supplied access token still works (e.g. for a quick manual test);
        # otherwise the OAuth token store provides it.
        "access_token": _env("ETSY_ACCESS_TOKEN", etsy.get("access_token")),
        "shop_id": _env("ETSY_SHOP_ID", etsy.get("shop_id")),
        "redirect_uri": _env("ETSY_REDIRECT_URI", etsy.get("redirect_uri")),
    }

    # Environment variables win over YAML.
    brand = {**brand, "name": _env("ONASSIS_BRAND_NAME", brand.get("name", "Onassis"))}

    return Config(
        app_name=app.get("name", "ONASSIS"),
        version=app.get("version", "0.1.0"),
        environment=_env("ONASSIS_ENV", app.get("environment", "development")),
        log_level=_env("LOG_LEVEL", logging_cfg.get("level", "INFO")),
        log_dir=logging_cfg.get("directory", "logs"),
        log_file=logging_cfg.get("file", "onassis.log"),
        log_max_bytes=int(logging_cfg.get("max_bytes", 1_048_576)),
        log_backup_count=int(logging_cfg.get("backup_count", 5)),
        db_path=_env("ONASSIS_DB_PATH", database.get("path", "data/onassis.db")),
        run_at=_env("ONASSIS_RUN_AT", scheduler.get("run_at", "08:00")),
        run_on_start=bool(scheduler.get("run_on_start", True)),
        llm_model=_env("ONASSIS_LLM_MODEL", llm.get("model", "claude-opus-4-8")),
        llm_effort=_env("ONASSIS_LLM_EFFORT", llm.get("effort", "high")),
        llm_max_tokens=int(_env("ONASSIS_LLM_MAX_TOKENS", llm.get("max_tokens", 8000))),
        brand=brand,
        content_targets=content_targets,
        policy=policy,
        compliance=compliance,
        profit=profit,
        etsy=etsy,
        optimiser=optimiser,
        listing=listing,
        publishing=publishing,
        pinterest=pinterest,
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
    )
