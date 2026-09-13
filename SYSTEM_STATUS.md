# ONASSIS — System Status

**Purpose of this file:** a single, current snapshot of what ONASSIS actually
*is* today — the product catalogue, every sales/marketing channel, the
dashboard, and how they're wired together — kept up to date as the system
grows, so nothing built gets forgotten between sessions. `README.md` is the
original v0.1 architecture write-up (still accurate for the daily
content/campaign loop); this file is everything since.

_Last updated: 2026-09-13._

---

## 1. What ONASSIS is now

ONASSIS started as an autonomous content engine (see `README.md`) and has
grown into a full print-on-demand commerce platform:

- A **55-product personaliser catalogue** — self-serve, AI-personalised
  portraits and keepsakes, sold as digital downloads / prints / canvases.
- **Two sales channels**: Etsy (primary) and Shopify (secondary), both wired
  to the same product data.
- A **marketing suite**: blog, Facebook, Pinterest, Instagram, TikTok,
  short-form video (Reels/TikTok clips), all schedulable from one command.
- **Gelato** print fulfilment for physical orders.
- The **Operations Centre** — a browser dashboard (`/operations`) that is the
  intended single control surface for the whole business: run the daily
  cycle, manage products, integrations, marketing, approvals, and (as of this
  update) personaliser products — no SSH, no scripts, for the operations that
  are wired in.
- An **Integrations registry** (`onassis/integrations.py`) — every
  credential (Anthropic, OpenAI, Etsy, Shopify, Pinterest, Facebook,
  Instagram, TikTok, Email, Gelato, Make.com) is entered and tested from the
  dashboard's Integrations tab; saved values persist in the `settings` table
  and overlay `.env`/`config.yaml` at runtime — no redeploy needed to pick up
  a new key.

## 2. The product catalogue — 55 personaliser products

`onassis/personaliser.py` → `PRODUCTS` (dict, key → `Product`). Every product
has a self-serve flow: buyer purchases the listing → gets a link to
`/make/<key>` → enters their order number → uploads a photo (tier 2) or fills
in details (tier 1) → picks a result → downloads instantly (or, for
print/canvas, the order is queued to Gelato).

| Tier | Meaning |
|---|---|
| 1 | Computed from typed details, no AI image call (place posters, star maps, birth stats, invitations) |
| 2 | AI photo transform — upload a photo, get 1 or 3 style variations |

### Original 15 (pre-existing)
Place Poster, Star Map, Birth Stats, Wedding/Party Invitation, Royal Pet
Portrait, Pet as a General, You as Royalty, Victorian Portrait, 80s Pop Star,
1920s Gatsby, 90s Boy Band (family), Family as Royalty, Custom Pet Portrait,
Renaissance Portrait, Vintage Photograph. Most of these offer **3 style
variations** to pick from.

### Pet-costume batch — 20 products (this session's predecessor work)
Single-theme, single-style tier-2 pet portraits — one painterly transform per
product (no 3-way picker), `£7` digital: Astronaut, Pharaoh, Viking, Sailor,
Superhero, Wizard, Cowboy, Knight, Mermaid, Rockstar, Detective, Chef,
Executive, Graduate, Ballet, Biker, Safari, Steampunk, Disco, 80s Pet
Portrait.

### Family/friends group-portrait batch — 20 products (this session)
Same single-theme pattern, for groups instead of solo pets: Rock Band, Pop
Band, 80s Prom, Vintage Family, Christmas Family, Vintage Wedding, Biker
Gang, Superhero Squad, Western Gang, Safari Family, Sports Team, Film Noir
Gang, 90s Sitcom Cast, Graduation Class, Royal Court, Disco Squad, Pirate
Crew, Viking Clan, Astronaut Crew, Renaissance Family.

**Known pre-existing catalogue defect:** `astronaut-pet` was found with only
1 style prompt where the test suite expected the tier-2 default of 3 — this
turned out to be correct (it's meant to be single-style, like the rest of the
costume/group batches); the test (`test_personaliser.py::
test_themed_catalogue_is_well_formed`) was out of date and has been fixed to
accept both shapes.

## 3. Sales channels

### Etsy — primary, live
- OAuth2/PKCE via `onassis/connectors/etsy*.py`. Shop ID `1230400663`.
- **40 personaliser listings confirmed live**: the pet-costume 20 + the
  family/friends 20, published as digital-download drafts (each buyer gets a
  card/PDF with their personal `/make/<key>` link, not the file itself).
- Publish path: `python main.py --publish-personaliser --apply` →
  `ContentEngine.publish_personaliser_listings()` — generates real gallery
  images per product (hero mockup, before/after strip for tier 2, the sample
  card), an access-card PDF, SEO title/tags via the LLM, and records a
  `publications` row (`platform='etsy'`, `product_id='personaliser-<key>'`).
  Idempotent — re-running skips a product that already has a listing.
  `--prints` creates the physical "Printed" sibling listing (Gelato-fulfilled)
  instead of/alongside the digital one; `--reset` forgets and recreates.

