"""Catalogue & Portfolio Manager (Sprint 44).

Turns ONASSIS from a product generator into a **catalogue builder**. A
deterministic manager (consistent with the CEO/CMO/CFO) that:

* tracks per-category targets and the current catalogue against them,
* runs gap analysis (what to prioritise, what to suspend),
* decides the operating mode — **build** (fill gaps) until every category
  reaches target, then **optimise** (expand winners, retire laggards),
* scores catalogue health, and
* flags retirement candidates (no views/favourites/sales after a grace period).

The commercial thesis: Build → Market → Measure → Optimise. You cannot optimise
a catalogue that does not yet exist, so Phase 1 builds balanced depth across
every target category before optimisation begins.
"""

from __future__ import annotations

from typing import Any

# Default per-category targets (configurable via ``catalogue.targets``). The
# key is the display category; product keys map onto these in ``category_of``.
DEFAULT_TARGETS: dict[str, int] = {
    "Mugs": 30, "T-Shirts": 25, "Hoodies": 15, "Tea Towels": 15, "Aprons": 10,
    "Olive Boards": 15, "Cushions": 15, "Posters": 20, "Tote Bags": 15,
    "Candles": 10, "Kitchen Accessories": 20,
}

# Substring → display category (first match wins). Order matters.
_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("tea_towel", "teatowel", "tea towel", "towel"), "Tea Towels"),
    (("apron",), "Aprons"),
    (("olive", "board", "cheese"), "Olive Boards"),
    (("cushion", "pillow"), "Cushions"),
    (("candle",), "Candles"),
    (("tote", "bag", "pouch"), "Tote Bags"),
    (("hoodie",), "Hoodies"),
    (("sweatshirt", "sweater", "crewneck"), "Hoodies"),
    (("tshirt", "t-shirt", "tee", "shirt", "apparel"), "T-Shirts"),
    (("mug", "cup", "tumbler"), "Mugs"),
    (("poster", "print", "canvas", "art", "wall"), "Posters"),
    (("coaster", "trivet", "kitchen", "utensil", "chopping"), "Kitchen Accessories"),
]


def category_of(product_key: str | None, name: str | None = None) -> str:
    text = f"{product_key or ''} {name or ''}".lower()
    for needles, category in _CATEGORY_RULES:
        if any(n in text for n in needles):
            return category
    return "Other"


def _status(current: int, target: int) -> str:
    if target <= 0:
        return "n/a"
    ratio = current / target
    if current >= target:
        return "complete"
    if current == 0:
        return "critical"
    if ratio < 0.34:
        return "priority"
    return "building"


