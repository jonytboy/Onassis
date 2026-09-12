"""The Gelato Fulfilment Engine — remove the human from fulfilment.

When an Etsy sale is paid, ONASSIS must turn it into a real production order
without anyone touching it. This module does exactly that:

* :class:`GelatoClient` — the only place that talks HTTP to Gelato's Order API
  v4 (create an order, read its status/tracking/cost). Injectable, so tests use
  a stub and never hit the network.
* :class:`GelatoConnector` — maps a paid Etsy order onto a Gelato order (product
  UID from the catalogue, the print file, the buyer's address), submits it with
  bounded retries, then polls production status + tracking and records the
  **actual** production cost — booking the true-vs-estimate difference to the
  ledger so "profit" reflects what fulfilment really cost.

Every order is fulfilled at most once (``order_ref`` is unique). A failure is
isolated and retried next cycle; it never blocks other orders.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Gelato order/fulfilment statuses mapped onto our lifecycle. Anything not listed
# is treated as an in-flight "in_production".
_TERMINAL = {"shipped", "delivered", "canceled", "cancelled", "failed"}
_STATUS_MAP = {
    "created": "created", "passed": "in_production", "in_production": "in_production",
    "printed": "in_production", "printing": "in_production", "shipped": "shipped",
    "delivered": "delivered", "canceled": "canceled", "cancelled": "canceled",
    "failed": "failed", "on_hold": "in_production", "draft": "created",
    "pending_approval": "in_production", "not_connected": "in_production",
}


class GelatoError(RuntimeError):
    """Raised on a Gelato API 4xx/5xx — carries the response body."""


class GelatoClient:
    """Minimal Gelato Order API v4 client (create + read). Injectable for tests."""

    def __init__(self, api_key: str, *,
                 base_url: str = "https://order.gelatoapis.com/v4",
                 timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        resp = httpx.post(f"{self.base_url}/orders", headers=self._headers(),
                          json=payload, timeout=self.timeout)
        if resp.status_code >= 400:
            raise GelatoError(f"Gelato createOrder HTTP {resp.status_code}: {resp.text}")
        return resp.json()

    def get_order(self, gelato_order_id: str) -> dict[str, Any]:
        import httpx

        resp = httpx.get(f"{self.base_url}/orders/{gelato_order_id}",
                         headers=self._headers(), timeout=self.timeout)
        if resp.status_code >= 400:
            raise GelatoError(f"Gelato getOrder HTTP {resp.status_code}: {resp.text}")
        return resp.json()


class GelatoConnector:
    """Submits paid Etsy orders to Gelato and tracks them to true cost."""

    name = "gelato"

    def __init__(self, config: Config, db: Database, client: Any | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = getattr(config, "gelato", None) or {}
        self._client = client
        self.max_retries = int(self.cfg.get("max_retries", 3))
        self.retry_backoff = float(self.cfg.get("retry_backoff_seconds", 0))
        self.currency = self.cfg.get("currency", "GBP")
        # Public base URL Gelato can fetch print files from (e.g. a CDN / the API
        # server serving the exports dir). Without it, orders cannot be fulfilled.
        self.file_base_url = (self.cfg.get("file_base_url") or "").rstrip("/")
        self.customer_ref = self.cfg.get("customer_reference_id", "onassis")

    # --- Configuration / client -------------------------------------

    @property
    def is_configured(self) -> bool:
        return self._client is not None or bool(self.cfg.get("api_key"))

    @property
    def can_fulfil(self) -> bool:
        """Fulfilment also needs a public print-file base URL for Gelato to fetch."""
        return self.is_configured and bool(self._client or self.file_base_url)

    def test_connection(self) -> dict[str, Any]:
        """Validate API access by reading the Gelato product catalogue (read-only)."""
        if not self.cfg.get("api_key"):
            return {"ok": False, "configured": False,
                    "detail": "Set GELATO_API_KEY (+ GELATO_FILE_BASE_URL to fulfil)."}
        try:
            import httpx

            resp = httpx.get("https://product.gelatoapis.com/v3/catalogs",
                             headers={"X-API-KEY": self.cfg.get("api_key")}, timeout=15.0)
            if resp.status_code >= 400:
                return {"ok": False, "configured": True,
                        "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            return {"ok": True, "configured": True, "detail": "Gelato API reachable"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "detail": str(exc)}

    @property
    def client(self) -> Any:
        if self._client is None:
            api_key = self.cfg.get("api_key")
            if not api_key:
                raise GelatoError("Gelato API key not set (GELATO_API_KEY).")
            self._client = GelatoClient(
                api_key, base_url=self.cfg.get("base_url", "https://order.gelatoapis.com/v4"))
        return self._client

    # --- Catalogue mapping ------------------------------------------

    def _gelato_uid(self, product_key: str | None) -> str | None:
        for item in (self.config.expansion or {}).get("catalogue", []) or []:
            if item.get("key") == product_key:
                return item.get("gelato_uid")
        return None

    def _resolve_product(self, order: dict[str, Any]) -> dict[str, Any] | None:
        """From an order, resolve its product (sku, product_key, campaign, cost)."""
        listing_id = order.get("product_id")
        pub = self.db.get_publication_by_listing_id(str(listing_id)) if listing_id else None
        sku = (pub or {}).get("product_id") or listing_id
        product = self.db.get_product_by_sku(str(sku)) if sku else None
        if not product:
            return None
        return product

    def _print_file_url(self, campaign_id: Any, product_key: str) -> str | None:
        if self._client is not None and not self.file_base_url:
            # In tests a stub client accepts any URL; use a deterministic path.
            return f"file://exports/{campaign_id}/{product_key}/print_file.png"
        if not self.file_base_url:
            return None
        return f"{self.file_base_url}/{campaign_id}/{product_key}/print_file.png"

    def _is_personaliser_product(self, sku: str | None) -> tuple[bool, str | None]:
        """Check if a product SKU is a personaliser print product.
        Returns (is_personaliser, personaliser_product_key) or (False, None)."""
        if not sku:
            return False, None
        if sku.startswith("personaliser-") and sku.endswith("-print"):
            # Extract the product key: "personaliser-<key>-print"
            key = sku[len("personaliser-"):-len("-print")]
            return True, key
        return False, None

    def _personaliser_print_file(self, order: dict[str, Any]) -> str | None:
        """Get the print file for a personaliser order from the buyer's session.
        Returns the file path if ready, None if still waiting."""
        ref = order.get("order_ref")
        if not ref:
            return None
        session = self.db.find_personaliser_session_for_order(ref)
        if not session:
            return None
        # Only submit if the buyer has finished personalizing.
        if session.get("status") != "done":
            return None
        return session.get("file_path")

    # --- Submit -----------------------------------------------------

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Turn one paid order into a Gelato production order (idempotent)."""
        ref = order.get("order_ref")
        if ref and self.db.get_fulfilment(ref):
            return {"status": "skipped", "reason": "already fulfilled", "order_ref": ref}

        product = self._resolve_product(order)
        if not product:
            return self._fail(order, "no matching product for the order (can't map to Gelato)")
        product_key = product.get("product_key")
        sku = product.get("sku")

        # Check if this is a personaliser print order.
        is_personaliser, personaliser_key = self._is_personaliser_product(sku)
        if is_personaliser:
            # For personaliser orders, the buyer must have finished personalizing.
            file_path = self._personaliser_print_file(order)
            if not file_path:
                # Not ready yet — will retry next cycle.
                return {"status": "waiting", "reason": "personaliser not finished",
                        "order_ref": ref}
            product_key = personaliser_key
            # Use a public export URL if available, else treat as file path.
            if self.file_base_url:
                file_url = f"{self.file_base_url.rstrip('/')}/personaliser/{ref}.png"
            else:
                file_url = file_path

        gelato_uid = self._gelato_uid(product_key)
        if not gelato_uid:
            return self._fail(order, f"no gelato_uid for product '{product_key}'",
                              product=product)

        if not is_personaliser:
            file_url = self._print_file_url(product.get("campaign_id"), product_key)
            if not file_url:
                return self._fail(order, "no public print-file URL (set gelato.file_base_url)",
                                  product=product, gelato_uid=gelato_uid)

        recipient = self._recipient(order)
        if not recipient:
            return self._fail(order, "no shipping address on the order",
                              product=product, gelato_uid=gelato_uid)

        payload = self._payload(order, product, gelato_uid, file_url, recipient)
        estimated = float(order.get("production_cost", 0) or 0)

        attempts, last_error = 0, None
        for attempt in range(1, self.max_retries + 1):
            attempts = attempt
            try:
                resp = self.client.create_order(payload)
                gelato_order_id = str(resp.get("id") or resp.get("orderId") or "")
                status = _STATUS_MAP.get(
                    str(resp.get("fulfillmentStatus", "created")).lower(), "created")
                fid = self.db.insert_fulfilment({
                    "order_ref": ref, "order_id": order.get("id"),
                    "product_id": sku, "product_key": product_key,
                    "gelato_uid": gelato_uid, "gelato_order_id": gelato_order_id,
                    "status": status, "estimated_cost": estimated,
                    "currency": order.get("currency", self.currency), "attempts": attempt,
                })
                log.info("Gelato order created for %s -> %s (%s).", ref, gelato_order_id, status)
                # If the create response already carries cost/tracking, capture it.
                if fid:
                    self._absorb(fid, {**order, "estimated_cost": estimated}, resp)
                return {"status": "created", "order_ref": ref,
                        "gelato_order_id": gelato_order_id, "fulfilment_id": fid}
            except Exception as exc:  # transient API error — retry
                last_error = str(exc)
                log.warning("Gelato submit attempt %d/%d failed for %s: %s",
                            attempt, self.max_retries, ref, exc)
                if self.retry_backoff and attempt < self.max_retries:
                    time.sleep(self.retry_backoff * attempt)
        return self._fail(order, last_error or "unknown error", product=product,
                          gelato_uid=gelato_uid, attempts=attempts)

    def fulfil_new_orders(self) -> dict[str, Any]:
        """Submit every paid Etsy order that hasn't been fulfilled yet."""
        if not self.can_fulfil:
            return {"submitted": 0, "failed": 0, "skipped": 0,
                    "reason": "Gelato not configured for fulfilment"}
        done = self.db.fulfilled_order_refs()
        submitted = failed = 0
        results: list[dict[str, Any]] = []
        for order in self.db.get_orders_by_platform("etsy"):
            ref = order.get("order_ref")
            if not ref or ref in done:
                continue
            res = self.submit_order(order)
            results.append(res)
            if res["status"] == "created":
                submitted += 1
            elif res["status"] == "failed":
                failed += 1
        log.info("Gelato fulfilment: %d submitted, %d failed.", submitted, failed)
        return {"submitted": submitted, "failed": failed, "results": results}

    # --- Status + cost ----------------------------------------------

    def sync_status(self) -> dict[str, Any]:
        """Poll Gelato for non-terminal fulfilments; update status/tracking/cost."""
        if not self.is_configured:
            return {"updated": 0, "reason": "Gelato not configured"}
        updated = 0
        for f in self.db.list_fulfilments():
            if f["status"] in _TERMINAL or not f.get("gelato_order_id"):
                continue
            try:
                resp = self.client.get_order(f["gelato_order_id"])
            except Exception as exc:
                log.warning("Gelato status poll failed for %s: %s", f["order_ref"], exc)
                continue
            if self._absorb(f["id"], f, resp):
                updated += 1
        log.info("Gelato status sync updated %d fulfilment(s).", updated)
        return {"updated": updated}

    def _absorb(self, fulfilment_id: int, ledger_ctx: dict[str, Any],
                resp: dict[str, Any]) -> bool:
        """Apply a Gelato order response to a fulfilment: status, tracking, cost.

        Books the actual-vs-estimated production cost to the ledger exactly once."""
        fields: dict[str, Any] = {}
        status = _STATUS_MAP.get(
            str(resp.get("fulfillmentStatus", "")).lower())
        if status:
            fields["status"] = status
        if resp.get("id"):
            fields["gelato_order_id"] = str(resp["id"])

        tracking = self._tracking(resp)
        if tracking:
            fields.update(tracking)
            if tracking.get("tracking_number"):
                fields.setdefault("status", "shipped")
                fields["shipped_at"] = datetime.now(timezone.utc).isoformat()

        actual = self._actual_cost(resp)
        current = self.db.get_fulfilment_by_id(fulfilment_id) or {}
        if actual is not None and not current.get("cost_booked"):
            fields["actual_cost"] = actual
            fields["cost_booked"] = 1
            self._book_cost(ledger_ctx, current, actual)

        if not fields:
            return False
        return self.db.update_fulfilment(fulfilment_id, fields)

    def _book_cost(self, ctx: dict[str, Any], fulfilment: dict[str, Any],
                   actual: float) -> None:
        """Record the true Gelato cost: book (actual − estimate) to the ledger so
        recorded profit reflects real fulfilment cost, not the estimate."""
        estimated = float(fulfilment.get("estimated_cost")
                          or ctx.get("estimated_cost")
                          or ctx.get("production_cost", 0) or 0)
        delta = round(actual - estimated, 2)
        if delta == 0:
            return
        self.db.insert_ledger_entry({
            "kind": "cost", "category": "production",
            "amount": delta, "entry_date": datetime.now(timezone.utc).date().isoformat(),
            "campaign_id": ctx.get("campaign_id"),
            "product_id": ctx.get("product_id") or fulfilment.get("product_id"),
            "marketplace": "gelato",
            "note": (f"Gelato true production cost for {fulfilment.get('order_ref')}: "
                     f"actual {actual:.2f} vs estimate {estimated:.2f} "
                     f"(adjustment {delta:+.2f})"),
        })
        log.info("Booked Gelato cost adjustment %.2f for %s (actual %.2f, est %.2f).",
                 delta, fulfilment.get("order_ref"), actual, estimated)

    # --- Payload / parsing helpers ----------------------------------

    def _payload(self, order: dict[str, Any], product: dict[str, Any],
                 gelato_uid: str, file_url: str, recipient: dict[str, Any]) -> dict[str, Any]:
        qty = int(order.get("quantity", 1) or 1)
        ref = order.get("order_ref")
        return {
            "orderType": "order",
            "orderReferenceId": ref,
            "customerReferenceId": self.customer_ref,
            "currency": order.get("currency", self.currency),
            "items": [{
                "itemReferenceId": f"{ref}-1",
                "productUid": gelato_uid,
                "quantity": qty,
                "files": [{"type": "default", "url": file_url}],
            }],
            "shippingAddress": recipient,
        }

    @staticmethod
    def _recipient(order: dict[str, Any]) -> dict[str, Any] | None:
        import json

        raw = order.get("shipping_address")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if not isinstance(raw, dict):
            return None
        name = (raw.get("name") or "").strip()
        first, _, last = name.partition(" ")
        mapped = {
            "firstName": first or name or "Customer",
            "lastName": last or "-",
            "addressLine1": raw.get("first_line"),
            "addressLine2": raw.get("second_line"),
            "city": raw.get("city"),
            "state": raw.get("state"),
            "postCode": raw.get("zip"),
            "country": raw.get("country_iso"),
            "email": raw.get("email"),
        }
        # A usable address needs at least a line, a city/postcode, and a country.
        if not (mapped["addressLine1"] and mapped["country"]):
            return None
        return {k: v for k, v in mapped.items() if v}

    @staticmethod
    def _tracking(resp: dict[str, Any]) -> dict[str, Any]:
        """Pull the first shipment's tracking number/url/carrier, if present."""
        shipments = resp.get("shipments") or resp.get("shipment") or []
        if isinstance(shipments, dict):
            shipments = [shipments]
        for s in shipments:
            number = s.get("trackingCode") or s.get("trackingNumber")
            if number:
                return {"tracking_number": number,
                        "tracking_url": s.get("trackingUrl") or s.get("trackingLink"),
                        "carrier": s.get("shipmentMethodName") or s.get("carrier")}
        # Some responses nest tracking under receipts/fulfilments.
        return {}

    @staticmethod
    def _actual_cost(resp: dict[str, Any]) -> float | None:
        """Total production+shipping cost from a Gelato order response, if present."""
        receipts = resp.get("receipts")
        if isinstance(receipts, list) and receipts:
            total = 0.0
            found = False
            for r in receipts:
                for key in ("totalAmount", "priceInclVat", "amount", "total"):
                    if r.get(key) is not None:
                        total += float(r[key])
                        found = True
                        break
            if found:
                return round(total, 2)
        for key in ("productionCost", "totalCost", "cost", "total"):
            if resp.get(key) is not None:
                return round(float(resp[key]), 2)
        return None

    # --- Failure handling -------------------------------------------

    def _fail(self, order: dict[str, Any], reason: str, *,
              product: dict[str, Any] | None = None, gelato_uid: str | None = None,
              attempts: int = 1) -> dict[str, Any]:
        ref = order.get("order_ref")
        record = {
            "order_ref": ref, "order_id": order.get("id"),
            "product_id": (product or {}).get("sku"),
            "product_key": (product or {}).get("product_key"),
            "gelato_uid": gelato_uid, "status": "failed",
            "estimated_cost": float(order.get("production_cost", 0) or 0),
            "currency": order.get("currency", self.currency),
            "attempts": attempts, "last_error": reason,
        }
        existing = self.db.get_fulfilment(ref) if ref else None
        if existing:
            self.db.update_fulfilment(existing["id"], {
                "status": "failed", "attempts": attempts, "last_error": reason})
        else:
            self.db.insert_fulfilment(record)
        log.warning("Gelato fulfilment FAILED for %s: %s", ref, reason)
        return {"status": "failed", "order_ref": ref, "reason": reason}

    # --- Reads ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        rows = self.db.list_fulfilments()
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {"total": len(rows), "by_status": by_status, "recent": rows[:10]}
