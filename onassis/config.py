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
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
    )
