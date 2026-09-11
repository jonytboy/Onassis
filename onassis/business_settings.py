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
    Setting("personaliser_test_mode", "Personaliser test mode", "bool",
            lambda c: bool(_cfg(c, "personaliser", "test_mode", False)),
            group="Personaliser",
            help="Accept the order code DEMO to try the buyer flow without a "
                 "purchase. Turn OFF before going live."),
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
    Setting("pinterest_via_make", "Pinterest via Make/Buffer", "bool",
            lambda c: bool((getattr(c, "pinterest", None) or {}).get("via_make", False)),
            group="Channels",
            help="Post pins through the Make webhook (which has production access) "
                 "instead of the Pinterest API — bypasses Trial-access limits."),
    Setting("email_enabled", "Email", "bool",
            lambda c: bool(_cfg(c, "marketing", "email_enabled", True)),
            group="Channels", help="Produce email marketing assets."),
    Setting("facebook_enabled", "Facebook", "bool",
            lambda c: bool(_cfg(c, "marketing", "facebook_enabled", False)),
            group="Channels", help="Produce Facebook marketing assets."),
    Setting("tiktok_enabled", "TikTok", "bool",
            lambda c: bool(_cfg(c, "marketing", "tiktok_enabled", False)),
            group="Channels", help="Publish product videos to TikTok."),
    Setting("tiktok_via_make", "TikTok via Make/Buffer", "bool",
            lambda c: bool(_cfg(c, "marketing", "tiktok_via_make", False)),
            group="Channels",
            help="Send TikTok videos to the Make.com webhook (→ Buffer → TikTok) "
                 "instead of the direct TikTok API."),
    Setting("marketing_every_hours", "Run marketing every N hours", "int",
            lambda c: int(_cfg(c, "scheduler", "every_hours", 0)),
            group="Schedule", minimum=0, maximum=24,
            help="How often the in-app scheduler runs the marketing push (posts "
                 "blogs + reels, cycling the catalogue). e.g. 3 = every 3 hours "
                 "(~8×/day). 0 = off (use an external cron instead). No crontab "
                 "editing — this is the whole schedule control."),
    Setting("evergreen_enabled", "Evergreen recycling", "bool",
            lambda c: bool(_cfg(c, "content", "evergreen_enabled", True)),
            group="Channels",
            help="Re-post existing reels and videos on a rotation, cycling through "
                 "all products and looping back to the start. Turn off to only post "
                 "content when a new product launches."),
    Setting("evergreen_reels_per_run", "Evergreen reels / run", "int",
            lambda c: int(_cfg(c, "content", "evergreen_reels_per_run", 1)),
            group="Channels", minimum=0, maximum=20,
            help="How many reels to post per marketing run, cycling through all "
                 "products (oldest-posted first) and looping back to the start. "
                 "The frequency is the cron schedule — 1/run + an every-few-hours "
                 "cron gives an every-few-hours drip. 0 = off."),
    Setting("blogs_per_run", "Blog posts / run", "int",
            lambda c: int(_cfg(c, "content", "blogs_per_run", 1)),
            group="Channels", minimum=0, maximum=20,
            help="Cap how many blog articles publish to Shopify per marketing run, "
                 "so a backlog drips out instead of dumping all at once. 1/run + an "
                 "every-3-hours cron = at most a few a day. 0 = don't publish blogs "
                 "on the scheduled run."),
    # --- Pricing (cost-plus / adaptive price discovery) ---
    Setting("pricing_adaptive", "Adaptive pricing", "bool",
            lambda c: str(_cfg(c, "pricing", "strategy", "")).lower() == "adaptive",
            group="Pricing",
            help="Price discovery: start at a margin, walk DOWN on no-sales and UP "
                 "on sales. Off = a fixed thin margin."),
    Setting("adaptive_start_profit", "Start profit (£/sale)", "float",
            lambda c: float(_cfg(c, "pricing", "adaptive_start_profit", 3.0)),
            group="Pricing", minimum=0, maximum=50,
            help="New products launch netting ~£this per sale, after fees + shipping."),
    Setting("adaptive_min_profit", "Floor profit (£/sale)", "float",
            lambda c: float(_cfg(c, "pricing", "adaptive_min_profit", 0.5)),
            group="Pricing", minimum=0, maximum=50,
            help="Hard floor — never price below cost + shipping + fees + this."),
    Setting("adaptive_max_profit", "Ceiling profit (£/sale)", "float",
            lambda c: float(_cfg(c, "pricing", "adaptive_max_profit", 8.0)),
            group="Pricing", minimum=0, maximum=100,
            help="The margin can climb to this while a product keeps selling."),
    Setting("adaptive_step", "Adjust step (£)", "float",
            lambda c: float(_cfg(c, "pricing", "adaptive_step", 0.5)),
            group="Pricing", minimum=0, maximum=20,
            help="How much £ the target margin moves each adjustment."),
    Setting("adaptive_window_days", "Adjust every N days", "int",
            lambda c: int(_cfg(c, "pricing", "adaptive_window_days", 7)),
            group="Pricing", minimum=1, maximum=90,
            help="Only move a product's price once per this many days (no thrashing)."),
    Setting("shipping_cost", "Gelato shipping (£/item)", "float",
            lambda c: float(_cfg(c, "pricing", "shipping_cost", 5.0)),
            group="Pricing", minimum=0, maximum=50,
            help="Your REAL Gelato per-item ship cost — the thin margin is a loss if "
                 "this is wrong. The single most important pricing number."),
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
