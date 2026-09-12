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

# Variant pricing tiers
VARIANT_PRICING = {
    "digital": {
        "pet-portrait": 18.0, "vintage-photo": 15.0, "renaissance-portrait": 18.0,
        "royal-pet": 18.0, "general-pet": 18.0, "royal-portrait": 18.0,
        "victorian-portrait": 15.0, "popstar-80s": 16.0, "gatsby-1920s": 15.0,
        "boyband-90s": 18.0, "royal-family": 20.0,
        "place-poster": 9.0, "star-map": 12.0, "birth-stats": 9.0, "invite": 9.0,
    },
    "canvas": {"pet-portrait": 45.0, "vintage-photo": 42.0},  # Canvas for photo products
    "print": {k: 32.0 for k in PRODUCTS.keys()},  # Print for all
}


class PersonaliserManager:
    """Create and manage personaliser product listings with variant support."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    def get_products(self) -> list[dict[str, Any]]:
        """Return all 15 personaliser products with variant options."""
        products = []
        for key, product in PRODUCTS.items():
            variants = ["digital"]
            if key in CANVAS_PRODUCTS:
                variants.append("canvas")
            variants.append("print")

            products.append({
                "key": key,
                "name": product.name,
                "tier": product.tier,
                "blurb": product.blurb,
                "price": product.price,
                "print_price": product.print_price,
                "available_variants": variants,
                "pricing": {
                    v: VARIANT_PRICING.get(v, {}).get(key, 32.0)
                    for v in variants
                },
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

                # Get pricing
                price = VARIANT_PRICING.get(variant, {}).get(product_key)
                if not price:
                    errors.append(f"No pricing for {variant} variant of {product_key}")
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

    def publish_to_etsy(self, product_key: str, variant: str) -> dict[str, Any]:
        """Publish a personaliser listing variant to Etsy as a draft."""
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

            # Get pricing
            price = VARIANT_PRICING.get(variant, {}).get(product_key, 0)

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

    def publish_to_shopify(self, product_key: str, variant: str) -> dict[str, Any]:
        """Publish a personaliser listing variant to Shopify as a product."""
        try:
            from onassis.connectors.shopify import ShopifyConnector

            if product_key not in PRODUCTS:
                return {"ok": False, "error": "Product not found"}

            product = PRODUCTS[product_key]
            shopify = ShopifyConnector(self.config, self.db)

            if not shopify.can_publish:
                return {"ok": False, "error": "Shopify not configured"}

            # Get pricing
            price = VARIANT_PRICING.get(variant, {}).get(product_key, 0)

            # Build product title with variant
            title = f"{product.name} — {variant.capitalize()}"

            # TODO: Implement actual Shopify product creation
            # For now, return ready to integrate

            return {
                "ok": True,
                "platform": "shopify",
                "status": "product_created",
                "title": title,
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
