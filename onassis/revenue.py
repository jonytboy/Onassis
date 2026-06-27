"""The Revenue Intelligence Engine — ONASSIS's financial source of truth.

ONASSIS must know exactly how much money it is making at all times. This engine
records every **order** (a sale) with its full cost breakdown, computes the
economics for each one, and rolls them up by day, month, and overall.

Entities:
    * Order   — a sale, with sale price, quantity, platform, and cost breakdown.
    * Product — the catalogue item an order refers to.
    * Campaign — already a first-class object; orders link to it.
    * Revenue / Cost / Profit — computed from orders (per order and aggregate).

Each order automatically calculates Gross Revenue, Gross Profit, Net Profit,
Profit Margin, and ROI (see :func:`compute_order_metrics`).

Orders **mirror** into the Sprint-8 ledger, so the company-wide ledger remains
the unified cash record the CEO/Profit Engine already use — meaning the CEO
keeps deciding on **net profit**, now driven by real sales.

Connector-ready: marketplaces and ad platforms (Etsy, Gelato, Pinterest, …)
plug in by implementing :class:`~onassis.connectors.base.RevenueConnector` to
emit canonical order dicts. The engine ingests them unchanged — no core edits.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.profit import ProfitEngine

log = get_logger(__name__)

# The six cost components every order may carry.
COST_FIELDS = (
    "ai_cost",
    "advertising_cost",
    "production_cost",
    "marketplace_fees",
    "payment_fees",
    "other_costs",
)

# Map each order cost field to a ledger category, so order costs roll up into
# the company ledger by the same categories the Profit Engine reports.
_COST_TO_LEDGER_CATEGORY = {
    "ai_cost": "ai",
    "advertising_cost": "advertising",
    "production_cost": "cogs",
    "marketplace_fees": "marketplace",
    "payment_fees": "payment",
    "other_costs": "other",
}


def compute_order_metrics(order: dict[str, Any]) -> dict[str, float]:
    """Compute an order's economics. Pure function — fully unit-tested.

    * Gross Revenue = sale_price × quantity
    * Total Cost    = sum of the six cost components
    * Gross Profit  = Gross Revenue − production cost (COGS)
    * Net Profit    = Gross Revenue − Total Cost
    * Profit Margin = Net Profit / Gross Revenue
    * ROI           = Net Profit / Total Cost
    """
    qty = int(order.get("quantity", 1) or 1)
    price = float(order.get("sale_price", 0) or 0)
    gross_revenue = price * qty

    production = float(order.get("production_cost", 0) or 0)
    total_cost = sum(float(order.get(f, 0) or 0) for f in COST_FIELDS)

    gross_profit = gross_revenue - production
    net_profit = gross_revenue - total_cost
    profit_margin = (net_profit / gross_revenue) if gross_revenue > 0 else 0.0
    roi = (net_profit / total_cost) if total_cost > 0 else 0.0

    return {
        "gross_revenue": round(gross_revenue, 2),
        "total_cost": round(total_cost, 2),
        "gross_profit": round(gross_profit, 2),
        "net_profit": round(net_profit, 2),
        "profit_margin": round(profit_margin, 4),
        "roi": round(roi, 4),
    }


class RevenueEngine:
    """Records orders, computes economics, and reports revenue & profit."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.profit = ProfitEngine(config, db)

    # --- Recording --------------------------------------------------

    def record_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Persist a sale (with computed economics) and mirror it to the ledger."""
        occurred_at = order.get("occurred_at") or datetime.now(timezone.utc).isoformat()
        sale_date = order.get("sale_date") or occurred_at[:10]
        metrics = compute_order_metrics(order)

        row = {**order, "occurred_at": occurred_at, "sale_date": sale_date, **metrics}
        order_id = self.db.insert_order(row)
        row["id"] = order_id

        self._mirror_to_ledger(row)
        log.info("Recorded order #%s: net profit %.2f", order_id, metrics["net_profit"])
        return row

    def _mirror_to_ledger(self, order: dict[str, Any]) -> None:
        """Reflect the order's revenue and costs into the company ledger."""
        common = {
            "campaign_id": order.get("campaign_id"),
            "product_id": order.get("product_id"),
            "brand": order.get("brand"),
            "marketplace": order.get("platform"),
            "entry_date": order["sale_date"],
        }
        if order["gross_revenue"]:
            self.profit.record_revenue(
                order["gross_revenue"], category="sale",
                note=f"order #{order['id']}", **common
            )
        for field, category in _COST_TO_LEDGER_CATEGORY.items():
            amount = float(order.get(field, 0) or 0)
            if amount:
                self.profit.record_cost(
                    amount, category=category, note=f"order #{order['id']} {field}", **common
                )

    def ingest(self, connector: Any) -> int:
        """Pull orders from a connector and record them. Returns the count.

        ``connector`` only needs a ``fetch_orders() -> list[dict]`` method, so
        Etsy/Gelato/Pinterest/ad connectors plug in without touching the engine.
        """
        orders = connector.fetch_orders()
        for order in orders:
            self.record_order(order)
        name = getattr(connector, "name", connector.__class__.__name__)
        log.info("Ingested %d order(s) from %s", len(orders), name)
        return len(orders)

    # --- Products ---------------------------------------------------

    def register_product(self, product: dict[str, Any]) -> dict[str, Any]:
        product_id = self.db.insert_product(product)
        stored = self.db.get_product(product_id)
        assert stored is not None
        return stored

    def list_products(self) -> list[dict[str, Any]]:
        return self.db.list_products()

    # --- Reads & rollups --------------------------------------------

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        return self.db.get_order(order_id)

    def list_orders(self) -> list[dict[str, Any]]:
        return self.db.list_orders()

    def revenue_today(self, today: str | None = None) -> dict[str, Any]:
        day = today or date.today().isoformat()
        return {"period": day, **self._aggregate(self.db.get_orders_on(day))}

    def revenue_month(self, year_month: str | None = None) -> dict[str, Any]:
        ym = year_month or date.today().strftime("%Y-%m")
        return {"period": ym, **self._aggregate(self.db.get_orders_in_month(ym))}

    def company_profit(self) -> dict[str, Any]:
        """The bottom line from the unified ledger (orders + all other costs)."""
        revenue = self.db.total_revenue()
        cost = self.db.total_cost()
        net = revenue - cost
        return {
            "gross_revenue": round(revenue, 2),
            "total_cost": round(cost, 2),
            "net_profit": round(net, 2),
            "profit_margin": round(net / revenue, 4) if revenue > 0 else 0.0,
            "roi": round(net / cost, 4) if cost > 0 else 0.0,
            "cash_balance": round(self.profit.cash_balance(), 2),
            "orders": len(self.db.list_orders()),
        }

    def _aggregate(self, orders: list[dict[str, Any]]) -> dict[str, Any]:
        """Sum a set of orders into a period revenue/profit summary."""
        gross_revenue = sum(o["gross_revenue"] for o in orders)
        total_cost = sum(o["total_cost"] for o in orders)
        gross_profit = sum(o["gross_profit"] for o in orders)
        net_profit = sum(o["net_profit"] for o in orders)
        return {
            "orders": len(orders),
            "units": sum(int(o["quantity"]) for o in orders),
            "gross_revenue": round(gross_revenue, 2),
            "total_cost": round(total_cost, 2),
            "gross_profit": round(gross_profit, 2),
            "net_profit": round(net_profit, 2),
            "profit_margin": round(net_profit / gross_revenue, 4) if gross_revenue > 0 else 0.0,
            "roi": round(net_profit / total_cost, 4) if total_cost > 0 else 0.0,
        }