### Shopify — secondary, in progress
- Client-credentials grant (`onassis/connectors/shopify_oauth.py`) — the
  operator enters Store URL + Client ID + Client Secret in the dashboard;
  ONASSIS exchanges those for an access token itself (no manual token
  handling), caches and auto-refreshes it.
- Store: `onassismed.myshopify.com`.
- **Until this session, personaliser products had NO Shopify publish path at
  all** — `ShopifyPublisher` (`onassis/shopify_publisher.py`) existed and was
  wired for the general catalogue (`daily_cycle.py`), but nothing called it
  for the personaliser products. The 40 Etsy listings above had no Shopify
  counterpart.
- **Fixed this session**: `ContentEngine.publish_personaliser_shopify()` — the
  Shopify sibling of `publish_personaliser_listings()`. Reuses whatever
  gallery images are already cached from the Etsy publish (same
  `exports/personaliser/listings/` cache) — publishing a product to Shopify
  after it's already on Etsy costs no extra AI generation. Records
  `publications` rows with `platform='shopify'`, same `personaliser-<key>`
  product_id convention as Etsy, so the two channels are trivially
  cross-referenced per product.
  - CLI: `python main.py --publish-personaliser-shopify --apply` (add
    `--reset` to recreate).
  - Backfill in progress for the family/friends 20 on production
    (`rockband-friends`, `popband-friends` confirmed created as of this
    write-up; the rest were mid-run).

