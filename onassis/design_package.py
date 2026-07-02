"""The Design Package Builder.

Turns **one CEO-approved product opportunity** into a complete, print-ready
**design package** — the clean, structured hand-off a future artwork generator
needs to produce the actual PNG/SVG with zero manual interpretation.

It does **not** publish, create Etsy listings, or create mock-ups. The
``mockup_prompt.txt`` it writes is a *prompt* for a future mock-up generator,
not a mock-up; the ``listing_seed.json`` is *seed material* for a future
listing, not a listing.

Two gates protect every export:

1. **CEO approval** — the opportunity must be approved by the CEO before a
   package is built (an opportunity already ``selected`` qualifies; a backlog
   opportunity is evaluated now and only proceeds if the CEO approves).
2. **Compliance** — the generated design brief is reviewed by the Compliance
   Director *before* anything is written to disk.

Output (``exports/opportunities/<opportunity_id>/``):

    design_brief.json       every field a designer/generator needs
    print_spec.json         the deterministic print/production spec
    artwork_prompt.txt      prompt for the artwork generator (the PNG/SVG)
    mockup_prompt.txt       prompt for a future mock-up generator (not a mock-up)
    listing_seed.json       seed material for a future listing (not a listing)
    compliance_report.json  the pre-export compliance review

The creative design decisions (colours, typography, layout, placement) are
LLM-generated; the technical print spec (formats, DPI, transparent background,
safe margin, Gelato notes) is deterministic from config.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from onassis.compliance import ComplianceDirector
from onassis.config import ROOT_DIR, Config
from onassis.database import Database
from onassis.llm import LLMClient
from onassis.logger import get_logger
from onassis.opportunities import OpportunityEngine
from onassis.proposals import APPROVE, Proposal

log = get_logger(__name__)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "shirt_colour": {"type": "string"},
        "print_colour": {"type": "string"},
        "typography_direction": {"type": "string"},
        "layout_direction": {"type": "string"},
        "print_placement": {"type": "string"},
        "print_size_guidance": {"type": "string"},
        "artwork_description": {"type": "string"},
        "mockup_scene": {"type": "string"},
        "design_rationale": {"type": "string"},
        "listing_title_seed": {"type": "string"},
        "listing_tags_seed": {"type": "array", "items": {"type": "string"}},
        "listing_description_seed": {"type": "string"},
    },
    "required": [
        "shirt_colour", "print_colour", "typography_direction", "layout_direction",
        "print_placement", "print_size_guidance", "artwork_description",
        "mockup_scene", "design_rationale", "listing_title_seed",
        "listing_tags_seed", "listing_description_seed",
    ],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a senior print/apparel designer for a premium Mediterranean "
    "lifestyle brand. You translate an approved product opportunity into a "
    "precise, print-ready design direction for Gelato direct-to-garment "
    "printing. You decide garment colour, print (ink) colour, typography and "
    "layout direction, and print placement/size. Your artwork is ALWAYS "
    "original — no trademarks, no copyrighted characters, no third-party logos. "
    "You describe the artwork and a mock-up scene as prompts for downstream "
    "generators; you do not create the assets yourself. Be concrete and "
    "specific so no human interpretation is needed."
)


class DesignPackageError(RuntimeError):
    """Raised when a package cannot be built (e.g. unknown opportunity)."""


class DesignPackageBuilder:
    """Builds a print-ready design package from one approved opportunity."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.cfg = config.design or {}
        self.opportunities = OpportunityEngine(config, db)
        self.compliance = ComplianceDirector(config, db)
        self._llm: LLMClient | None = None

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    # --- Public entry point -----------------------------------------

    def build(self, opportunity_id: str) -> dict[str, Any]:
        """Build, validate, and export the design package for an opportunity.

        Returns a result dict with ``status`` of ``ready`` or ``blocked``.
        Raises :class:`DesignPackageError` if the opportunity does not exist.
        """
        opp = self.db.get_opportunity(opportunity_id)
        if opp is None:
            raise DesignPackageError(f"No opportunity with id {opportunity_id}")

        # Gate 1 — the CEO must approve the opportunity before a package exists.
        selection = self.opportunities.select(opportunity_id, agent_name="CEO")
        ceo = selection["ceo"]
        if ceo["verdict"] != APPROVE:
            return {
                "status": "blocked",
                "opportunity_id": opportunity_id,
                "reason": "Opportunity is not CEO-approved; no design package built.",
                "ceo": ceo,
            }
        opp = selection["opportunity"]

        # Gate 2 — Compliance, run autonomously. If the brief needs changes, the
        # builder amends the design (feeding the required corrections back into
        # generation) and re-runs compliance, up to a bounded number of attempts.
        def _produce(corrections: list[str] | None) -> dict[str, Any]:
            design = self._generate_design(opp, corrections)
            return {"design": design, "brief": self._build_brief(opp, design)}

        outcome = self.compliance.resolve(
            _produce, lambda a: self._review_brief(opp, a["brief"]))
        if outcome["status"] != "approved":
            reason = ("Design brief was REJECTED by compliance."
                      if outcome["status"] == "rejected" else
                      f"Design brief still required changes after "
                      f"{outcome['attempts']} amendment attempt(s).")
            return {
                "status": "blocked",
                "opportunity_id": opportunity_id,
                "reason": reason,
                "missing": outcome["missing"],       # precisely what's still wrong
                "compliance_attempts": outcome["attempts"],
                "compliance": outcome["report"],
            }

        design, brief = outcome["artifact"]["design"], outcome["artifact"]["brief"]
        package = self._write_package(opp, brief, design, outcome["report"])
        package["compliance_attempts"] = outcome["attempts"]
        log.info("Design package ready for %s at %s (%d compliance attempt(s))",
                 opportunity_id, package["path"], outcome["attempts"])
        return package

    def get_package(self, opportunity_id: str) -> dict[str, Any] | None:
        """Read back a previously built package, or None if not built."""
        folder = self._folder(opportunity_id)
        brief_path = folder / "design_brief.json"
        if not brief_path.exists():
            return None
        return {
            "status": "ready",
            "opportunity_id": opportunity_id,
            "path": str(folder),
            "design_brief": self._read_json(folder / "design_brief.json"),
            "print_spec": self._read_json(folder / "print_spec.json"),
            "listing_seed": self._read_json(folder / "listing_seed.json"),
            "compliance_report": self._read_json(folder / "compliance_report.json"),
            "artwork_prompt": (folder / "artwork_prompt.txt").read_text(encoding="utf-8"),
            "mockup_prompt": (folder / "mockup_prompt.txt").read_text(encoding="utf-8"),
        }

    # --- Generation -------------------------------------------------

    def _generate_design(
        self, opp: dict[str, Any], corrections: list[str] | None = None
    ) -> dict[str, Any]:
        gen = self.llm.generate_json(
            system=_SYSTEM, prompt=self._prompt(opp, corrections), schema=_SCHEMA
        )
        gen["listing_tags_seed"] = [
            str(t).strip() for t in gen.get("listing_tags_seed", []) if str(t).strip()
        ]
        return gen

    def _build_brief(self, opp: dict[str, Any], design: dict[str, Any]) -> dict[str, Any]:
        """Merge LLM creative decisions with the deterministic print spec."""
        formats = self.cfg.get("file_formats", ["PNG", "SVG"])
        dpi = int(self.cfg.get("dpi", 300))
        transparent = bool(self.cfg.get("transparent_background", True))
        return {
            "opportunity_id": opp["opportunity_id"],
            "brand": opp.get("brand"),
            "theme": opp.get("theme"),
            "product_type": opp.get("product_type"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # --- required design-brief fields ---
            "product_name": opp.get("product_name"),
            "target_customer": opp.get("target_customer"),
            "emotional_angle": opp.get("emotional_angle"),
            "shirt_colour": design["shirt_colour"],
            "print_colour": design["print_colour"],
            "typography_direction": design["typography_direction"],
            "layout_direction": design["layout_direction"],
            "print_placement": design["print_placement"],
            "print_size_guidance": design["print_size_guidance"]
            or self.cfg.get("print_size_default", ""),
            "file_format_requirements": (
                f"{' + '.join(formats)} — {formats[0]} raster master"
                + (f", {formats[1]} vector master" if len(formats) > 1 else "")
                + f" at {dpi} DPI."
            ),
            "transparent_background_required": transparent,
            "dpi_requirement": dpi,
            "safe_margin_guidance": self.cfg.get(
                "safe_margin",
                "Keep critical elements inside the print area's safe margin.",
            ),
            "gelato_compatibility_notes": self.cfg.get("gelato_notes", ""),
            # --- design intent / rationale ---
            "print_area": self.cfg.get("print_area", ""),
            "artwork_description": design["artwork_description"],
            "design_rationale": design["design_rationale"],
        }

    def _print_spec(self, brief: dict[str, Any]) -> dict[str, Any]:
        """The deterministic production spec extracted from the brief."""
        return {
            "opportunity_id": brief["opportunity_id"],
            "file_format_requirements": brief["file_format_requirements"],
            "file_formats": self.cfg.get("file_formats", ["PNG", "SVG"]),
            "dpi_requirement": brief["dpi_requirement"],
            "transparent_background_required": brief["transparent_background_required"],
            "colour_mode": "sRGB",
            "shirt_colour": brief["shirt_colour"],
            "print_colour": brief["print_colour"],
            "print_placement": brief["print_placement"],
            "print_size_guidance": brief["print_size_guidance"],
            "print_area": brief["print_area"],
            "safe_margin_guidance": brief["safe_margin_guidance"],
            "gelato_compatibility_notes": brief["gelato_compatibility_notes"],
        }

    def _listing_seed(self, opp: dict[str, Any], design: dict[str, Any]) -> dict[str, Any]:
        """Seed material for a FUTURE listing — not an Etsy listing."""
        return {
            "opportunity_id": opp["opportunity_id"],
            "product_name": opp.get("product_name"),
            "theme": opp.get("theme"),
            "target_customer": opp.get("target_customer"),
            "search_intent": opp.get("search_intent"),
            "seasonal_relevance": opp.get("seasonal_relevance"),
            "title_seed": design["listing_title_seed"],
            "tags_seed": design["listing_tags_seed"],
            "description_seed": design["listing_description_seed"],
            "note": "Seed only — a future listing factory turns this into a real listing.",
        }

    def _artwork_prompt(self, opp: dict[str, Any], brief: dict[str, Any]) -> str:
        return (
            f"Create ORIGINAL print artwork for '{brief['product_name']}' "
            f"({opp.get('product_type')}) — {opp.get('brand')} "
            f"(premium Mediterranean lifestyle).\n\n"
            f"Theme: {brief['theme']}\n"
            f"Emotional angle: {brief['emotional_angle']}\n"
            f"Artwork: {brief['artwork_description']}\n"
            f"Typography direction: {brief['typography_direction']}\n"
            f"Layout: {brief['layout_direction']}\n"
            f"Print (ink) colour: {brief['print_colour']} on a "
            f"{brief['shirt_colour']} garment.\n\n"
            f"TECHNICAL: {brief['file_format_requirements']} "
            f"Transparent background: {brief['transparent_background_required']}. "
            f"{brief['dpi_requirement']} DPI, sRGB. {brief['safe_margin_guidance']} "
            f"Placement: {brief['print_placement']} — {brief['print_size_guidance']}.\n"
            f"Output the ARTWORK only (no garment, no mock-up). "
            f"Original work only — no trademarks, no copyrighted characters."
        )

    def _mockup_prompt(self, opp: dict[str, Any], brief: dict[str, Any], scene: str) -> str:
        return (
            f"PROMPT FOR A FUTURE MOCK-UP GENERATOR (this builder does not create "
            f"mock-ups).\n\n"
            f"Show '{brief['product_name']}' as a {brief['shirt_colour']} "
            f"{opp.get('product_type')} with the approved artwork at "
            f"{brief['print_placement']} ({brief['print_size_guidance']}).\n"
            f"Scene: {scene}\n"
            f"Style: editorial, natural light, premium Mediterranean lifestyle; "
            f"the product is the hero."
        )

    def _prompt(self, opp: dict[str, Any], corrections: list[str] | None = None) -> str:
        palette = ", ".join(opp.get("colour_palette", []))
        amend = ""
        if corrections:
            bullets = "\n".join(f"- {c}" for c in corrections)
            amend = (
                "\nREQUIRED COMPLIANCE AMENDMENTS — a prior version was flagged. You "
                "MUST apply ALL of these and produce a revised, LOWER-RISK design "
                "(keep it original, no trademarks/copyright/celebrity/logos):\n"
                f"{bullets}\n"
            )
        return f"""Translate this approved product opportunity into a print-ready design direction.
{amend}

OPPORTUNITY
- Product name: {opp.get('product_name')}
- Product type: {opp.get('product_type')}
- Theme: {opp.get('theme')}
- Target customer: {opp.get('target_customer')}
- Emotional angle: {opp.get('emotional_angle')}
- Concept: {opp.get('concept')}
- Suggested palette: {palette}
- Suggested typography: {opp.get('typography_style')}
- Suggested illustration: {opp.get('illustration_style')}

Decide and return:
- `shirt_colour`: the garment colour (specific, e.g. "natural/ecru").
- `print_colour`: the ink/print colour(s).
- `typography_direction`: concrete type direction (style, case, weight).
- `layout_direction`: how elements are arranged.
- `print_placement`: e.g. "centre chest", "left chest", "full front".
- `print_size_guidance`: approximate width and position within the print area.
- `artwork_description`: exactly what the artwork depicts (for the generator).
- `mockup_scene`: a scene for a future mock-up (NOT created here).
- `design_rationale`: why these choices fit the customer and emotional angle.
- `listing_title_seed`, `listing_tags_seed` (8-13), `listing_description_seed`:
  seed material only — NOT a finished listing.
Original, on-brand, Gelato DTG-friendly. No trademarks or copyrighted material."""

    # --- Compliance -------------------------------------------------

    def _review_brief(self, opp: dict[str, Any], brief: dict[str, Any]) -> dict[str, Any]:
        proposal = Proposal(
            agent_name="DesignPackageBuilder",
            requested_action=f"Produce print design '{brief['product_name']}'",
            reasoning=(
                f"{brief['artwork_description']} Typography: "
                f"{brief['typography_direction']}. Print {brief['print_colour']} on "
                f"{brief['shirt_colour']}. Theme {brief['theme']}, angle "
                f"{brief['emotional_angle']}."
            ),
        )
        return self.compliance.review_proposal(proposal, proposal_id=None)

    # --- Write ------------------------------------------------------

    def _folder(self, opportunity_id: str) -> Path:
        base = Path(self.cfg.get("exports_dir", "exports"))
        if not base.is_absolute():
            base = ROOT_DIR / base
        return base / self.cfg.get("subdir", "opportunities") / opportunity_id

    def _write_package(
        self, opp: dict[str, Any], brief: dict[str, Any], design: dict[str, Any],
        review: dict[str, Any],
    ) -> dict[str, Any]:
        folder = self._folder(opp["opportunity_id"])
        folder.mkdir(parents=True, exist_ok=True)

        print_spec = self._print_spec(brief)
        listing_seed = self._listing_seed(opp, design)
        artwork_prompt = self._artwork_prompt(opp, brief)
        mockup_prompt = self._mockup_prompt(opp, brief, design["mockup_scene"])

        self._write_json(folder / "design_brief.json", brief)
        self._write_json(folder / "print_spec.json", print_spec)
        self._write_json(folder / "listing_seed.json", listing_seed)
        self._write_json(folder / "compliance_report.json", review)
        (folder / "artwork_prompt.txt").write_text(artwork_prompt, encoding="utf-8")
        (folder / "mockup_prompt.txt").write_text(mockup_prompt, encoding="utf-8")

        files = [
            "design_brief.json", "print_spec.json", "artwork_prompt.txt",
            "mockup_prompt.txt", "listing_seed.json", "compliance_report.json",
        ]
        return {
            "status": "ready",
            "opportunity_id": opp["opportunity_id"],
            "path": str(folder),
            "files": files,
            "design_brief": brief,
            "print_spec": print_spec,
            "listing_seed": listing_seed,
            "artwork_prompt": artwork_prompt,
            "mockup_prompt": mockup_prompt,
            "compliance_report": review,
        }

    # --- Helpers ----------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))
