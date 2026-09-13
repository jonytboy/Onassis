"""Personaliser product workflow manager — create, configure, and publish personaliser listings.

Handles the flow: choose product → select variants → auto-publish to Etsy + Shopify.
Variants: digital (default), canvas (pet & vintage), print (all products).
"""

from __future__ import annotations

import json
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger
from onassis.personaliser import PRODUCTS

log = get_logger(__name__)

# Products that support canvas variant
CANVAS_PRODUCTS = {"pet-portrait", "vintage-photo"}

# NOTE: Pricing is now dynamically managed in personaliser_products table.
# See get_variant_price() below for how pricing is resolved.


class PersonaliserManager:
    """Create and manage personaliser product listings with variant support."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    def get_variant_price(self, product_key: str, variant: str) -> float:
        """Get price for a product variant from the database.

        Variants: 'digital' (product.price), 'canvas' (product.print_price), 'print' (product.print_price)
        """
        prod = self.db.get_personaliser_product(product_key)
        if not prod:
            # Fallback to hardcoded product if not in DB
            if product_key in PRODUCTS:
                p = PRODUCTS[product_key]
                return p.print_price if variant in ("canvas", "print") else p.price
            return 0.0

        # Return appropriate price based on variant
        if variant == "digital":
            return float(prod.get("price", 9.0))
        else:  # canvas or print
            return float(prod.get("print_price", 48.0))

    def get_products(self) -> list[dict[str, Any]]:
        """Return all 15 personaliser products with variant options."""
        products = []
        for key, product in PRODUCTS.items():
            variants = ["digital"]
            if key in CANVAS_PRODUCTS:
                variants.append("canvas")
            variants.append("print")

            cli_etsy = self._cli_publication(key, "etsy")
            cli_shopify = self._cli_publication(key, "shopify")
            products.append({
                "key": key,
                "name": product.name,
                "tier": product.tier,
                "blurb": product.blurb,
                "price": product.price,
                "print_price": product.print_price,
                "available_variants": variants,
                "pricing": {
                    v: self.get_variant_price(key, v)
                    for v in variants
                },
                # Real (image-rich) listing status from the CLI/pipeline path,
                # so the dashboard shows what's actually live before publishing.
                "etsy_live": (cli_etsy or {}).get("status") if cli_etsy else None,
                "shopify_live": (cli_shopify or {}).get("status") if cli_shopify else None,
            })
        return sorted(products, key=lambda p: p["tier"])

    def create_listing(self, product_key: str, variants: list[str]) -> dict[str, Any]:
        """Create personaliser listing(s) with selected variants.

        For each variant, stores a separate listing record in the database.
        Variants: 'digital', 'canvas', 'print'
        """
        if product_key not in PRODUCTS:
            return {"ok": False, "error": f"Product '{product_key}' not found"}

        product = PRODUCTS[product_key]
        created = []
        errors = []

        for variant in variants:
            try:
                # Validate variant
                if variant == "canvas" and product_key not in CANVAS_PRODUCTS:
                    errors.append(f"Canvas not available for {product_key}")
                    continue

                # Get pricing from database
                price = self.get_variant_price(product_key, variant)
                if not price or price <= 0:
                    errors.append(f"No valid pricing for {variant} variant of {product_key}")
                    continue

                # Create database record
                listing_id = self.db.insert_publication({
                    "platform": "personaliser",
                    "product_id": f"{product_key}-{variant}",
                    "campaign_id": 0,
                    "listing_id": None,
                    "mode": "draft",
                    "status": "created",
                    "metadata": json.dumps({
                        "product_key": product_key,
                        "variant": variant,
                        "price": price,
                        "tier": product.tier,
                        "etsy_status": "pending",
                        "shopify_status": "pending",
                    })
                })

                created.append({
                    "product": product_key,
                    "variant": variant,
                    "price": price,
                    "status": "created",
                    "id": listing_id,
                })
            except Exception as exc:
                errors.append(f"{variant} variant of {product_key}: {str(exc)}")

        return {
            "ok": len(errors) == 0,
            "product": product_key,
            "created": created,
            "errors": errors,
        }

    def get_listings(self) -> list[dict[str, Any]]:
        """Return all personaliser listings with their status."""
        pubs = [p for p in self.db.list_publications()
                if p.get("platform") == "personaliser"]

        listings = []
        for pub in pubs:
            meta = {}
            try:
                meta = json.loads(pub.get("metadata") or "{}")
            except Exception:
                pass

            listings.append({
                "id": pub["id"],
                "product": meta.get("product_key", "?"),
                "variant": meta.get("variant", "?"),
                "price": meta.get("price"),
                "tier": meta.get("tier"),
                "status": pub.get("status"),
                "etsy_status": meta.get("etsy_status", "pending"),
                "shopify_status": meta.get("shopify_status", "pending"),
                "etsy_listing_id": pub.get("listing_id"),
            })

        return sorted(listings, key=lambda x: (x["product"], x["variant"]))

    def _cli_publication(self, product_key: str, platform: str) -> dict[str, Any] | None:
        """The product's publication from the **image-rich** pipeline (real
        mockups/before-after renders, access card) — ``product_id`` convention
        ``personaliser-<key>``, created by :meth:`ContentEngine.
        publish_personaliser_listings`/``publish_personaliser_shopify`` (CLI
        ``--publish-personaliser``/``--publish-personaliser-shopify``, or the
        Operations Centre's bulk publish API). Digital listings only."""
        return self.db.get_latest_publication(0, platform, product_id=f"personaliser-{product_key}")

    def publish_to_etsy(self, product_key: str, variant: str) -> dict[str, Any]:
        """Publish a personaliser listing variant to Etsy as a draft.

        For the ``digital`` variant this delegates to the image-rich pipeline
        (:meth:`ContentEngine.publish_personaliser_listings`) so a listing made
        from the dashboard has the same real gallery photos as one made from the
        CLI, and is idempotent against it (won't create a text-only duplicate of
        a listing that already exists). ``canvas``/``print`` — not yet covered by
        that pipeline — still use the simpler bespoke listing below."""
        if variant == "digital":
            return self._publish_digital_via_pipeline(product_key, "etsy")
        try:
            from onassis.etsy_automation import EtsyAutomationEngine
            from onassis.llm import LLMClient
            from onassis.seo import build_seo

            if product_key not in PRODUCTS:
                return {"ok": False, "error": "Product not found"}

            product = PRODUCTS[product_key]
            etsy = EtsyAutomationEngine(self.config, self.db)
            llm = LLMClient(self.config)

            if not etsy.is_configured:
                return {"ok": False, "error": "Etsy not configured"}

            # Build SEO title/tags
            title_prefix = "Printed " if variant == "print" else ("Canvas " if variant == "canvas" else "")
            seo = build_seo({
                "product_type": f"{title_prefix}{product.name}",
                "subject": product.blurb,
                "current_title": product.name,
            }, llm)

            # Get pricing from database
            price = self.get_variant_price(product_key, variant)

            # Build description based on variant
            if variant == "print":
                desc = f"{product.blurb}\n\nPRINTED EDITION — professionally printed and shipped.\n\nWe print 300 DPI on premium matte paper and ship within 5 business days."
                listing_type = "physical"
            elif variant == "canvas":
                desc = f"{product.blurb}\n\nCANVAS EDITION — gallery-wrapped, ready to hang.\n\nHigh-quality canvas print, 1.5\" depth stretchers, arrives ready to display."
                listing_type = "physical"
            else:  # digital
                desc = f"{product.blurb}\n\nDIGITAL PRODUCT — instant download, personal use only."
                listing_type = "download"

            # Create draft listing
            draft = etsy.client.create_draft({
                "title": seo["title"],
                "description": desc,
                "price": price,
                "tags": seo["tags"],
                "taxonomy_id": (self.config.listing or {}).get("taxonomy_id", 0),
                "type": listing_type,
                "who_made": "i_did",
                "when_made": "2020_2025",
                "quantity": 999,
            })

            listing_id = draft.get("listing_id")

            # Update publication record with listing ID and status
            pub_id = next((p["id"] for p in self.db.list_publications()
                          if p.get("product_id") == f"{product_key}-{variant}"), None)
            if pub_id:
                self.db.set_publication_status(pub_id, "draft", listing_id=listing_id)

            return {
                "ok": True,
                "platform": "etsy",
                "status": "draft_created",
                "listing_id": listing_id,
            }
        except Exception as exc:
            log.error(f"Etsy publish error: {exc}")
            return {"ok": False, "error": str(exc)}

    def _publish_digital_via_pipeline(self, product_key: str, platform: str) -> dict[str, Any]:
        """Digital-variant publish for one product via the image-rich CLI
        pipeline — idempotent (returns the existing listing rather than
        duplicating it) and shared by :meth:`publish_to_etsy`/
        :meth:`publish_to_shopify`."""
        from onassis.content_engine import ContentEngine

        if product_key not in PRODUCTS:
            return {"ok": False, "error": "Product not found"}
        existing = self._cli_publication(product_key, platform)
        if existing and existing.get("status") not in ("failed", "pending_oauth"):
            return {"ok": True, "platform": platform, "status": "already_published",
                    "listing_id": existing.get("listing_id"),
                    "note": f"Already has a real {platform.title()} listing from the "
                            "personaliser pipeline — not duplicating it."}
        engine = ContentEngine(self.config, self.db)
        try:
            if platform == "etsy":
                result = engine.publish_personaliser_listings(
                    apply=True, product_key=product_key, prints=False)
            else:
                result = engine.publish_personaliser_shopify(
                    apply=True, product_key=product_key)
        except Exception as exc:
            log.error(f"{platform} pipeline publish error: {exc}")
            return {"ok": False, "error": str(exc)}
        row = next((p for p in result.get("products", []) if p.get("product") == product_key), None)
        if row is None:
            return {"ok": False, "error": "Product not found in pipeline output."}
        status = row.get("status")
        ok = status in ("draft_created", "exists")
        out = {"ok": ok, "platform": platform, "status": status,
              "listing_id": row.get("listing_id")}
        if row.get("link"):
            out["link"] = row["link"]
        if row.get("url"):
            out["url"] = row["url"]
        if row.get("error"):
            out["error"] = row["error"]
        return out

    def publish_to_shopify(self, product_key: str, variant: str) -> dict[str, Any]:
        """Publish a personaliser listing variant to Shopify as a draft product.

        For the ``digital`` variant this delegates to the image-rich pipeline
        (:meth:`ContentEngine.publish_personaliser_shopify`) — see
        :meth:`publish_to_etsy` for why. ``canvas``/``print`` Shopify products
        are not yet supported by that pipeline and still use the bespoke,
        image-less listing below."""
        if variant == "digital":
            return self._publish_digital_via_pipeline(product_key, "shopify")
        try:
            from onassis.connectors.shopify import ShopifyConnector
            from onassis.llm import LLMClient
            from onassis.seo import build_seo

            if product_key not in PRODUCTS:
                return {"ok": False, "error": "Product not found"}

            product = PRODUCTS[product_key]
            shopify = ShopifyConnector(self.config, self.db)
            llm = LLMClient(self.config)

            if not shopify.can_publish:
                return {"ok": False, "error": "Shopify not configured"}

            # Build SEO title/tags (same as Etsy)
            title_prefix = "Printed " if variant == "print" else ("Canvas " if variant == "canvas" else "")
            seo = build_seo({
                "product_type": f"{title_prefix}{product.name}",
                "subject": product.blurb,
                "current_title": product.name,
            }, llm)

            # Get pricing from database
            price = self.get_variant_price(product_key, variant)

            # Build description based on variant (same as Etsy)
            if variant == "print":
                desc = f"{product.blurb}\n\nPRINTED EDITION — professionally printed and shipped.\n\nWe print 300 DPI on premium matte paper and ship within 5 business days."
            elif variant == "canvas":
                desc = f"{product.blurb}\n\nCANVAS EDITION — gallery-wrapped, ready to hang.\n\nHigh-quality canvas print, 1.5\" depth stretchers, arrives ready to display."
            else:  # digital
                desc = f"{product.blurb}\n\nDIGITAL PRODUCT — instant download, personal use only."

            # Create Shopify listing package
            listing = {
                "title": seo["title"],
                "description": desc,
                "price": price,
                "tags": seo["tags"],
                "product_id": f"{product_key}-{variant}",
                "product_key": product_key,
            }

            # Create draft product on Shopify (active=False for draft)
            result = shopify.publish_product(listing, active=False)

            if not result.get("ok"):
                return {"ok": False, "error": result.get("error", "Shopify product creation failed")}

            product_id = result.get("product_id")

            # Update publication record with Shopify product ID and status
            pub_id = next((p["id"] for p in self.db.list_publications()
                          if p.get("product_id") == f"{product_key}-{variant}"), None)
            if pub_id:
                # Update metadata with Shopify info
                pub = next((p for p in self.db.list_publications() if p["id"] == pub_id), None)
                if pub:
                    meta = {}
                    try:
                        meta = json.loads(pub.get("metadata") or "{}")
                    except Exception:
                        pass
                    meta["shopify_status"] = "draft_created"
                    meta["shopify_product_id"] = product_id
                    # Update the publication with new metadata
                    with self.db._connect() as conn:
                        conn.execute(
                            "UPDATE publications SET status = ?, metadata = ? WHERE id = ?",
                            ("draft", json.dumps(meta), pub_id)
                        )

            return {
                "ok": True,
                "platform": "shopify",
                "status": "draft_created",
                "product_id": product_id,
                "handle": result.get("handle"),
                "url": result.get("url"),
                "title": seo["title"],
                "price": price,
            }
        except Exception as exc:
            log.error(f"Shopify publish error: {exc}")
            return {"ok": False, "error": str(exc)}

    def publish(self, product_key: str, variant: str) -> dict[str, Any]:
        """Publish to both Etsy and Shopify simultaneously."""
        etsy_result = self.publish_to_etsy(product_key, variant)
        shopify_result = self.publish_to_shopify(product_key, variant)

        return {
            "ok": etsy_result["ok"] and shopify_result["ok"],
            "product": product_key,
            "variant": variant,
            "etsy": etsy_result,
            "shopify": shopify_result,
        }
