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


def runtime_root() -> Path:
    """Where mutable runtime state (db, logs, backups, exports) lives.

    Defaults to the repo root for backwards-compatible local/dev runs, but
    setting ``ONASSIS_RUNTIME_DIR`` moves ALL runtime assets outside the Git
    checkout (Sprint 40.1: application code becomes read-only; runtime state is
    separated from source so a deploy/rollback never touches business data)."""
    override = os.environ.get("ONASSIS_RUNTIME_DIR")
    return Path(override).expanduser().resolve() if override else ROOT_DIR


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
    opportunity: dict[str, Any] = field(default_factory=dict)
    design: dict[str, Any] = field(default_factory=dict)
    expansion: dict[str, Any] = field(default_factory=dict)
    launch: dict[str, Any] = field(default_factory=dict)
    image: dict[str, Any] = field(default_factory=dict)
    fees: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    market: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    thumbnails: dict[str, Any] = field(default_factory=dict)
    marketing: dict[str, Any] = field(default_factory=dict)
    traffic: dict[str, Any] = field(default_factory=dict)
    gelato: dict[str, Any] = field(default_factory=dict)
    protection: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    # Sprint 41 — commerce reach: a second sales channel + social/email/blog.
    shopify: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    email: dict[str, Any] = field(default_factory=dict)
    # Sprint 43 — Make.com single marketing distribution engine.
    make: dict[str, Any] = field(default_factory=dict)
    # Sprint 42.2 — AI pricing overrides for cost accounting (optional).
    ai_pricing: dict[str, Any] = field(default_factory=dict)

    # secrets / future integrations
    anthropic_api_key: str | None = None

    def __post_init__(self) -> None:
        # Resolve runtime paths under the runtime root (repo root by default,
        # or ONASSIS_RUNTIME_DIR when runtime is kept outside the checkout). An
        # absolute db_path/log_dir (e.g. ONASSIS_DB_PATH=/var/lib/...) is honoured
        # as-is.
        base = runtime_root()
        self.runtime_dir = base
        db = Path(self.db_path)
        self.db_path = db.resolve() if db.is_absolute() else (base / db).resolve()
        log = Path(self.log_dir)
        self.log_dir = log.resolve() if log.is_absolute() else (base / log).resolve()


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
    opportunity = raw.get("opportunity", {})
    design = raw.get("design", {})
    expansion = raw.get("expansion", {})
    launch = raw.get("launch", {})
    image = raw.get("image", {})
    image = {
        **image,
        "api_key": _env("IMAGE_API_KEY", _env("OPENAI_API_KEY", image.get("api_key"))),
    }
    fees = raw.get("fees", {})
    report = raw.get("report", {})
    market = raw.get("market", {})
    portfolio = raw.get("portfolio", {})
    pricing = raw.get("pricing", {})
    thumbnails = raw.get("thumbnails", {})
    marketing = raw.get("marketing", {})
    traffic = raw.get("traffic", {})
    gelato = raw.get("gelato", {})
    gelato = {
        **gelato,
        "api_key": _env("GELATO_API_KEY", gelato.get("api_key")),
        "file_base_url": _env("GELATO_FILE_BASE_URL", gelato.get("file_base_url")),
    }
    # Shopify — second sales channel (products published alongside Etsy). New
    # Dev Dashboard apps issue a Client ID/Secret and use the client-credentials
    # grant; a legacy static Admin token is still honoured when present.
    shopify = raw.get("shopify", {})
    shopify = {
        **shopify,
        "store_domain": _env("SHOPIFY_STORE_DOMAIN", shopify.get("store_domain")),
        "client_id": _env("SHOPIFY_CLIENT_ID", shopify.get("client_id")),
        "client_secret": _env("SHOPIFY_CLIENT_SECRET", shopify.get("client_secret")),
        "admin_token": _env("SHOPIFY_ADMIN_TOKEN", shopify.get("admin_token")),  # legacy
        "api_version": _env("SHOPIFY_API_VERSION", shopify.get("api_version", "2024-10")),
        "location_id": _env("SHOPIFY_LOCATION_ID", shopify.get("location_id")),
    }
    # Meta (Instagram + Facebook publishing via the Graph API).
    meta = raw.get("meta", {})
    meta = {
        **meta,
        "page_access_token": _env("META_PAGE_ACCESS_TOKEN", meta.get("page_access_token")),
        "facebook_page_id": _env("FACEBOOK_PAGE_ID", meta.get("facebook_page_id")),
        "instagram_user_id": _env("INSTAGRAM_USER_ID", meta.get("instagram_user_id")),
        "api_version": _env("META_API_VERSION", meta.get("api_version", "v21.0")),
    }
    # Email newsletter (SMTP).
    email = raw.get("email", {})
    email = {
        **email,
        "smtp_host": _env("SMTP_HOST", email.get("smtp_host")),
        "smtp_port": int(_env("SMTP_PORT", email.get("smtp_port", 587))),
        "smtp_user": _env("SMTP_USER", email.get("smtp_user")),
        "smtp_password": _env("SMTP_PASSWORD", email.get("smtp_password")),
        "from_address": _env("EMAIL_FROM", email.get("from_address")),
        "to_address": _env("EMAIL_TO", email.get("to_address")),
        "use_tls": bool(email.get("use_tls", True)),
    }
    # Make.com single marketing distribution webhook (Sprint 43).
    make = raw.get("make", {})
    make = {
        **make,
        "webhook_url": _env("MAKE_WEBHOOK_URL", make.get("webhook_url")),
        "api_key": _env("MAKE_API_KEY", make.get("api_key")),
        "file_base_url": _env("MAKE_FILE_BASE_URL", make.get("file_base_url")),
    }
    # Financial protection: only the CORE commercial controls come from the env;
    # all fee/estimation/business logic stays in the YAML `protection` section.
    security = raw.get("security", {})
    security = {
        **security,
        # The API key comes from the env; everything else stays in config.yaml.
        "api_key": _env("ONASSIS_API_KEY", security.get("api_key")),
    }
    protection = raw.get("protection", {})
    protection = {
        **protection,
        "min_gross_margin_percent": float(_env(
            "MIN_GROSS_MARGIN_PERCENT", protection.get("min_gross_margin_percent", 25))),
        "min_contribution_margin_percent": float(_env(
            "MIN_CONTRIBUTION_MARGIN_PERCENT",
            protection.get("min_contribution_margin_percent", 18))),
        "default_risk_reserve_percent": float(_env(
            "DEFAULT_RISK_RESERVE_PERCENT",
            protection.get("default_risk_reserve_percent", 8))),
        "max_risk_reserve_percent": float(_env(
            "MAX_RISK_RESERVE_PERCENT", protection.get("max_risk_reserve_percent", 15))),
        "max_single_price_change_percent": float(_env(
            "MAX_SINGLE_PRICE_CHANGE_PERCENT",
            protection.get("max_single_price_change_percent", 15))),
        "min_confidence_score": float(_env(
            "MIN_CONFIDENCE_SCORE", protection.get("min_confidence_score", 0.85))),
    }
    etsy = raw.get("etsy", {})
    pinterest = raw.get("pinterest", {})
    pinterest = {
        **pinterest,
        "access_token": _env("PINTEREST_ACCESS_TOKEN", pinterest.get("access_token")),
        "ad_account_id": _env("PINTEREST_AD_ACCOUNT_ID", pinterest.get("ad_account_id")),
        "board_id": _env("PINTEREST_BOARD_ID", pinterest.get("board_id")),
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
        opportunity=opportunity,
        design=design,
        expansion=expansion,
        launch=launch,
        image=image,
        fees=fees,
        report=report,
        market=market,
        portfolio=portfolio,
        pricing=pricing,
        thumbnails=thumbnails,
        marketing=marketing,
        traffic=traffic,
        gelato=gelato,
        protection=protection,
        security=security,
        shopify=shopify,
        meta=meta,
        email=email,
        make=make,
        ai_pricing=raw.get("ai_pricing", {}),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
    )
