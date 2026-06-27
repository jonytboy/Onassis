"""Pinterest analytics connector (read-only).

Emits canonical metric snapshots (impressions, saves, outbound clicks, CTR)
for the Analytics Collector. It implements the same ``fetch_metrics()``
contract as the Etsy source, so it plugs in without changing the engine.

Real Pinterest access needs credentials (``PINTEREST_ACCESS_TOKEN``,
``PINTEREST_AD_ACCOUNT_ID``); without them this is a safe no-op (returns no
metrics). A live implementation would map the Pinterest v5 analytics response
onto the same metric rows.
"""

from __future__ import annotations

from typing import Any

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


class PinterestConnector:
    """Read-only Pinterest analytics source for the collector."""

    name = "pinterest"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.cfg = config.pinterest or {}

    @property
    def is_configured(self) -> bool:
        return bool(self.cfg.get("access_token"))

    def fetch_metrics(self) -> list[dict[str, Any]]:
        if not self.is_configured:
            return []  # safe no-op until credentials are provided
        # A live implementation would call the Pinterest v5 analytics API here
        # and map the response onto metric rows (impressions/saves/outbound
        # clicks/ctr). Kept out of scope (no credentials available).
        log.info("Pinterest configured but live fetch is not implemented yet.")
        return []
