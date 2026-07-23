"""One-off: pin every active product to the connected Pinterest board.

Run once after connecting Pinterest to "catch up" the board with the existing
catalogue:

    python -m scripts.pin_all_products            # pin everything
    python -m scripts.pin_all_products --limit 25 # cap this run
    python -m scripts.pin_all_products --require-link  # only products with a live listing

Pinterest has no natural dedupe, so running it twice re-pins — run it once. It is
best-effort: a pin that fails (e.g. a rate limit) is counted, not fatal.
"""

from __future__ import annotations

import argparse

from onassis.config import load_config
from onassis.database import Database
from onassis.traffic import TrafficEngine


def main() -> None:
    ap = argparse.ArgumentParser(description="Pin all products to Pinterest.")
    ap.add_argument("--limit", type=int, default=None, help="max products to pin")
    ap.add_argument("--require-link", action="store_true",
                    help="skip products with no live listing to link to")
    args = ap.parse_args()

    config = load_config()
    db = Database(config.db_path)
    result = TrafficEngine(config, db).publish_all_products(
        limit=args.limit, require_link=args.require_link)

    if result.get("reason"):
        print(f"Not run: {result['reason']}")
        return
    print(f"Posted {result['posted']}/{result['total']} pin(s) — "
          f"{result['failed']} failed, {result['no_image']} without a hero image, "
          f"{result['skipped']} skipped.")
    for r in result.get("results", []):
        if r.get("error"):
            print(f"  ✗ {r['sku']}: {r['error']}")


if __name__ == "__main__":
    main()