class CatalogueManager:
    """Deterministic catalogue balance, gap analysis, mode and health."""

    def __init__(self, config: Any, db: Any) -> None:
        self.config = config
        self.db = db
        self.cfg = dict(getattr(config, "catalogue", None) or {})
        self.retirement_days = int(self.cfg.get("retirement_days", 45))

    # --- Targets & counts -------------------------------------------

    def targets(self) -> dict[str, int]:
        """Per-category targets: operator setting → config → defaults.

        A config ``targets`` map is authoritative (it replaces the defaults), so
        operators fully control the catalogue shape; only when none is set do the
        built-in defaults apply."""
        cfg_targets = self.cfg.get("targets")
        targets = ({k: int(v) for k, v in cfg_targets.items()} if cfg_targets
                   else dict(DEFAULT_TARGETS))
        try:  # a Business Setting can override the whole map (JSON)
            from onassis.business_settings import BusinessSettings
            override = BusinessSettings(self.db, self.config).get("catalogue_targets")
            if isinstance(override, dict):
                targets.update({k: int(v) for k, v in override.items()})
        except Exception:
            pass
        return targets

    def _catalogue_products(self) -> list[dict[str, Any]]:
        """Products that count toward the catalogue (active, not archived)."""
        try:
            return [p for p in self.db.list_products() if p.get("active", 1)]
        except Exception:
            return []

    def current_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self._catalogue_products():
            cat = category_of(p.get("product_key"), p.get("name"))
            counts[cat] = counts.get(cat, 0) + 1
        return counts

    # --- Gap analysis (Obj 3) ---------------------------------------

    def gap_analysis(self) -> dict[str, Any]:
        targets = self.targets()
        counts = self.current_counts()
        rows: list[dict[str, Any]] = []
        for cat, target in targets.items():
            current = counts.get(cat, 0)
            rows.append({
                "category": cat, "target": target, "current": current,
                "remaining": max(0, target - current),
                "status": _status(current, target)})
        rows.sort(key=lambda r: (-r["remaining"], r["category"]))
        prioritise = [r["category"] for r in rows
                      if r["status"] in ("critical", "priority")]
        suspend = [r["category"] for r in rows if r["status"] == "complete"]
        return {"categories": rows, "prioritise": prioritise, "suspend": suspend,
                "mode": self.mode(rows)}

    def priority_categories(self) -> list[str]:
        return self.gap_analysis()["prioritise"]

    def saturated_categories(self) -> set[str]:
        return set(self.gap_analysis()["suspend"])

    # --- Operating mode (Obj 7, 8, 15) ------------------------------

    def mode(self, rows: list[dict[str, Any]] | None = None) -> str:
        rows = rows if rows is not None else self.gap_analysis()["categories"]
        return "optimise" if all(r["remaining"] == 0 for r in rows) else "build"

    # --- Catalogue health (Obj 11) ----------------------------------

    def health(self) -> dict[str, Any]:
        ga = self.gap_analysis()
        rows = ga["categories"]
        n = len(rows) or 1
        # Coverage: fraction of target units filled across the catalogue.
        total_target = sum(r["target"] for r in rows) or 1
        total_current = sum(min(r["current"], r["target"]) for r in rows)
        coverage = round(total_current / total_target * 100)
        # Category balance: how evenly categories are progressing (100 = all equal).
        ratios = [min(1.0, r["current"] / r["target"]) if r["target"] else 1.0 for r in rows]
        avg = sum(ratios) / n
        spread = (sum((x - avg) ** 2 for x in ratios) / n) ** 0.5
        balance = round(max(0.0, 1 - spread) * 100)
        # Diversity: fraction of target categories that have at least one product.
        represented = sum(1 for r in rows if r["current"] > 0)
        diversity = round(represented / n * 100)
        # Collection completeness: average collection size vs an ideal spread.
        completeness = self._collection_completeness()
        # Commercial readiness: are there enough live products + categories to
        # start measuring? (a soft gate on entering optimisation).
        ready = round(min(100, coverage))
        overall = round((coverage + balance + diversity + completeness + ready) / 5)
        return {
            "category_balance": balance, "diversity": diversity, "coverage": coverage,
            "collection_completeness": completeness, "commercial_readiness": ready,
            "overall": overall, "mode": ga["mode"]}

    def _collection_completeness(self) -> int:
        """Average catalogue-category coverage per collection (campaign)."""
        try:
            products = self._catalogue_products()
        except Exception:
            return 0
        by_campaign: dict[Any, set] = {}
        for p in products:
            cid = p.get("campaign_id")
            by_campaign.setdefault(cid, set()).add(
                category_of(p.get("product_key"), p.get("name")))
        if not by_campaign:
            return 0
        # A "complete" collection spans >= 5 categories (premium breadth).
        ideal = 5
        scores = [min(1.0, len(cats) / ideal) for cats in by_campaign.values()]
        return round(sum(scores) / len(scores) * 100)

    # --- Build-mode selection (Obj 4) -------------------------------

    def rank_for_build(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Re-rank scored candidates to fill catalogue gaps first.

        ``candidates`` are scored product dicts (``product_key``,
        ``composite_score``). In build mode, products in a critical/priority
        category sort first and saturated categories sort last — so ONASSIS
        stops over-producing full categories (mugs/posters/totes) and expands
        the under-represented ones (apparel, kitchen textiles)."""
        ga = self.gap_analysis()
        remaining = {r["category"]: r["remaining"] for r in ga["categories"]}
        rank = {"critical": 0, "priority": 1, "building": 2, "complete": 4, "n/a": 3}
        status_by_cat = {r["category"]: r["status"] for r in ga["categories"]}

        def key(c: dict[str, Any]) -> tuple:
            cat = category_of(c.get("product_key"), c.get("product_name"))
            st = status_by_cat.get(cat, "n/a")
            # gap bucket asc, then remaining desc, then composite score desc
            return (rank.get(st, 3), -remaining.get(cat, 0),
                    -float(c.get("composite_score", 0) or 0))

        return sorted(candidates, key=key)

    # --- Retirement candidates (Obj 9) ------------------------------

    def retirement_candidates(self, *, min_age_days: int | None = None) -> list[dict[str, Any]]:
        """Live products with no views/favourites/sales past the grace period.
        Never deleted — these are archive candidates in optimisation mode."""
        from datetime import datetime, timezone
        age = self.retirement_days if min_age_days is None else min_age_days
        now = datetime.now(timezone.utc)
        perf = {p.get("product_key"): p for p in self._perf()}
        out: list[dict[str, Any]] = []
        for p in self._catalogue_products():
            created = p.get("launched_at") or p.get("created_at") or ""
            try:
                days = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
            except (ValueError, TypeError):
                days = 0
            if days < age:
                continue
            pf = perf.get(p.get("product_key"), {})
            units = int(pf.get("units_sold", 0) or 0)
            views = int(pf.get("views", 0) or 0)
            favourites = int(pf.get("favourites", 0) or 0)
            if units == 0 and views == 0 and favourites == 0:
                out.append({"sku": p.get("sku"), "name": p.get("name"),
                            "product_key": p.get("product_key"),
                            "category": category_of(p.get("product_key"), p.get("name")),
                            "age_days": days})
        return out

    def _perf(self) -> list[dict[str, Any]]:
        try:
            return self.db.list_product_performance()
        except Exception:
            return []

    # --- Dashboards (Obj 5, 6, 12) ----------------------------------

    def catalogue_dashboard(self) -> dict[str, Any]:
        ga = self.gap_analysis()
        return {
            "mode": ga["mode"], "categories": ga["categories"],
            "prioritise": ga["prioritise"], "suspend": ga["suspend"],
            "health": self.health(),
            "total_target": sum(r["target"] for r in ga["categories"]),
            "total_current": sum(r["current"] for r in ga["categories"]),
        }

    def collection_dashboard(self) -> list[dict[str, Any]]:
        """Per-collection (campaign) intelligence: products, published, sales,
        traffic, conversion, ROI, status."""
        from onassis.collections import collection_name

        db = self.db
        perf = {p.get("product_key"): p for p in self._perf()}
        collections: dict[Any, dict[str, Any]] = {}
        for p in self._catalogue_products():
            cid = p.get("campaign_id")
            col = collections.setdefault(cid, {
                "campaign_id": cid, "products": 0, "published": 0, "categories": set(),
                "units": 0, "revenue": 0.0, "ai_cost": 0.0, "views": 0})
            col["products"] += 1
            col["categories"].add(category_of(p.get("product_key"), p.get("name")))
            pf = perf.get(p.get("product_key"), {})
            col["units"] += int(pf.get("units_sold", 0) or 0)
            col["revenue"] += float(pf.get("gross_revenue", 0) or 0)
            col["views"] += int(pf.get("views", 0) or 0)
        # Published counts + names.
        campaigns = {c["id"]: c for c in db.list_campaigns()}
        out: list[dict[str, Any]] = []
        for cid, col in collections.items():
            camp = campaigns.get(cid, {})
            published = db.count_publications(cid) if hasattr(db, "count_publications") else 0
            name = collection_name(None, camp) if camp else f"Collection {cid}"
            conv = round(col["units"] / col["views"] * 100, 1) if col["views"] else None
            status = ("selling" if col["units"] else
                      ("published" if published else "building"))
            out.append({
                "campaign_id": cid, "name": name,
                "products": col["products"], "published": published,
                "category_count": len(col["categories"]),
                "units": col["units"], "revenue": round(col["revenue"], 2),
                "conversion": conv, "status": status})
        out.sort(key=lambda c: (-c["revenue"], -c["units"], -c["products"]))
        return out
