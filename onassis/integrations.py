"""The Integration Manager — one place to configure, test, monitor and diagnose
every external service ONASSIS depends on.

Business Settings define *how ONASSIS operates*; Integrations define *how ONASSIS
connects to external services*. This module owns the second: a registry of every
supported connector (AI providers, marketplaces, marketing channels, production),
their credential fields, a **real** connection test, health status, and an
activity/audit trail — all operable from the Operations Centre, no SSH.

Credentials are editable from the UI: overrides persist in the ``settings`` table
(namespaced ``integration.<key>.<field>``) and overlay ``.env``/``config.yaml`` at
runtime, so a connector picks up a new key without a redeploy. Secrets are masked
by default, revealed only on explicit request, encrypted at rest when a key +
``cryptography`` are available, and never written to logs. Every save and test is
audited to ``integration_events``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Health states (spec §3).
HEALTHY, WARNING, FAILED, NOT_CONFIGURED = "healthy", "warning", "failed", "not_configured"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = False
    env: str = ""


@dataclass(frozen=True)
class Integration:
    key: str
    label: str
    category: str
    fields: list[Field]
    required: list[str]
    setup: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


# --- Registry ---------------------------------------------------------

REGISTRY: list[Integration] = [
    Integration("anthropic", "Anthropic", "AI",
                [Field("api_key", "API Key", secret=True, env="ANTHROPIC_API_KEY"),
                 Field("model", "Model", env="ONASSIS_LLM_MODEL")],
                required=["api_key"], actions=["test"],
                setup=["Create an Anthropic API key", "Paste it here", "Test Connection"]),
    Integration("openai", "OpenAI", "AI",
                [Field("api_key", "API Key", secret=True, env="OPENAI_API_KEY"),
                 Field("model", "Model", env="")],
                required=["api_key"], actions=["test"],
                setup=["Create an OpenAI API key", "Paste it here", "Test Connection"]),
    Integration("etsy", "Etsy", "Commerce",
                [Field("client_id", "Client ID / Keystring", secret=True, env="ETSY_CLIENT_ID"),
                 Field("client_secret", "Client Secret", secret=True, env="ETSY_CLIENT_SECRET"),
                 Field("shop_id", "Shop ID", env="ETSY_SHOP_ID")],
                required=["client_id"], actions=["test", "reconnect_oauth"],
                setup=["Create an Etsy app", "Copy the keystring + secret",
                       "Paste them here", "Reconnect OAuth", "Test Connection"]),
    Integration("shopify", "Shopify", "Commerce",
                [Field("store_domain", "Store URL", env="SHOPIFY_STORE_DOMAIN"),
                 Field("client_id", "Client ID", secret=True, env="SHOPIFY_CLIENT_ID"),
                 Field("client_secret", "Client Secret", secret=True, env="SHOPIFY_CLIENT_SECRET"),
                 Field("blog_id", "Blog ID", env="")],
                required=["store_domain", "client_id", "client_secret"],
                actions=["test", "publish_test_product", "list_blogs"],
                setup=["Shopify Dev Dashboard → Create an app",
                       "Grant Products + Content scopes; install it on your store",
                       "Copy the app's Client ID + Client Secret",
                       "Paste store URL + Client ID + Client Secret here",
                       "Test Connection — ONASSIS obtains the access token automatically"]),
    Integration("pinterest", "Pinterest", "Marketing",
                [Field("access_token", "Access Token", secret=True, env="PINTEREST_ACCESS_TOKEN"),
                 Field("board_id", "Board ID", env="PINTEREST_BOARD_ID")],
                required=["access_token"], actions=["test"],
                setup=["Create a Pinterest app + access token",
                       "Copy a target board id", "Paste here", "Test Connection"]),
    Integration("facebook", "Facebook", "Marketing",
                [Field("page_access_token", "Page Access Token", secret=True,
                       env="META_PAGE_ACCESS_TOKEN"),
                 Field("facebook_page_id", "Page ID", env="FACEBOOK_PAGE_ID")],
                required=["page_access_token", "facebook_page_id"], actions=["test"],
                setup=["Create a Meta app", "Get a Page access token",
                       "Paste token + page id here", "Test Connection"]),
    Integration("instagram", "Instagram", "Marketing",
                [Field("page_access_token", "Access Token", secret=True,
                       env="META_PAGE_ACCESS_TOKEN"),
                 Field("instagram_user_id", "Business Account ID", env="INSTAGRAM_USER_ID")],
                required=["page_access_token", "instagram_user_id"], actions=["test"],
                setup=["Connect an IG Business account to your Page",
                       "Get the IG user id", "Paste token + id here", "Test Connection"]),
    Integration("email", "Email", "Marketing",
                [Field("smtp_host", "SMTP Host", env="SMTP_HOST"),
                 Field("smtp_port", "SMTP Port", env="SMTP_PORT"),
                 Field("smtp_user", "Username", env="SMTP_USER"),
                 Field("smtp_password", "Password", secret=True, env="SMTP_PASSWORD"),
                 Field("from_address", "From Address", env="EMAIL_FROM"),
                 Field("to_address", "To Address", env="EMAIL_TO")],
                required=["smtp_host", "from_address", "to_address"],
                actions=["test", "send_test"],
                setup=["Get SMTP host/port + credentials from your email provider",
                       "Paste them here", "Test Email"]),
    Integration("gelato", "Gelato", "Production",
                [Field("api_key", "API Key", secret=True, env="GELATO_API_KEY"),
                 Field("file_base_url", "Public File Base URL", env="GELATO_FILE_BASE_URL")],
                required=["api_key"], actions=["test"],
                setup=["Create a Gelato API key", "Set a public /exports base URL",
                       "Paste here", "Test Connection"]),
]
_BY_KEY = {i.key: i for i in REGISTRY}
_SETTING = "integration."


# --- Secret sealing (best-effort encryption at rest) -----------------

def _fernet() -> Any | None:
    secret = os.environ.get("ONASSIS_SECRET_KEY")
    if not secret:
        return None
    try:
        import base64
        import hashlib

        from cryptography.fernet import Fernet
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        return Fernet(key)
    except Exception:  # cryptography not installed / bad key
        return None


def _seal(value: str) -> str:
    f = _fernet()
    if not f or value is None:
        return value
    try:
        return "enc:" + f.encrypt(value.encode()).decode()
    except Exception:
        return value


def _unseal(value: str | None) -> str | None:
    if not value or not str(value).startswith("enc:"):
        return value
    f = _fernet()
    if not f:
        return value            # can't decrypt without the key; leave sealed
    try:
        return f.decrypt(value[4:].encode()).decode()
    except Exception:
        return value


def encryption_available() -> bool:
    return _fernet() is not None


def mask(value: str | None) -> str:
    if value is None or value == "":
        return "—"
    v = str(value)
    return (v[:3] + "…" + v[-2:]) if len(v) > 8 else "set"


# =====================================================================

class IntegrationManager:
    def __init__(self, config: Config, db: Database,
                 testers: dict[str, Callable] | None = None) -> None:
        self.config = config
        self.db = db
        self._testers = testers or _DEFAULT_TESTERS

    # --- Credential resolution --------------------------------------

    def _config_value(self, key: str, field_key: str) -> Any:
        """The .env/config.yaml value for a field (before DB override)."""
        if key == "anthropic":
            return {"api_key": self.config.anthropic_api_key,
                    "model": self.config.llm_model}.get(field_key)
        if key == "openai":
            img = self.config.image or {}
            return {"api_key": img.get("api_key"), "model": img.get("model")}.get(field_key)
        section = getattr(self.config, key, None) or {}
        return section.get(field_key) if isinstance(section, dict) else None

    def _override(self, key: str, field_key: str) -> Any:
        raw = self.db.get_setting(f"{_SETTING}{key}.{field_key}", None)
        return _unseal(raw) if raw is not None else None

    def resolve(self, key: str) -> dict[str, Any]:
        """Current effective values (DB override > env/config) for every field."""
        out: dict[str, Any] = {}
        for f in _BY_KEY[key].fields:
            ov = self._override(key, f.key)
            out[f.key] = ov if ov is not None else self._config_value(key, f.key)
        return out

    def _configured(self, key: str, resolved: dict[str, Any]) -> bool:
        return all(resolved.get(r) for r in _BY_KEY[key].required)

    # --- Health + activity ------------------------------------------

    def _activity(self, key: str) -> dict[str, Any]:
        last_ok = self.db.last_integration_event(key, status="ok")
        last_err = self.db.last_integration_event(key, status="failed")
        last_pub = None
        for platform in ({"etsy": "etsy", "shopify": "shopify"}.get(key),):
            if platform:
                pubs = [p for p in self.db.list_publications()
                        if p.get("platform") == platform and p.get("status") in ("draft", "live", "published")]
                if pubs:
                    last_pub = pubs[0].get("created_at")
        return {
            "last_success": (last_ok or {}).get("created_at"),
            "last_error": (last_err or {}).get("detail") if last_err else None,
            "last_error_at": (last_err or {}).get("created_at"),
            "last_publish": last_pub,
        }

    def _health(self, key: str, resolved: dict[str, Any]) -> str:
        if not self._configured(key, resolved):
            return NOT_CONFIGURED
        last_test = self.db.last_integration_event(key, kind="test")
        if last_test is None:
            return WARNING              # configured but never tested
        last_cred = self.db.last_integration_event(key, kind="credential_update")
        if last_cred and last_cred["id"] > last_test["id"]:
            return WARNING              # credentials changed since the last test
        return HEALTHY if last_test.get("status") == "ok" else FAILED

    # --- Views ------------------------------------------------------

    def _card(self, key: str, *, reveal: bool = False) -> dict[str, Any]:
        integ = _BY_KEY[key]
        resolved = self.resolve(key)
        fields = []
        for f in integ.fields:
            value = resolved.get(f.key)
            fields.append({
                "key": f.key, "label": f.label, "secret": f.secret, "env": f.env,
                "value": (value if (reveal or not f.secret) else mask(value)),
                "set": bool(value),
            })
        return {
            "key": key, "label": integ.label, "category": integ.category,
            "configured": self._configured(key, resolved),
            "health": self._health(key, resolved),
            "fields": fields, "actions": integ.actions, "setup": integ.setup,
            "activity": self._activity(key),
        }

    def describe(self) -> dict[str, Any]:
        cards = [self._card(i.key) for i in REGISTRY]
        by_cat: dict[str, list] = {}
        for c in cards:
            by_cat.setdefault(c["category"], []).append(c)
        summary = {"healthy": 0, "warning": 0, "failed": 0, "not_configured": 0}
        for c in cards:
            summary[c["health"]] = summary.get(c["health"], 0) + 1
        return {"categories": by_cat, "summary": summary,
                "encryption": encryption_available()}

    def detail(self, key: str, *, reveal: bool = False) -> dict[str, Any] | None:
        if key not in _BY_KEY:
            return None
        card = self._card(key, reveal=reveal)
        card["events"] = self.db.list_integration_events(key, limit=25)
        return card

    # --- Test + save ------------------------------------------------

    def test(self, key: str) -> dict[str, Any]:
        if key not in _BY_KEY:
            return {"ok": False, "detail": "Unknown integration."}
        resolved = self.resolve(key)
        if not self._configured(key, resolved):
            result = {"ok": False, "configured": False, "detail": "Not configured."}
        else:
            tester = self._testers.get(key)
            try:
                result = tester(self.config, resolved, self.db) if tester else {
                    "ok": False, "configured": True, "detail": "No test available."}
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "configured": True, "detail": str(exc)}
        self.db.insert_integration_event({
            "integration": key, "kind": "test",
            "status": "ok" if result.get("ok") else "failed",
            "detail": result.get("detail")})
        result["health"] = self._health(key, resolved)
        return result

    def save(self, key: str, values: dict[str, Any], operator: str | None = None) -> dict[str, Any]:
        if key not in _BY_KEY:
            raise ValueError(f"Unknown integration '{key}'.")
        valid = {f.key for f in _BY_KEY[key].fields}
        changed = []
        for fk, val in values.items():
            if fk not in valid:
                raise ValueError(f"Unknown field '{fk}' for {key}.")
            if val is None or val == "":
                continue
            self.db.set_setting(f"{_SETTING}{key}.{fk}", _seal(str(val)), updated_by=operator)
            self._apply_live(key, fk, str(val))     # so live connectors pick it up
            changed.append(fk)
        self.db.insert_integration_event({
            "integration": key, "kind": "credential_update", "status": "ok",
            "detail": f"{operator or 'operator'} updated: {', '.join(changed) or 'nothing'}"})
        return self._card(key)

    def _apply_live(self, key: str, field_key: str, value: str) -> None:
        """Overlay a saved credential onto the live Config so already-built
        connectors use it without a restart (their cfg is the same dict object)."""
        if key == "anthropic":
            if field_key == "api_key":
                self.config.anthropic_api_key = value
            elif field_key == "model":
                self.config.llm_model = value
            return
        if key == "openai":
            img = self.config.image or {}
            img[field_key] = value
            self.config.image = img
            return
        section = getattr(self.config, key, None)
        if isinstance(section, dict):
            section[field_key] = value


# --- Testers (real connection checks) --------------------------------

def _ns(section: str, resolved: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(**{section: dict(resolved)})


def _test_shopify(config, resolved, db):
    from onassis.connectors.shopify import ShopifyConnector
    return ShopifyConnector(_ns("shopify", resolved)).test_connection()


def _test_pinterest(config, resolved, db):
    from onassis.connectors.pinterest import PinterestConnector
    return PinterestConnector(_ns("pinterest", resolved)).test_connection()


def _test_facebook(config, resolved, db):
    from onassis.connectors.social import FacebookPublisher
    return FacebookPublisher(_ns("meta", resolved)).test_connection()


def _test_instagram(config, resolved, db):
    from onassis.connectors.social import InstagramPublisher
    return InstagramPublisher(_ns("meta", resolved)).test_connection()


def _test_email(config, resolved, db):
    from onassis.connectors.email_sender import EmailSender
    return EmailSender(_ns("email", resolved)).test_connection()


def _test_gelato(config, resolved, db):
    from onassis.connectors.gelato import GelatoConnector
    return GelatoConnector(_ns("gelato", resolved), db).test_connection()


def _test_etsy(config, resolved, db):
    if not resolved.get("client_id"):
        return {"ok": False, "configured": False, "detail": "Set the Etsy keystring."}
    try:
        from onassis.connectors.etsy_oauth import build_etsy_oauth
        oauth = build_etsy_oauth(config)
        if getattr(oauth, "is_authorised", False):
            return {"ok": True, "configured": True, "detail": "OAuth token valid."}
        return {"ok": False, "configured": True,
                "detail": "Not authorised — use Reconnect OAuth."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "configured": True, "detail": str(exc)}


def _test_anthropic(config, resolved, db):
    key = resolved.get("api_key")
    if not key:
        return {"ok": False, "configured": False, "detail": "Set ANTHROPIC_API_KEY."}
    try:
        import httpx
        resp = httpx.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": resolved.get("model") or "claude-opus-4-8",
                                "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
                          timeout=20.0)
        if resp.status_code < 400:
            return {"ok": True, "configured": True, "detail": "Anthropic API reachable."}
        return {"ok": False, "configured": True,
                "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "configured": True, "detail": str(exc)}


def _test_openai(config, resolved, db):
    key = resolved.get("api_key")
    if not key:
        return {"ok": False, "configured": False, "detail": "Set OPENAI_API_KEY."}
    try:
        import httpx
        resp = httpx.get("https://api.openai.com/v1/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=20.0)
        if resp.status_code < 400:
            return {"ok": True, "configured": True, "detail": "OpenAI API reachable."}
        return {"ok": False, "configured": True,
                "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "configured": True, "detail": str(exc)}


_DEFAULT_TESTERS: dict[str, Callable] = {
    "anthropic": _test_anthropic, "openai": _test_openai, "etsy": _test_etsy,
    "shopify": _test_shopify, "pinterest": _test_pinterest, "facebook": _test_facebook,
    "instagram": _test_instagram, "email": _test_email, "gelato": _test_gelato,
}


def apply_integration_overrides(config: Config, db: Database) -> None:
    """Overlay operator-saved credentials from the DB onto the live Config at
    startup, so connectors built afterwards use them (no .env edit / restart of
    the whole box needed). Called by create_app before the engines are built."""
    mgr = IntegrationManager(config, db)
    for integ in REGISTRY:
        for f in integ.fields:
            ov = mgr._override(integ.key, f.key)
            if ov is not None:
                mgr._apply_live(integ.key, f.key, ov)
