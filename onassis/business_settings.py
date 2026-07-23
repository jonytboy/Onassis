"""Business Settings — operator-editable configuration (Sprint 40, Objective 7).

The Operations Centre must let the operator tune how the business runs *without
editing config.yaml and redeploying*. This module defines the small set of
commercial dials the operator controls, resolves their current value (a DB
override on top of the config.yaml default), validates edits, and persists them.

Each setting has a typed schema so the API can validate input and the UI can
render the right control. Reads are cheap and safe; an unknown/absent override
falls back to the config.yaml value, so the system behaves identically until the
operator deliberately changes something.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

_SETTING_PREFIX = "business."


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str                       # "int" | "float" | "bool"
    default: Callable[[Any], Any]   # resolves the config.yaml default
    group: str = "General"
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""


def _cfg(config: Any, section: str, name: str, fallback: Any) -> Any:
    sec = getattr(config, section, None) or {}
    if isinstance(sec, dict):
        return sec.get(name, fallback)
    return fallback


# The commercial dials the operator controls, in display order.
SCHEMA: list[Setting] = [
    Setting("products_per_campaign", "Products per Campaign", "int",
            lambda c: int(_cfg(c, "expansion", "max_products", 3)),
            group="Volume", minimum=1, maximum=10,
            help="How many product variants each new campaign launches."),
    Setting("research_opportunities", "Research Opportunities", "int",
            lambda c: int(_cfg(c, "research", "opportunities", 5)),
            group="Volume", minimum=1, maximum=25,
            help="Opportunities scored per research run."),
    Setting("max_campaigns_per_day", "Max Campaigns / Day", "int",
            lambda c: int(_cfg(c, "portfolio", "max_new_listings_per_day", 2)),
            group="Volume", minimum=0, maximum=20,
            help="Daily cap on new product launches (protects the shop)."),
    Setting("auto_approval_threshold", "Auto-Approval Threshold", "float",
            lambda c: float(_cfg(c, "compliance", "auto_approve_confidence", 0.85)),
            group="Approvals", minimum=0.0, maximum=1.0,
            help="Confidence at/above which a product is auto-approved."),
    Setting("auto_publish", "Auto-Publish Approved", "bool",
            lambda c: bool((getattr(c, "launch", None) or {}).get("auto_go_live", False)),
            group="Approvals",
            help="Publish approved products to Etsy automatically."),
    Setting("marketing_enabled", "Marketing", "bool",
            lambda c: bool(_cfg(c, "marketing", "enabled", True)),
            group="Channels", help="Generate marketing content."),
    Setting("pinterest_enabled", "Pinterest", "bool",
            lambda c: bool((getattr(c, "pinterest", None) or {}).get("enabled", True)),
            group="Channels", help="Schedule and post pins."),
    Setting("pinterest_daily_pins", "Pinterest Pins / Day", "int",
            lambda c: int((getattr(c, "traffic", None) or {}).get("max_pins_per_day", 10)),
            group="Channels",
            help="How many pins to post per day, cycling through all products."),
    Setting("email_enabled", "Email", "bool",
            lambda c: bool(_cfg(c, "marketing", "email_enabled", True)),
            group="Channels", help="Produce email marketing assets."),
    Setting("facebook_enabled", "Facebook", "bool",
            lambda c: bool(_cfg(c, "marketing", "facebook_enabled", False)),
            group="Channels", help="Produce Facebook marketing assets."),
]
_BY_KEY = {s.key: s for s in SCHEMA}


def _coerce(setting: Setting, value: Any) -> Any:
    """Validate + coerce a raw input to the setting's type, or raise ValueError."""
    if setting.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    try:
        num = int(value) if setting.kind == "int" else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{setting.key} must be a {setting.kind}.")
    if setting.minimum is not None and num < setting.minimum:
        raise ValueError(f"{setting.key} must be >= {setting.minimum}.")
    if setting.maximum is not None and num > setting.maximum:
        raise ValueError(f"{setting.key} must be <= {setting.maximum}.")
    return num


class BusinessSettings:
    """Resolves and persists operator-editable settings against the Database."""

    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config

    def get(self, key: str) -> Any:
        setting = _BY_KEY[key]
        return self.db.get_setting(_SETTING_PREFIX + key, setting.default(self.config))

    def all(self) -> dict[str, Any]:
        return {s.key: self.get(s.key) for s in SCHEMA}

    def describe(self) -> list[dict[str, Any]]:
        """The settings with their current value + schema, for the UI."""
        out = []
        for s in SCHEMA:
            out.append({
                "key": s.key, "label": s.label, "kind": s.kind, "group": s.group,
                "value": self.get(s.key), "default": s.default(self.config),
                "min": s.minimum, "max": s.maximum, "help": s.help,
            })
        return out

    def update(self, changes: dict[str, Any], operator: str | None = None) -> dict[str, Any]:
        """Validate and persist a batch of changes. Raises ValueError on the
        first invalid key/value (nothing is written if validation fails)."""
        coerced: dict[str, Any] = {}
        for key, raw in changes.items():
            if key not in _BY_KEY:
                raise ValueError(f"Unknown setting '{key}'.")
            coerced[key] = _coerce(_BY_KEY[key], raw)
        for key, value in coerced.items():
            self.db.set_setting(_SETTING_PREFIX + key, value, updated_by=operator)
        return self.all()
