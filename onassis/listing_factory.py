"""The Autonomous Listing Factory.

Turns an approved campaign into a complete, **upload-ready** Etsy listing
package — with zero manual editing required — and writes it to disk. It does
**not** publish and never touches Etsy; the package is the hand-off point for a
future automatic publisher.

For an approved campaign it produces every field an Etsy upload needs (title,
description, 13 tags, materials, colours, category, SEO keywords, image alt
text, product attributes, pricing recommendation), a mock-up manifest, an
image order, and a file manifest. It generates a reference (and a placeholder
file) for every required mock-up, validates that every required image exists,
validates compliance before export, and writes:

    exports/<campaign_id>/
        listing.json     # every field required for an Etsy upload
        manifest.json    # files, image order, validation, compliance
        images/          # one file per required mock-up

Creative fields are LLM-generated; pricing is deterministic. The approval gate
(campaign must be compliance-approved) and a fresh pre-export compliance review
of the generated listing both protect the export.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from onassis.artwork import ArtworkStudio
from onassis.compliance import ComplianceDirector
from onassis.config import ROOT_DIR, Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger
from onassis.proposals import APPROVE, Proposal, is_compliant

log = get_logger(__name__)

_TAG_COUNT = 13

# Markers that a piece of customer-facing copy is unfinished / truncated.
_TRUNCATION_MARKERS = ("…", "...")
# A finished sentence ends with one of these; anything else reads as cut off.
_SENTENCE_END = tuple(".!?") + ('"', "'", "”", "’", ")", "]")
# Trailing words that mean the sentence stops mid-thought.
_MID_THOUGHT = {
    "and", "or", "but", "the", "a", "an", "with", "to", "of", "for", "in", "on",
    "at", "by", "from", "as", "is", "are", "your", "our", "this", "that", "&",
}


def _safe_title(raw: str, limit: int = 140) -> str:
    """Trim a title to ``limit`` chars WITHOUT cutting a word in half."""
    t = " ".join((raw or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rstrip()
    if " " in cut:                      # back off to the last whole word
        cut = cut[:cut.rfind(" ")]
    return cut.strip().rstrip(",-;:")


def _looks_truncated(text: str) -> bool:
    """True if a description reads as unfinished (cut off mid-sentence/word)."""
    t = (text or "").strip()
    if not t:
        return True
    if t.endswith(_TRUNCATION_MARKERS) or t.endswith(("-", ",", ";", ":")):
        return True
    last = t.split()[-1].strip(".,;:!?\"'()[]").lower()
    if last in _MID_THOUGHT:
        return True
    return not t.endswith(_SENTENCE_END)   # prose should end with terminal punctuation


def _token_truncated(token: str) -> bool:
    """True if a short token (tag/material) looks cut off or empty."""
    s = str(token).strip()
    return (not s) or s.endswith(_TRUNCATION_MARKERS) or s.endswith("-")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "materials": {"type": "array", "items": {"type": "string"}},
        "primary_colour": {"type": "string"},
        "secondary_colour": {"type": "string"},
        "category": {"type": "string"},
        "seo_keywords": {"type": "array", "items": {"type": "string"}},
        "image_alt_texts": {"type": "array", "items": {"type": "string"}},
        "product_attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "required": ["name", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "title", "description", "tags", "materials", "primary_colour",
        "secondary_colour", "category", "seo_keywords", "image_alt_texts",
        "product_attributes",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are an expert Etsy listing copywriter for a premium Mediterranean "
    "lifestyle brand. You write upload-ready, conversion-optimised, SEO-strong "
    "listings that read as authentic and original — never as adverts, never "
    "infringing trademarks or copyright. Titles are <=140 characters; tags are "
    "short (<=20 characters) and varied; alt text is descriptive and accessible."
)


def _coerce_len(items: list[str], n: int, filler: list[str], prefix: str) -> list[str]:
    """Return exactly ``n`` non-empty strings, padding/trimming as needed."""
    out = [str(s).strip() for s in items if str(s).strip()][:n]
    pool = [str(s).strip() for s in filler if str(s).strip()]
    i = 0
    while len(out) < n:
        candidate = pool[i] if i < len(pool) else f"{prefix} {len(out) + 1}"
        if candidate not in out:
            out.append(candidate)
        i += 1
    return out


class ListingError(RuntimeError):
    """Raised when a campaign cannot be turned into a listing."""


class ListingFactory:
    """Builds, validates, and exports complete Etsy listing packages."""

    def __init__(self, config: Config, db: Database,
                 studio: ArtworkStudio | None = None) -> None:
        self.config = config
        self.db = db
        self.cfg = config.listing or {}
        self.compliance = ComplianceDirector(config, db)
        # The Artwork Studio produces the REAL commercial images (no placeholders).
        self.studio = studio or ArtworkStudio(config, db)
        self.min_description_chars = int(self.cfg.get("min_description_chars", 120))
        self._llm: LLMClient | None = None

    @property
    def gallery_count(self) -> int:
        return self.studio.gallery_count

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    # --- Public entry point -----------------------------------------

    def export(self, campaign_id: int,
               design_package: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build, validate, and write the listing package for a campaign.

        ``design_package`` (the approved master design) feeds the Artwork Studio
        the full commercial brief so the artwork is brief-driven, not generic.
        Returns a result dict with ``status`` of ``ready`` or ``blocked``.
        Raises :class:`ListingError` if the campaign does not exist.
        """
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            raise ListingError(f"No campaign with id {campaign_id}")

        # Gate 1 — only compliance-cleared campaigns may proceed (Company Law).
        approval = self.db.get_compliance_for_campaign(campaign_id)
        if not approval or not is_compliant(approval.get("verdict", "")):
            return {
                "status": "blocked",
                "campaign_id": campaign_id,
                "reason": "Campaign is not compliance-approved; cannot prepare a listing.",
            }

        # Gate 2 — Compliance on the generated listing, run autonomously: amend
        # the copy and re-review until approved or a bounded attempt limit.
        outcome = self.compliance.resolve(
            lambda corr: self._build_listing(campaign, design_package=design_package,
                                             corrections=corr),
            lambda listing: self._review_listing(listing, campaign_id))
        if outcome["status"] != "approved":
            return {
                "status": "blocked",
                "campaign_id": campaign_id,
                "reason": ("Generated listing was REJECTED by compliance."
                           if outcome["status"] == "rejected" else
                           f"Listing still required changes after "
                           f"{outcome['attempts']} amendment attempt(s)."),
                "missing": outcome["missing"],
                "compliance": outcome["report"],
            }

        package = self._write_package(campaign_id, outcome["artifact"], outcome["report"])
        package["compliance_attempts"] = outcome["attempts"]
        log.info("Listing package ready for campaign #%s at %s (%d compliance attempt(s))",
                 campaign_id, package["path"], outcome["attempts"])
        return package

    def export_products(self, campaign_id: int,
                        design_package: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build one listing package per **CEO-approved** product for a design.

        Iterates only the products the Revenue Expansion Engine launched (never
        the rejected ones), adapting the artwork, title, attributes and pricing
        to each product. ``design_package`` (the approved master design) carries
        the full commercial brief into the Artwork Studio so each product's
        artwork reflects the customer, emotion, palette and intended use — not a
        generic title-only prompt. Writes each to
        ``exports/<campaign_id>/<product_key>/``.
        """
        campaign = self.db.get_campaign(campaign_id)
        if campaign is None:
            raise ListingError(f"No campaign with id {campaign_id}")

        approval = self.db.get_compliance_for_campaign(campaign_id)
        if not approval or not is_compliant(approval.get("verdict", "")):
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "Campaign is not compliance-approved; cannot prepare listings."}

        launched = [s for s in self.db.list_product_scores(campaign_id) if s.get("launched")]
        if not launched:
            return {"status": "blocked", "campaign_id": campaign_id,
                    "reason": "No CEO-approved products for this design."}

        packages = [self.export_product(campaign_id, spec, design_package=design_package,
                                        campaign=campaign) for spec in launched]
        ready = [p for p in packages if p.get("status") == "ready"]
        log.info("Built %d/%d approved product listing(s) for campaign #%s",
                 len(ready), len(launched), campaign_id)
        return {
            "status": "ready" if ready else "blocked",
            "campaign_id": campaign_id,
            "count": len(ready),
            "products": packages,
        }

    def export_product(
        self, campaign_id: int, spec: dict[str, Any],
        design_package: dict[str, Any] | None = None,
        campaign: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build ONE approved product's listing package (real artwork + gallery +
        autonomous compliance). This is the streaming unit — the daily cycle
        publishes each product the instant its package is ready, rather than
        waiting for the whole batch.
        """
        campaign = campaign or self.db.get_campaign(campaign_id)
        if campaign is None:
            raise ListingError(f"No campaign with id {campaign_id}")
        # Autonomous compliance: amend the copy and re-review until approved, or
        # block this product after a bounded attempt limit.
        outcome = self.compliance.resolve(
            lambda corr: self._build_listing(
                campaign, product=spec, design_package=design_package, corrections=corr),
            lambda listing: self._review_listing(listing, campaign_id))
        if outcome["status"] != "approved":
            return {"product_key": spec["product_key"], "status": "blocked",
                    "reason": ("Listing REJECTED by compliance."
                               if outcome["status"] == "rejected" else
                               f"Still needed changes after {outcome['attempts']} attempt(s)."),
                    "missing": outcome["missing"]}
        pkg = self._write_package(campaign_id, outcome["artifact"], outcome["report"],
                                  subdir=spec["product_key"])
        pkg["compliance_attempts"] = outcome["attempts"]
        # Advisories are logged with the product, never a gate.
        pkg["advisories"] = outcome["report"].get("advisories", [])
        return {"product_key": spec["product_key"], **pkg}

    # --- Build ------------------------------------------------------

    def _build_listing(
        self, campaign: dict[str, Any], product: dict[str, Any] | None = None,
        design_package: dict[str, Any] | None = None,
        corrections: list[str] | None = None,
    ) -> dict[str, Any]:
        brief = self.db.get_brief(campaign.get("brief_id")) or {}
        design_brief = (design_package or {}).get("design_brief", {}) or {}

        if product is not None:  # a launched product from the Expansion Engine
            product_key = product["product_key"]
            product_name = product.get("product_name") or product_key
            product_id = f"{campaign['id']}-{product_key}"
            production_cost = product.get("production_cost")
            retail_price = product.get("retail_price")
        else:  # single-listing (legacy) path
            products = self.db.get_products_for_campaign(campaign["id"])
            p0 = products[0] if products else None
            product_key = None
            product_name = None
            product_id = p0["sku"] if p0 else str(campaign["id"])
            production_cost = p0["production_cost"] if p0 else None
            retail_price = None

        gen = self.llm.generate_json(
            system=_SYSTEM,
            prompt=self._prompt(campaign, brief, product_name, retail_price, corrections),
            schema=_SCHEMA,
        )

        # Normalise to exact, upload-ready shapes.
        tags = _coerce_len(gen["tags"], _TAG_COUNT, gen.get("seo_keywords", []), "tag")
        alt_texts = _coerce_len(
            gen["image_alt_texts"], self.gallery_count,
            [f"{campaign['name']} image" for _ in range(self.gallery_count)], "image",
        )
        pricing = self._pricing(production_cost, retail_price)
        title = _safe_title(gen["title"], 140)

        # The commercial brief the Artwork Studio renders from. The approved
        # master design (customer, emotion, palette, artwork intent, rationale)
        # is the rich base; per-product/listing fields layer on top so the model
        # gets the WHOLE context, never just the product title.
        studio_brief = {
            **design_brief,  # target_customer, emotional_angle, artwork_description, ...
            "product_name": product_name or campaign.get("name", ""),
            "brand": design_brief.get("brand") or (self.config.brand or {}).get("name", ""),
            "theme": design_brief.get("theme") or brief.get("theme")
            or campaign.get("theme", "coastal"),
            "shirt_colour": design_brief.get("shirt_colour") or gen["primary_colour"],
            "print_colour": design_brief.get("print_colour") or gen["secondary_colour"],
            "primary_colour": gen["primary_colour"],
            "secondary_colour": gen["secondary_colour"],
            "listing_title_seed": title,
            "artwork_description": (
                design_brief.get("artwork_description") or brief.get("visual_direction")
                or campaign.get("story") or f"{campaign.get('theme', '')} original artwork"
            ),
            "transparent_background_required": True,
            "dpi_requirement": int(design_brief.get("dpi_requirement")
                                    or (self.config.design or {}).get("dpi", 300)),
        }

        return {
            "campaign_id": campaign["id"],
            "campaign_name": campaign.get("name", ""),
            "product_id": product_id,
            "product_key": product_key,
            "product_name": product_name,
            # The design is the master asset; the same artwork is adapted per product.
            "artwork_source": "design master asset",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # Etsy upload fields
            "state": "draft",
            "type": "physical",
            "title": title,
            "description": gen["description"].strip(),
            "tags": tags,
            "materials": [m.strip() for m in gen["materials"] if str(m).strip()][:13],
            "primary_colour": gen["primary_colour"],
            "secondary_colour": gen["secondary_colour"],
            "category": gen["category"],
            "seo_keywords": gen["seo_keywords"],
            "product_attributes": gen["product_attributes"],
            "who_made": self.cfg.get("who_made", "i_did"),
            "when_made": self.cfg.get("when_made", "made_to_order"),
            "is_supply": bool(self.cfg.get("is_supply", False)),
            "taxonomy_id": self.cfg.get("taxonomy_id"),
            "shipping_profile_id": self.cfg.get("shipping_profile_id"),
            "quantity": int(self.cfg.get("quantity", 50)),
            "price": pricing["price"],
            "currency": pricing["currency"],
            "pricing_recommendation": pricing,
            # Images are generated for real at write time by the Artwork Studio.
            "_studio_brief": studio_brief,
            "_alt_texts": alt_texts,
        }

    def _pricing(
        self, production_cost: Any = None, retail_price: Any = None
    ) -> dict[str, Any]:
        production_cost = float(
            production_cost if production_cost not in (None, "")
            else self.cfg.get("default_production_cost", 12.0)
        )
        if retail_price not in (None, ""):  # catalogue retail from the Expansion Engine
            price = round(float(retail_price), 2)
            margin = round((price - production_cost) / price, 4) if price else 0.0
            rationale = (
                f"Catalogue retail {price:.2f} — a {margin:.0%} margin over a "
                f"{production_cost:.2f} production cost."
            )
        else:
            margin = float(self.cfg.get("target_margin", 0.60))
            price = round(production_cost / (1 - margin), 2) if margin < 1 else production_cost
            rationale = (
                f"Priced for a {margin:.0%} margin over a {production_cost:.2f} "
                f"production cost."
            )
        return {
            "price": price,
            "currency": self.cfg.get("currency", "GBP"),
            "production_cost": round(production_cost, 2),
            "target_margin": margin,
            "rationale": rationale,
        }

    def _prompt(
        self, campaign: dict[str, Any], brief: dict[str, Any],
        product_name: str | None = None, retail_price: Any = None,
        corrections: list[str] | None = None,
    ) -> str:
        keywords = ", ".join(brief.get("keywords", []))
        product_line = ""
        if product_name:
            price_hint = f" priced around {float(retail_price):.2f}" if retail_price else ""
            product_line = (
                f"\n- PRODUCT TYPE: {product_name}{price_hint}. Write the title, "
                f"description, tags, materials and attributes specifically for a "
                f"{product_name} carrying this design.\n"
            )
        amend = ""
        if corrections:
            bullets = "\n".join(f"- {c}" for c in corrections)
            amend = (
                "\nREQUIRED COMPLIANCE AMENDMENTS — a prior version of this listing "
                "was flagged. You MUST apply ALL of these and produce revised, "
                "lower-risk copy (original, no trademarks/celebrity/logos/misleading "
                f"claims):\n{bullets}\n"
            )
        return f"""Create a complete, upload-ready Etsy listing for this product.
{amend}
PRODUCT / CAMPAIGN
- Name: {campaign.get('name', '')}
- Theme: {campaign.get('theme', '')}
- Story: {campaign.get('story', '')}
- Visual direction: {brief.get('visual_direction', '')}
- Keywords: {keywords}{product_line}

Produce:
- `title`: <=140 chars, search-friendly, benefit-led, no ALL CAPS.
- `description`: a full, persuasive, well-structured description (origin, what
  it is, materials/feel, sizing/use, care) — authentic, not an advert.
- `tags`: EXACTLY 13 short tags (each <=20 chars), varied, no duplicates.
- `materials`: realistic materials list.
- `primary_colour` and `secondary_colour`.
- `category`: an Etsy-style category path.
- `seo_keywords`: 6-10 strong search phrases.
- `image_alt_texts`: {self.gallery_count} descriptive alt texts for the Etsy
  gallery (hero, lifestyle, close-up, scale, room), accessible and specific.
- `product_attributes`: name/value pairs (e.g. style, room, occasion, finish).
Original, on-brand premium Mediterranean lifestyle. No trademarks, no
copyrighted characters, no third-party logos.
"""

    # --- Compliance -------------------------------------------------

    def _copy_quality_issues(self, listing: dict[str, Any]) -> list[str]:
        """Deterministic launch-quality checks on the customer-facing copy.

        Truncated / unfinished title, description, materials or tags make a
        listing look broken — a commercial launch-blocker (not a legal one). Each
        issue returned becomes a FIXABLE blocking issue so the copy is regenerated
        before the Etsy draft is ever created.
        """
        issues: list[str] = []
        title = (listing.get("title") or "").strip()
        desc = (listing.get("description") or "").strip()
        tags = listing.get("tags") or []
        materials = listing.get("materials") or []

        if not title:
            issues.append("Title is empty — regenerate a complete title.")
        elif title.endswith(_TRUNCATION_MARKERS) or _token_truncated(title):
            issues.append(f"Title looks truncated/cut off ('…{title[-24:]}') — "
                          f"regenerate a complete title.")

        if len(desc) < self.min_description_chars:
            issues.append(f"Description is too short/incomplete ({len(desc)} chars, "
                          f"min {self.min_description_chars}) — regenerate a full description.")
        elif _looks_truncated(desc):
            issues.append("Description appears truncated — it does not end as a complete "
                          "sentence. Regenerate a full, finished description.")

        if len(tags) < _TAG_COUNT:
            issues.append(f"Only {len(tags)}/{_TAG_COUNT} tags — regenerate a full tag set.")
        if any(_token_truncated(t) for t in tags):
            issues.append("One or more tags look truncated — regenerate clean tags.")

        if not materials:
            issues.append("Materials are missing — regenerate a realistic materials list.")
        elif any(_token_truncated(m) for m in materials):
            issues.append("One or more materials look truncated — regenerate clean materials.")
        return issues

    def _review_listing(self, listing: dict[str, Any], campaign_id: int) -> dict[str, Any]:
        # Launch-quality gate: unfinished/truncated copy is a fixable BLOCK so it
        # is regenerated before the Etsy draft is created (not just an advisory).
        quality = self._copy_quality_issues(listing)
        extra_blocking = [{"category": "incomplete_copy", "detail": q, "fixable": True}
                          for q in quality]
        proposal = Proposal(
            agent_name="ListingFactory",
            requested_action=f"Prepare Etsy listing '{listing['title']}'",
            reasoning=(
                f"{listing['description'][:500]} Tags: {', '.join(listing['tags'])}."
            ),
            campaign_id=campaign_id,
        )
        return self.compliance.review_proposal(
            proposal, proposal_id=None, extra_blocking=extra_blocking)

    # --- Write & validate -------------------------------------------

    def _exports_base(self) -> Path:
        base = Path(self.cfg.get("exports_dir", "exports"))
        return base if base.is_absolute() else (ROOT_DIR / base)

    def _write_package(
        self, campaign_id: int, listing: dict[str, Any], review: dict[str, Any],
        subdir: str | None = None,
    ) -> dict[str, Any]:
        folder = self._exports_base() / str(campaign_id)
        if subdir:  # one sub-folder per product for the approved product set
            folder = folder / subdir
        images_dir = folder / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        # Generate the REAL commercial assets (not placeholders): the master
        # artwork + print file at the folder root, and an 8-10 image Etsy gallery.
        studio_brief = listing.pop("_studio_brief", {})
        alt_texts = listing.pop("_alt_texts", [])
        design_package = {"design_brief": studio_brief}
        product = {"product_key": listing.get("product_key") or str(campaign_id),
                   "product_name": listing.get("product_name")
                   or listing.get("campaign_name")}
        master = self.studio.generate_master(design_package, folder)
        gallery = self.studio.build_product_gallery(
            design_package, product, images_dir, alt_texts=alt_texts)

        images = [
            {"order": m["order"], "mockup_type": m["scene"], "filename": m["filename"],
             "alt_text": m["alt_text"], "source": m["source"],
             "quality_score": m["review"]["score"]}
            for m in gallery
        ]
        image_order = [img["filename"] for img in images]
        listing["images"] = images
        listing["image_order"] = image_order
        listing["mockup_manifest"] = images
        listing["artwork_backend"] = master["backend"]
        listing["file_manifest"] = [
            "listing.json", "manifest.json", "master_artwork.png", "print_file.png",
            *[f"images/{f}" for f in image_order],
        ]

        # Validate every generated image exists and is non-empty — real files.
        missing = [
            img["filename"] for img in images
            if not (images_dir / img["filename"]).exists()
            or (images_dir / img["filename"]).stat().st_size == 0
        ]
        production_files = ["master_artwork.png", "print_file.png"]
        missing_production = [f for f in production_files
                              if not (folder / f).exists()
                              or (folder / f).stat().st_size == 0]
        validation = {
            "required_images": len(images),
            "present_images": len(images) - len(missing),
            "all_images_present": not missing and not missing_production,
            "missing": missing + missing_production,
            "tag_count_ok": len(listing["tags"]) == _TAG_COUNT,
            "master_artwork_present": "master_artwork.png" not in missing_production,
            "print_file_present": "print_file.png" not in missing_production,
            "gallery_size_ok": 8 <= len(images) <= 10,
        }

        (folder / "listing.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")

        manifest = {
            "campaign_id": campaign_id,
            "generated_at": listing["generated_at"],
            "artwork_backend": master["backend"],
            "image_order": image_order,
            "mockup_manifest": images,
            "production_files": production_files,
            "master_review": master["master_review"],
            "print_review": master["print_review"],
            "files": self._file_listing(folder),
            "validation": validation,
            "compliance": {
                "verdict": review["verdict"],
                "compliance_score": review["compliance_score"],
            },
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return {
            "status": "ready",
            "campaign_id": campaign_id,
            "path": str(folder),
            "listing": listing,
            "manifest": manifest,
            "validation": validation,
        }

    @staticmethod
    def _file_listing(folder: Path) -> list[dict[str, Any]]:
        files = []
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                files.append(
                    {"path": str(path.relative_to(folder)), "bytes": path.stat().st_size}
                )
        return files
