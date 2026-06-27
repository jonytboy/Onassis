"""Connector interface for revenue sources.

A connector's only job is to produce **canonical order dicts** that the
:class:`~onassis.revenue.RevenueEngine` understands. Concrete connectors for
Etsy, Gelato, Pinterest, and advertising platforms will subclass this and map
their own API payloads onto the canonical shape — the engine never changes.

Canonical order dict keys (all optional unless noted):
    order_ref, occurred_at (ISO; defaults to now), product_id, campaign_id,
    platform, sale_price (required), currency, quantity, and any of the cost
    components: ai_cost, advertising_cost, production_cost, marketplace_fees,
    payment_fees, other_costs.
"""

from __future__ import annotations

import abc
from typing import Any


class RevenueConnector(abc.ABC):
    """Base class for all revenue-source connectors."""

    #: Human-readable connector name (e.g. "etsy", "gelato").
    name: str = "connector"

    @abc.abstractmethod
    def fetch_orders(self) -> list[dict[str, Any]]:
        """Return new orders as canonical order dicts (may be empty)."""
        raise NotImplementedError


class ManualConnector(RevenueConnector):
    """A trivial connector that replays a fixed list of orders.

    Useful for manual imports, backfills, and tests. Real connectors follow the
    same contract but fetch from an external API instead.
    """

    name = "manual"

    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self._orders = orders

    def fetch_orders(self) -> list[dict[str, Any]]:
        return list(self._orders)