### A second, older Shopify+Etsy publish path also exists — now reconciled
`onassis/personaliser_manager.py` (`PersonaliserManager`) is a separate,
earlier "product → variant (digital/canvas/print) → publish to both
platforms" workflow, wired into the dashboard's **Personaliser** tab and the
`/personaliser/*` API (`onassis/api.py`). It predates the image-rich pipeline
above: its `publish_to_etsy`/`publish_to_shopify` built bare text listings
with **no gallery images at all**, under a *different* product_id convention
(`<key>-<variant>` vs. the CLI path's `personaliser-<key>`) — so clicking
"Publish" for a product already live via the CLI path would have silently
created a second, lower-quality duplicate listing.

**Fixed this session**: for the `digital` variant, `PersonaliserManager` now
delegates straight into the image-rich pipeline
(`publish_personaliser_listings`/`publish_personaliser_shopify`) and is
idempotent against it — clicking "Publish" for an already-live product
returns its existing listing instead of duplicating it. `get_products()` also
now surfaces each product's real Etsy/Shopify status
(`etsy_live`/`shopify_live`) so the dashboard shows accurate state before you
click. `canvas`/`print` variants still use the older bare-listing path (that
pipeline doesn't cover physical Shopify products or a distinct canvas listing
yet — a follow-up item, see §7).

## 4. Dashboard — Operations Centre (`/operations`)

Single-page app (Jinja + Alpine + HTMX, no build step) served by the FastAPI
app. Tabs: Overview, Business, Commercial, Catalogue, Content, AI Cost,
Products, **Personaliser**, Marketing, Approvals, Integrations, System.

- **Personaliser tab** (pre-existing, now fixed/reconciled — see §3): lists
  all 55 products, lets you create digital/canvas/print listings and publish
  them to Etsy + Shopify per product, from the browser.
- **Integrations tab**: add/test/reveal credentials for all 11 registered
  integrations (Anthropic, OpenAI, Etsy, Shopify, Pinterest, Facebook,
  Instagram, TikTok, Email, Gelato, Make.com) — this is where the Shopify
  Store URL/Client ID/Secret shown as "Healthy" were entered.
- **RUN BUSINESS**: one button runs the full daily commercial cycle
  (`daily_cycle.py`) with a live stage tracker and log stream.
- New this session — a **bulk/background personaliser publish API**
  (`POST /operations/api/personaliser/publish`, `GET
  /operations/api/personaliser/products`) for publishing many/all products at
  once with progress in the log stream, matching the pattern used by
  RUN BUSINESS and the reel-builder. Not yet wired to its own UI panel (the
  existing Personaliser tab covers the per-product case); worth adding a
  "publish all" button there if bulk publishing from the browser becomes a
  regular need.

Auth: the whole `/operations` subtree is exempt from the global API-key
middleware and uses its own operator check (`X-API-Key` header or `?key=`) —
required in production, open in development.

## 5. Marketing channels

Wired via the same Integrations registry; each is independently
configurable/testable from the dashboard:

| Channel | Status mechanism | Notes |
|---|---|---|
| Blog (Shopify) | `content_engine.py` blog routes, `/operations/api/content/blog*` | Auto-schedules, rewrites, diagnoses, republishes |
| Facebook | `META_PAGE_ACCESS_TOKEN` + Page ID | `--facebook-token` mints a never-expiring token from a short-lived one |
| Instagram | Shares the Facebook Page token + IG Business Account ID | |
| Pinterest | App ID/Secret + OAuth reconnect + Board ID | |
| TikTok | Access token | |
| Email | — | |
| Short-form video (Reels/TikTok clips) | `onassis/content_engine.py`, `/operations/api/content/reels*` | 3 formats: style_slide, product_in_use, gifting |

Run the whole marketing suite once: `python main.py --run-marketing` (cron
this daily). Check what's actually connected and its live/test status any
time from the dashboard's Integrations tab, or `GET
/operations/api/integrations`.

## 6. Fulfilment — Gelato

`GELATO_API_KEY` + `GELATO_FILE_BASE_URL` (a public HTTPS base serving
`exports/<campaign>/<product_key>/print_file.png`). Auto-fulfils paid
physical orders. Catalogue sync: `/operations/api/sync-gelato` or the
Catalogue tab.

**Personaliser print orders — fixed this session, was actually broken.**
`ContentEngine.publish_personaliser_listings(prints=True)` created the Etsy
listing but never inserted the `products` row `GelatoConnector.
_resolve_product` needs to map a paid order to a print job — fixed (now
inserts one, default format = matte poster, same verified-on-Gelato UID as
the general catalogue's `premium_poster`; `GelatoConnector._gelato_uid` falls
back to it for any personaliser key with no explicit `expansion.catalogue`
override).

The deeper bug: `Database.find_personaliser_session_for_order()` matched by
concatenating **every digit** in the order's compound ref
(`etsy-<receipt>-<transaction>`) against the session's stored ref (just the
receipt number the buyer typed) — only ever matched by coincidence for a
single-digit transaction id; any real multi-digit one meant a finished print
order sat "waiting" forever, silently, with no error. Fixed to match on the
receipt-id segment specifically (same fix applied to the buyer-web-app's own
order lookup, `personaliser_web._find_purchase`, new). This was very likely
why fulfilment looked unreliable/untested despite one manual order succeeding
— that one probably just had a 1-digit transaction id.

Also fixed: variant detection (digital/canvas/print) only understood the
older dashboard publish path's metadata; the image-rich CLI pipeline's
listings (no metadata) were silently treated as digital even when printed —
now inferred from the `product_id` convention too. And: the local `orders`
table is only periodically synced (`--etsy-sync`), not real-time — a fast
buyer could finish before their order synced; `finalize()` now falls back to
a live Etsy receipt lookup. And: the buyer's finished file is now actually
copied to the order-ref-named path Gelato's URL construction expects (it
never was before).

## 7. Known gaps / follow-ups

1. **Publish 'Printed' physical listings** for all 55 products — code path is
   complete and tested (see §6); this is now an operational step:
   `python main.py --publish-personaliser --apply --prints` on production.
   Shopify still has no physical-listing path (only digital — see §3's
   "canvas/print variants" note).
2. Shopify backfill: family/friends 20 — resume/finish with
   `python main.py --publish-personaliser-shopify --apply` on production
   (idempotent, skips what's already live). The pet-costume 20 need the same
   — same command, no extra step, since it processes the whole catalogue and
   skips already-published products.

## 8. Environment gotcha worth remembering

**Every coding session here runs in a fresh, ephemeral container.**
`data/*.db`, `onassis.db`, and `data/family_publish/` (generated images) are
all git-ignored — nothing saved through the dashboard, and no generated
image, survives past the container it was made in. The **only** durable
state is (a) what's committed to git, and (b) whatever lives on the real
production host (`api.onassismed.com`, systemd service `onassis`, deployed
via `deploy.sh`) — a *separate*, persistent machine with its own DB and
files. A dev session's outbound network is also proxied and can flatly block
a host (seen this session: `myshopify.com` CONNECT returned 403 from the
sandbox gateway, nothing to do with Shopify credentials) — when a connector
test fails oddly in a dev session, check
`curl -sS "$HTTPS_PROXY/__agentproxy/status"` before assuming it's a config
or credentials problem. **Any real backfill/publish run needs to happen on
production**, not in a coding session, unless the session's network policy
is confirmed to allow the target host.

## 9. CLI quick reference

```bash
# Personaliser catalogue → Etsy / Shopify
python main.py --publish-personaliser --apply            # Etsy digital drafts
python main.py --publish-personaliser --apply --prints    # + physical listings
python main.py --publish-personaliser-shopify --apply     # Shopify drafts (new)
python main.py --publish-personaliser[-shopify] --apply --reset   # recreate

# Marketing
python main.py --run-marketing                            # full suite once

# Dashboard
python main.py --serve --host 0.0.0.0 --port 8000          # /operations lives here

# Daily commercial cycle (what RUN BUSINESS triggers)
python main.py --daily-run

# Diagnostics
python main.py --readiness            # production readiness report
python main.py --ops-check            # operational health check
```

All `--publish-*` / bulk mutating commands are **preview by default** — add
`--apply` to actually create anything.
