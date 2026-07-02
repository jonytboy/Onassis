# ONASSIS — Autonomous Content Engine (v0.1)

ONASSIS is an autonomous content engine for lifestyle brands. This is
**Version 0.1**: a small, production-quality foundation that proves out
one end-to-end loop and is built to extend.

The brand it produces for is **Local Celebrity** — a premium Mediterranean
lifestyle label. Content is generated with the **Anthropic API** and is
written to read like high-end editorial, never like an advert.

## What v0.1 does

Every morning the pipeline runs automatically:

1. **Content Director** generates a unique daily **campaign brief** using the
   Anthropic API, drawing on: the Local Celebrity brand, the Mediterranean
   lifestyle, the current season, the brand's content pillars, and previous
   campaigns stored in SQLite (so it never repeats itself).
2. **Campaign Manager** turns that brief into a **campaign** — the central
   object everything else belongs to (see below).
3. **Content Creator** uses the brief to generate (via the LLM):
   - 5 Pinterest posts
   - 3 Instagram captions
   - 2 Facebook posts
   - 3 cinematic image prompts
4. **ONASSIS Brain** forms a prediction (knowledge) about the campaign —
   a hypothesis it can later be measured against (see below).
5. **Publisher** and **Analytics Agent** are wired in as **placeholders**
   (no publishing yet).

Everything is stored in **SQLite**.

## Campaigns — the central object

A **campaign** is the spine of ONASSIS: everything belongs to one. Each
daily brief becomes exactly one campaign (1:1), and the content generated
for that brief belongs to the campaign. Every campaign has:

| Field | Source |
|-------|--------|
| Campaign ID | auto-assigned |
| Campaign Name | the brief's campaign name |
| Theme | the brief's theme |
| Story | the brief's creative concept |
| Date Created | when the campaign was created |
| Status | `Draft` → `Scheduled` → `Live` → `Complete` (default `Draft`) |
| Content IDs | the content items that belong to it |

Campaigns live in a `campaigns` table (the dashboard). `CampaignManager`
owns the lifecycle; existing briefs are automatically backfilled into
campaigns, so nothing is ever orphaned.

```bash
python main.py --campaigns                 # the dashboard — list all campaigns
python main.py --campaign <id>             # everything for one campaign (JSON)
python main.py --set-status <id> <status>  # Draft | Scheduled | Live | Complete
```

> No publishing and no social-media integration yet — status is **tracked,
> not acted on**.

## The ONASSIS Brain — predict & remember

The **Brain** is the learning layer. For every campaign it forms a
falsifiable prediction and stores it in the `knowledge` table — its
long-term memory. Each knowledge record has:

| Field | Meaning |
|-------|---------|
| Campaign ID | the campaign it belongs to (1:1) |
| Hypothesis | a falsifiable reason this campaign will (or won't) perform |
| Variables | what the campaign effectively tests |
| Predicted outcome | what the Brain expects to happen |
| Confidence | 0-100, honestly calibrated |
| Success metrics | concrete signals to monitor |
| Recommendation | a next action tied to a metric threshold |

The Brain only **predicts and remembers** — it does not read real
analytics yet. The seam for that is built in: every record carries
`status`, `actual_outcome`, and `observed_metrics` columns, and
`OnassisBrain.record_outcome(...)` is the hook a future analytics layer
will call to fold real results back in and revise predictions
automatically.

The Brain is a manager module (`onassis/brain.py`), **not** an agent — the
agent roster is unchanged.

```bash
python main.py --knowledge        # the Brain's memory — every prediction
python main.py --learn <id>       # generate knowledge for a campaign (LLM call)
python main.py --learn            # backfill every campaign missing knowledge
python main.py --campaign <id>    # campaign view now embeds its prediction
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env           # add your ANTHROPIC_API_KEY (required)

python main.py --once          # run the pipeline once
python main.py --show-last     # print the latest brief + its content (JSON)
python main.py                 # start the daily scheduler (long-running)
```

Content generation calls the Anthropic API, so an `ANTHROPIC_API_KEY` is
required. The agents fail with a clear message if it's missing.

## Publisher — automatic Etsy drafts (Draft mode)

The publisher (`onassis/publishing.py`) reads an exported listing package and
publishes it to Etsy as a **draft**. Modes are Dry Run, Draft, and Live —
only Dry Run and Draft are enabled; **Live is intentionally not implemented**.
It publishes only **compliance-approved** campaigns, **logs every
publication** (platform, product, campaign, date/time, listing id, status),
**retries safely** on transient failures and records the reason, and **never
creates a duplicate** (an existing draft/published record short-circuits).
Etsy *write* access is injected (`EtsyDraftClient`), so it runs live with write
credentials and is fully tested offline. No advertising; products aren't
modified after publication.

**Per approved product:** `publish_products(campaign_id)` publishes **one draft
per CEO-approved product** — iterating only the launched set and de-duplicating
per product — so one design becomes the full set of live-ready Etsy drafts.

```bash
python main.py --publish <campaign_id> --mode draft   # or --mode dry_run
```
`POST /publish/{campaign_id}` publishes the single package;
`POST /publish/{campaign_id}/products` publishes every approved product's draft;
`GET /publishing/status` returns the publication log summary.

## Launch Engine — one approval launches the whole design

Each daily cycle produces a **master design + its CEO-approved product set**
(the cold-start 3-5). The Launch Engine (`launch()` in `onassis/publishing.py`)
drafts every approved product and reaches **Launch Ready**, then applies the
configured launch policy (`launch.policy` in config):

* **`manual`** *(default)* — everything is drafted and waits for a **single
  approval**. One `approve_launch(campaign_id)` action launches the master design
  and **every** approved product derived from it together — not one approval per
  product.
* **`scheduled`** — same as manual (drafts + waits); a scheduler can approve.
* **`automatic`** — the launch is approved immediately, no human step.

Launch status is `none → launch_ready → launched`.

```bash
python main.py --pending-launches            # designs awaiting one approval
python main.py --approve-launch <campaign_id> # approve the design + all products
```
`POST /launch/approve/{campaign_id}` approves in one action;
`GET /launch/status/{campaign_id}` returns the status;
`GET /launch/pending` lists designs at Launch Ready.

## Listing Factory — upload-ready Etsy packages (no publishing)

The Listing Factory (`onassis/listing_factory.py`) turns an **approved**
campaign into a complete, **upload-ready** Etsy listing package — no manual
editing — and writes it to disk. It does **not** publish or touch Etsy.

It generates every Etsy upload field (title, description, 13 tags, materials,
primary/secondary colour, category, SEO keywords, image alt text, product
attributes, **deterministic** pricing recommendation), a mock-up manifest,
image order, and file manifest; creates a file for every required mock-up;
validates every image exists; and **validates compliance before export**.
Two gates protect it: the campaign must be compliance-approved, and the
generated listing is re-reviewed by the Compliance Director.

```
exports/<campaign_id>/
    listing.json     # every field required for an Etsy upload
    manifest.json    # files, image order, validation, compliance
    images/          # one file per required mock-up
```

**Per approved product (Revenue Expansion):** `export_products(campaign_id)`
builds **one listing package per CEO-approved product** — iterating only the
products the Expansion Engine launched (never the rejected ones) — adapting the
title, attributes and pricing (catalogue retail) to each, from the same master
design artwork. Each is written to `exports/<campaign_id>/<product_key>/`.

`GET /listing/{campaign_id}` builds the single package;
`GET /listing/{campaign_id}/products` builds one per approved product. CLI
`--build-listing <id>`. (Mock-up images are placeholders pending a future image
generator; the structure is upload-ready.)

## Product Optimiser — improve earners before building new ones

The optimiser (`onassis/optimiser.py`) analyses every **live** product and
recommends the **single** highest-value action — increasing profit from what
already exists. It's a deterministic analytics module (no AI agent), so it's
explainable and fully tested. Per product it computes: views, visits,
favourites, conversion rate, revenue, net profit, ROI, profit trend, traffic
trend, and a confidence.

It recommends exactly one of: *leave unchanged · new Pinterest campaign ·
fresh product images · new lifestyle mockups · rewrite Etsy title · rewrite
Etsy description · improve SEO keywords · one design variation · archive
product* — each with estimated cost, expected profit increase, confidence, and
reasoning. The **CEO then evaluates** the recommendation under existing company
policy; **nothing is executed automatically**. Recommendations on profitable
products are always preferred. Marketplace-agnostic — traffic comes from
listing stats and profit from orders, so new marketplaces need no logic change.

```bash
python main.py --optimise      # the single highest-value product action
```
`GET /optimiser` returns the product analysed, recommendation, expected ROI,
reasoning, confidence (and the CEO's verdict).

## Product Opportunity Engine — discover ideas before any design work

The Opportunity Engine (`onassis/opportunities.py`) decides **what is worth
building** before a single pixel is drawn. It generates commercially viable
product opportunities and stores them as the permanent product development
**backlog**, ranked by expected commercial value. It generates **no images, no
mock-ups, and no listings** — only high-quality ideas and creative direction.

Every opportunity carries a commercial frame (brand, theme, target customer,
emotional angle, product type, search intent, seasonal relevance), a 0-100
scorecard (commercial / originality / brand-fit, estimated demand, estimated
competition, confidence), and concrete creative direction (product name,
one-sentence concept, and suggested colour palette, typography, illustration,
photography, and mock-up styles).

The creative + scoring fields are LLM-generated; the **expected commercial
value** that orders the backlog is computed **deterministically** from the
component scores (weighted blend rewarding commercial/originality/brand-fit/
demand, penalising competition, scaled by confidence), so the ranking is
explainable and testable. **Duplicate concepts are avoided** both by telling
the model what already exists and by enforcing a normalised fingerprint at
insert time.

The **Product Optimiser and CEO choose from this ranked queue**:
`ProductOptimiser.next_opportunity()` pulls the top backlog idea and frames it
as an investment proposal; the CEO evaluates it under the same company policy
as everything else, and on approval the opportunity is marked *selected*.

| Method & path | Purpose |
|---|---|
| `GET /opportunities` | The whole backlog, ranked by expected commercial value. |
| `GET /opportunities/top?limit=` | The top backlog opportunities. |
| `POST /opportunities/generate?count=` | Discover new opportunities (no assets). |

```bash
python main.py --generate-opportunities 8   # discover ideas
python main.py --opportunities               # show the ranked backlog
```

## Design Package Builder — one opportunity → a print-ready package

The Design Package Builder (`onassis/design_package.py`) turns **one
CEO-approved opportunity** into a complete, print-ready **design package** — the
clean, structured hand-off a future artwork generator needs to produce the
actual PNG/SVG with zero manual interpretation. It **does not publish, create
Etsy listings, or create mock-ups**.

Two gates protect every export:

1. **CEO approval** — the opportunity must be CEO-approved before a package is
   built (a backlog opportunity is evaluated now and only proceeds on approval).
2. **Compliance** — the generated design brief is reviewed by the Compliance
   Director *before* anything is written to disk.

It writes `exports/opportunities/<opportunity_id>/`:

| File | Contents |
|---|---|
| `design_brief.json` | Every field a designer/generator needs (below). |
| `print_spec.json` | The deterministic print/production spec. |
| `artwork_prompt.txt` | Prompt for the artwork generator (the PNG/SVG). |
| `mockup_prompt.txt` | Prompt for a *future* mock-up generator (not a mock-up). |
| `listing_seed.json` | Seed material for a *future* listing (not a listing). |
| `compliance_report.json` | The pre-export compliance review. |

The brief includes product name, target customer, emotional angle, shirt
colour, print colour, typography direction, layout direction, print placement,
print size guidance, file-format requirements, transparent-background
requirement, DPI requirement, safe-margin guidance, and Gelato compatibility
notes. The creative decisions (colours, typography, layout, placement) are
LLM-generated; the technical print spec (formats, DPI, transparent background,
safe margin, Gelato notes) is deterministic from config.

| Method & path | Purpose |
|---|---|
| `POST /opportunities/{id}/build-design-package` | Build the package (CEO + compliance gated). |
| `GET /opportunities/{id}/design-package` | Read back a built package. |

```bash
python main.py --build-design-package OPP-1234abcd
```

## Revenue Expansion Engine — the optimal product set per design

The **design is the master asset; products are investments.** The Revenue
Expansion Engine (`onassis/expansion.py`) turns one approved design into the
*optimal set of commercially viable products* — launching it only on the
product types where it will actually make money. The objective is **lifetime
profit per design, not product count**.

**Phase-1 catalogue (10 proven Gelato products):** Premium T-Shirt, Heavyweight
Hoodie, Sweatshirt, Premium Poster, Framed Poster, Canvas, Ceramic Mug, Tote
Bag, Hardcover Notebook, Greeting Card. Each carries a Gelato product UID and an
`available` flag — an unavailable product is skipped gracefully (no failed
launch). All ten are verified available on Gelato's current catalogue.

For every design it scores each product 0-100 on **brand fit, commercial
suitability, estimated conversion, expected profit, production cost, retail
price, and historical performance**. The composite is deterministic (weights in
config); the **CEO evaluates every product as an investment — a product the CEO
rejects is never launched.** Products at/above the threshold (default 80) form
the strict set.

**Cold start.** With no sales history yet, few products clear the threshold, so
the engine tops up with the best CEO-approved products to guarantee a healthy
launch — between `min_variants` (default 3) and `max_variants` (default 5) per
design (`cold_start: true` in config). Once real sales accumulate, more products
clear the threshold on merit and cold-start top-up stops mattering. A CEO
rejection always overrides cold start.

**It learns from its own sales.** `learn_from_sales()` recomputes per-product-
type performance from real orders, so if mugs outperform notebooks for
Mediterranean artwork, mugs' historical score rises and they become more likely
to launch; under-performers become less likely. This runs inside the daily cycle
(the **Expand Products** stage) so scores adapt continuously.

| Method & path | Purpose |
|---|---|
| `GET /expansion/catalogue` | The Phase-1 product catalogue. |
| `POST /expansion/plan/{campaign_id}` | Score + launch the profitable product set. |
| `GET /expansion/plan/{campaign_id}` | The recorded scores for a design. |
| `GET /expansion/performance` | Learned per-product performance from sales. |

```bash
python main.py --catalogue          # the ten products
python main.py --expansion 1        # the launch plan for a design (campaign)
```

## Experiment Engine — every optimisation is a measurable test

The Experiment Engine (`onassis/experiments.py`) turns each optimisation into a
business experiment: a hypothesis about **one changed variable** (title,
thumbnail, mock-up, price, keywords, new Pinterest campaign, …), a success
metric, and — once it ends — a result, a **statistical confidence** (two-
proportion z-test for rate metrics), and a learning. It enforces **one active
experiment per (product, variable)** (no duplicates), and on a win **promotes
the learning into company knowledge**.

Completed experiments feed future decisions: the Product Optimiser won't
re-run a variable already under test, lowers confidence for variables that
*lost*, and raises it for ones that *won* — and that adjusted confidence flows
into the CEO's evaluation.

| Method & path | Purpose |
|---|---|
| `GET /experiments` | All experiments. |
| `GET /experiments/active` | Currently-running experiments. |
| `GET /experiments/{id}` | One experiment. |
| `POST /experiments` | Start an experiment (409 on a duplicate). |
| `POST /experiments/{id}/complete` | Record result, confidence, learning. |

```bash
python main.py --experiments    # list experiments and results
```

## Analytics Collector — historical performance data

The collector (`onassis/analytics.py`) gathers real-world metrics — Etsy
(views, visits, favourites, orders, revenue) and Pinterest (impressions,
saves, outbound clicks, CTR) — and stores **every** observation as a
timestamped snapshot. History is **append-only and never overwritten**. From
that history it derives, per product, the **Traffic / Conversion / Revenue /
Profit** trends. It only collects and reports — no dashboards, no advertising,
no recommendations. Sources are pluggable (`fetch_metrics()`), so new
marketplaces add data with no engine change.

Crucially, the **CEO and Product Optimiser use these historical trends**, not
just today's values: the optimiser reads trends from the collected history and
sets each proposal's risk level from the profit trend, so the CEO discounts the
ROI of declining products.

| Method & path | Purpose |
|---|---|
| `GET /analytics` | Collection summary (snapshots, products, platforms). |
| `GET /analytics/product/{id}` | A product's metric history and trends. |
| `GET /analytics/campaign/{id}` | A campaign's metric history and trends. |

```bash
python main.py --collect-analytics    # append a fresh metric snapshot
```

## Etsy connector — read-only observation

ONASSIS observes the real Etsy shop through a **read-only** connector
(`onassis/connectors/etsy.py`) built on the `RevenueConnector` architecture.
It **never modifies Etsy** (no listing creation/editing/publishing). A sync:

- imports new **orders** (receipts → canonical orders → Revenue Engine),
- imports **listings**, **listing stats**, **favourites**, **visits**, and
  **conversion** (where available),
- is **incremental** (per-resource cursor) and **idempotent** (orders dedupe
  by external ref; listings/stats upsert) — a re-sync never duplicates,
- timestamps every imported record,
- links every listing to its **Product**, **Campaign**, **Revenue**, and
  **Profit** (from the orders it generated),
- updates the Revenue Engine automatically, so the **CEO's business metrics
  refresh after every sync** (they read the live ledger).

Etsy access is injected (`EtsyClient`, real Open API v3) and authenticated via
**OAuth 2.0** (see below); without authorisation a sync is a safe no-op. Future
marketplaces implement the same connector contract — the core engine never
changes.

| Method & path | Purpose |
|---|---|
| `GET /etsy/orders` | Orders imported from Etsy. |
| `GET /etsy/listings` | Listings linked to revenue & profit. |
| `GET /etsy/stats` | Listing stats (views, favourites, conversion). |
| `GET /etsy/sync` | Run a read-only import; refreshes metrics. |

```bash
python main.py --etsy-sync     # import orders & listings (read-only)
```

### Etsy OAuth 2.0 (Authorization Code + PKCE)

Etsy's Open API v3 authenticates with **OAuth 2.0 using PKCE**
(`onassis/connectors/etsy_oauth.py`). The app **keystring** is both the OAuth
`client_id` and the `x-api-key` header; the access token is short-lived (one
hour) and is **refreshed automatically** using the stored refresh token, so
once authorised the connector keeps working without manual steps.

Credentials come only from the environment — **nothing is hardcoded**:

| Env var | Meaning |
|---|---|
| `ETSY_CLIENT_ID` | App keystring (Client ID). Also sent as `x-api-key`. |
| `ETSY_CLIENT_SECRET` | Shared secret. PKCE means it isn't sent on the wire for token requests; read for completeness. |
| `ETSY_REDIRECT_URI` | Redirect URI registered on the Etsy app (must match exactly). |
| `ETSY_SHOP_ID` | Numeric shop id for shop-scoped calls. |

One-time authorisation (interactive):

```bash
python main.py --etsy-login                 # prints the consent URL (PKCE + state)
# open it, approve, copy the redirect URL Etsy sends you back to
python main.py --etsy-callback "<redirect URL>"   # exchanges the code for tokens
python main.py --etsy-auth-status           # shows authorised / token expiry
```

Or over HTTP — `GET /etsy/oauth/login` returns the consent URL,
`GET /etsy/oauth/callback?code=...&state=...` is the registered redirect target
that completes the exchange, and `GET /etsy/oauth/status` reports state.

**Secure storage:** tokens are written to `data/etsy_tokens.json` with `0600`
permissions, atomically (no readable half-written window), and the path is
git-ignored — secrets never enter the repo. PKCE `state` is verified on
callback to defend against CSRF.

## Revenue Intelligence Engine — the financial source of truth

ONASSIS knows exactly how much money it makes. The Revenue Engine
(`onassis/revenue.py`) records every **order** (a sale) with its full cost
breakdown and computes its economics. Entities: **Order**, **Product**,
**Campaign** (already first-class), and the derived **Revenue / Cost /
Profit**. Each order auto-calculates:

| Metric | Formula |
|--------|---------|
| Gross Revenue | sale price × quantity |
| Gross Profit | gross revenue − production cost (COGS) |
| Net Profit | gross revenue − all costs |
| Profit Margin | net profit / gross revenue |
| ROI | net profit / total cost |

Cost breakdown per order: AI, advertising, production, marketplace fees,
payment fees, other. Orders **mirror into the ledger**, so the company-wide
ledger stays the single cash record — which means the **CEO keeps deciding on
net profit**, now driven by real sales (recording an order raises the cash
balance the CEO allocates from).

**Connector-ready:** Etsy, Gelato, Pinterest, and ad platforms plug in by
implementing `onassis.connectors.base.RevenueConnector` (`fetch_orders()` →
canonical order dicts). `RevenueEngine.ingest(connector)` records them — the
core engine never changes.

API endpoints:

| Method & path | Purpose |
|---|---|
| `GET /revenue/today` | Today's orders: revenue, net profit, margin. |
| `GET /revenue/month` | This month's orders. |
| `GET /profit` | Company-wide profit (the bottom line). |
| `GET /orders` | All orders with computed economics. |
| `GET /orders/{id}` | One order. |

```bash
python main.py --orders     # recorded orders with economics
python main.py --revenue    # today / month / company revenue & profit
```

## Profit Engine — the operating philosophy

ONASSIS is a **capital-allocation system**: its purpose is to maximise
long-term sustainable net profit. Every proposal is an **investment** — it
carries cost, expected revenue, net profit, ROI, confidence, time-to-payback,
and a risk level (`onassis/proposals.py`). The CEO ranks proposals by
**risk-adjusted ROI** (`roi × confidence × risk weight`) and allocates the
daily budget to the highest returns first, asking of each:

> "If I invest £1 here, is this the highest expected return currently
> available?" — if it fails the ROI hurdle (`policy.min_roi`), it's rejected.

`Governance.allocate([...])` decides a whole batch competing for one budget:
Compliance screens each, survivors are ranked, and capital is committed
best-first until the daily AI budget or cash reserve runs out.

**Company policies** (enforced): never breach copyright/trademark/platform
policy (Compliance), never exceed the daily AI budget, never fall below the
minimum cash reserve, always prefer long-term profit over vanity metrics.

The **profit dashboard** (`onassis/profit.py`) prioritises, in order:
Net Profit · ROI · Cash Balance · AI Cost · Advertising Cost · Active
Products · Profit Per Product · Profit Per Campaign. Followers/likes/reach are
deliberately absent. It's fed by a `ledger` table of costs and revenue, and
is brand-/marketplace-/product-tagged so multiple brands and marketplaces roll
up through the same engine **without changing the decision logic**.

```bash
python main.py --dashboard     # the profit-first company scoreboard
```
`GET /dashboard` exposes the same metrics over the API.

## Operations Manager — the operational control point

The Operations Manager (`onassis/operations.py`) is the single point of
operational control. It makes **no commercial decisions** — it verifies the
business is healthy and able to operate. **Before** every Daily Cycle it
pre-flights: Etsy, Pinterest, Revenue Engine, Analytics Engine, database
integrity, exports folder, AI provider, AI budget, and cash reserve. If a
**critical** dependency fails it **aborts** the cycle, records the reason, and
notifies the CEO. **After** a cycle it produces an Operations Report:

- **SYSTEM** — overall health, runtime, errors, warnings.
- **BUSINESS** — campaigns generated, listings built, listings published,
  revenue imported, orders imported, profit today, AI spend today.
- **RECOMMENDATIONS** — e.g. "Pinterest connection: not configured.",
  "Revenue sync successful.", "No action required."

Which checks are critical depends on the mode (a `dry_run` only needs the
read/decision modules). `POST /daily/run` now runs **through** the Operations
Manager.

| Method & path | Purpose |
|---|---|
| `POST /operations/check?mode=` | Run pre-flight health checks only. |
| `GET /operations/status` | Latest operational status. |
| `GET /operations/report` | Latest full Operations Report. |

```bash
python main.py --ops-check               # pre-flight health checks
python main.py --daily-run --mode dry_run  # guarded cycle (pre-flight + report)
```

## Production Readiness — what's real vs. dev-only

The Production Readiness audit (`onassis/production_readiness.py`) is
**read-only** — it adds no business logic. It inspects the existing
configuration and modules and reports how close ONASSIS is to operating in the
real world. Every module is graded **Production Ready** / **Partially Ready** /
**Development Only**, every blocker carries a *description, priority, estimated
effort, and recommended fix*, and the report finishes with a single ✅/❌
checklist plus a verification of the nine named subsystems (Etsy reading, Etsy
publishing, Pinterest publishing, Analytics collection, Revenue collection,
Compliance, CEO, Daily Cycle, Operations Manager).

Grades are derived from what is actually configured: the AI provider is ready
when `ANTHROPIC_API_KEY` is set; Etsy reading/publishing are ready once Etsy
credentials are present; the Pinterest connector stays **Development Only**
(its live fetch is a no-op and there is no publishing path); the Listing
Factory is **Partially Ready** because mock-up images are placeholder PNGs; and
live publishing is intentionally disabled (Draft mode only).

| Method & path | Purpose |
|---|---|
| `GET /production/readiness` | Full Production Readiness Report. |

```bash
python main.py --readiness               # print the report (modules, blockers, checklist)
```

## Daily Cycle — the single execution entry point (product-first)

The Daily Cycle (`onassis/daily_cycle.py`) is pure **orchestration** — it adds
no new agents and makes no decisions of its own; each stage delegates to a
module that already owns that responsibility. It is **product-first**:
marketing is generated only **after** a commercially viable product exists. It
runs, in order:

1. Sync Etsy → 2. Sync Pinterest → 3. Import Revenue → 4. Import Analytics →
5. Run Product Optimiser → 6. CEO Decision →
**7. Create Product Opportunity** (CEO-approved) →
**8. Build Design Package** (design brief + artwork prompt, compliance-gated) →
**9. Create Product Campaign** (campaign + product *from the opportunity*) →
**10. Build Etsy Listing Package** (the upload-ready Etsy product) →
**11. Generate Marketing Content** (Pinterest/Instagram/Facebook — promotes the product) →
12. Publish Draft → 13. Record Results

**Marketing is driven by products, not the other way round:** the campaign — and
all the content generated from it — is created from the approved product
opportunity, and content generation is the *last* creative step. The output of a
completed production cycle is at least one upload-ready Etsy product (published
as a draft where Etsy is authorised) — i.e. every cycle can produce a sale.

Every stage logs start/finish, records its duration, captures failures, and the
cycle **continues safely** past a failed stage. Two modes: **dry_run**
(observation + decision only — no product creation, content, or publishing) and
**production** (the full cycle). No scheduling, cron, or timers — just
coordination; the independent modules stay independent.

| Method & path | Purpose |
|---|---|
| `POST /daily/run?mode=` | Run the cycle (`production` or `dry_run`). |
| `GET /daily/status` | The most recent run. |
| `GET /daily/history` | Past runs. |

```bash
python main.py --daily-run                 # production
python main.py --daily-run --mode dry_run  # observation + decision only
```

## Governance — Compliance Director & CEO

No agent acts on its own. Each submits a structured **proposal**
(`onassis/proposals.py`: action, estimated cost, expected benefit,
confidence, risks, reasoning) and the governance layer decides:

1. **Compliance Director** (`onassis/compliance.py`) — a permanent executive
   with **veto power over everyone, including the CEO**. It scores trademark,
   copyright, and platform risk plus brand consistency (LLM-reasoned), then
   applies deterministic thresholds to verdict APPROVE / REJECT /
   REQUEST_MORE_INFO. It stores every report and feeds past rejections back
   into its prompt so it learns from precedent. *Company Law: ONASSIS never
   knowingly infringes IP or platform policy in pursuit of profit.*
2. **CEO** (`onassis/ceo.py`) — the single business decision authority. A
   deterministic engine that checks each proposal against company policy
   (min profit margin, cash reserve, max AI spend, max experiment budget,
   confidence, brand consistency) and writes its reasoning.
3. **Governance** (`onassis/governance.py`) ties them together: Compliance
   reviews first; if it doesn't APPROVE, its verdict **overrules** the CEO.
   Only when Compliance approves does the CEO's verdict decide. Every
   proposal, report, and decision is stored.

The daily pipeline now also runs a Compliance review of each generated
campaign, and a campaign **cannot progress beyond `Draft`** without an
approving compliance report.

```bash
python main.py --proposals     # submitted proposals + their status
python main.py --decisions     # the decision log (CEO + Compliance), with reasoning
python main.py --compliance    # all compliance reports (scores + risks)
```

> Decision engine only — no advertising and no publishing.

## REST API (the service interface)

ONASSIS exposes a FastAPI service so external systems (e.g. Make) can drive
it. The API is a thin layer — it only calls existing services, no business
logic of its own. JSON only; no auth yet.

```bash
python main.py --serve            # or: uvicorn onassis.api:app --port 8000
```

Interactive **Swagger docs at `/docs`**, OpenAPI schema at `/openapi.json`.

| Method & path | Purpose |
|---|---|
| `GET /health` | System status (app, version, counts). |
| `POST /campaign/create` | Run the one product-first workflow (same as `/daily/run`); returns the campaign it created. |
| `GET /campaigns` | All campaigns (the dashboard). |
| `GET /campaign/{id}` | One campaign with all assets + its prediction. |
| `GET /campaign/latest` | The most recently generated campaign, with assets. |
| `GET /dashboard` | The profit-first company dashboard. |
| `GET /optimiser` | Highest-value action for an existing product (CEO-reviewed). |

`POST /campaign/create` runs the **single, product-first** production workflow
(a sellable product is created before any marketing content) and returns the
campaign it produced:

```json
{ "campaign_id": 17, "status": "completed", "assets_created": 13, "duration_seconds": 47 }
```

There is **one** production workflow. Every production entry point —
`POST /campaign/create`, `POST /daily/run`, `python main.py --once`, and the
scheduler — runs the same product-first Daily Cycle. There is no separate
content-first path.

## Tests

The suite mocks the Anthropic API, so it runs fast, offline, and
deterministically — no API key needed.

```bash
pip install -r requirements-dev.txt
pytest
```

Every agent has automated tests (`tests/test_content_director.py`,
`test_content_creator.py`, `test_publisher.py`, `test_analytics.py`), as do
the agent framework (`test_base_agent.py`), the database, the LLM wrapper,
and the end-to-end orchestrator. `tests/test_pipeline.py` is a live
integration test that hits the real API and **skips** unless
`ANTHROPIC_API_KEY` is set.

By default the scheduler runs once on start (`run_on_start: true`) and
then daily at `08:00` local time. Both are configurable.

## Project layout

| Path | Purpose |
|------|---------|
| `main.py` | Thin entry point: loads config, wires everything, runs/schedules. |
| `config.yaml` | Single source of truth for non-secret settings. |
| `.env.example` | Template for environment variables (override config; secrets). |
| `requirements.txt` | Minimal, pure-Python dependencies. |
| `onassis/config.py` | Loads & merges YAML + env into a typed `Config`. |
| `onassis/logger.py` | Centralized console + rotating-file logging. |
| `onassis/database.py` | SQLite layer: schema + all queries (`briefs`, `content_items`, `campaigns`). |
| `onassis/llm.py` | Anthropic API wrapper: prompt → schema-validated JSON. |
| `onassis/campaign_manager.py` | The campaign — central object: lifecycle, dashboard, views. |
| `onassis/brain.py` | The ONASSIS Brain: predicts & remembers (the `knowledge` table). |
| `onassis/proposals.py` | The `Proposal` structure + shared verdict vocabulary. |
| `onassis/ceo.py` | CEO Agent — deterministic company-policy decision engine. |
| `onassis/compliance.py` | Compliance Director — risk review with veto power. |
| `onassis/governance.py` | Routes proposals: Compliance (veto) → CEO → final verdict; batch allocation. |
| `onassis/profit.py` | The Profit Engine — ledger + profit-first dashboard. |
| `onassis/revenue.py` | Revenue Intelligence Engine — orders, economics, rollups. |
| `onassis/optimiser.py` | Product Optimiser — one highest-value action per product. |
| `onassis/opportunities.py` | Product Opportunity Engine — ranked product development backlog. |
| `onassis/design_package.py` | Design Package Builder — opportunity → print-ready design package. |
| `onassis/expansion.py` | Revenue Expansion Engine — scores the catalogue; CEO launches the profitable product set; learns from sales. |
| `onassis/connectors/` | Pluggable revenue sources: base contract, Etsy connector, and Etsy OAuth 2.0 (PKCE). |
| `onassis/api.py` | FastAPI service — thin REST layer over the existing services. |
| `onassis/orchestrator.py` | Owns the content agents + the marketing-content step (the workflow's last creative step). |
| `onassis/daily_cycle.py` | The Daily Cycle — orchestrates all modules in order. |
| `onassis/operations.py` | Operations Manager — health gate + Operations Report. |
| `onassis/production_readiness.py` | Production Readiness audit — per-module grades, blockers, checklist (read-only). |
| `onassis/scheduler.py` | Runs the pipeline daily at a fixed time. |
| `onassis/agents/base.py` | `BaseAgent` — the agent framework foundation. |
| `onassis/agents/content_director.py` | LLM-generates the daily campaign brief (working). |
| `onassis/agents/content_creator.py` | LLM-generates platform content (working). |
| `onassis/agents/publisher.py` | Publishing (placeholder). |
| `onassis/agents/analytics.py` | Performance reporting (placeholder). |
| `tests/test_pipeline.py` | End-to-end smoke test. |

## Configuration

Settings live in `config.yaml`; secrets and per-environment overrides
live in `.env` (git-ignored). **Environment variables always win** over
`config.yaml`. Notable knobs:

- `brand.*` — the Local Celebrity identity: positioning, tagline, voice,
  audience, and `content_pillars` the Director draws on each day.
- `llm.model` / `llm.effort` / `llm.max_tokens` — which Anthropic model to
  use and how hard it works (overridable via `ONASSIS_LLM_*` env vars).
- `content_targets` — how many of each content type the Creator makes.
  Change a number here and both the prompt and output adapt; no code change.
- `scheduler.run_at` / `ONASSIS_RUN_AT` — daily run time.

## How it's built to extend

The architecture is deliberately decoupled so each future version slots
in cleanly:

- **Agents** subclass `BaseAgent` and implement one method (`run`). The
  base class gives every agent config, the database, logging, and uniform
  error handling. Adding an agent = one new file.
- **Real publishing** drops into `agents/publisher.py` — the orchestrator
  already calls it; only the body changes.
- **Real analytics** drops into `agents/analytics.py` and can feed
  insights back to the Director to close the loop.
- **LLM generation** is isolated in `onassis/llm.py` (prompt → validated
  JSON). Swapping models or providers is a change to that one file; the
  agents only build prompts and map the structured result.
- **Storage** is isolated in `database.py`; swapping SQLite for Postgres
  is a localized change.

## Database schema

- **`campaigns`** — one row per campaign (name, theme, story, status,
  created_at), linked 1:1 to a brief via `brief_id`. The central object.
- **`briefs`** — one row per daily brief (theme, tone, audience,
  objective, keywords, plus the full brief as JSON for forward-compat).
- **`content_items`** — many rows per brief (platform, content_type,
  title, body, metadata, status), linked via `brief_id`. They belong to the
  brief's campaign.
- **`knowledge`** — one row per campaign (hypothesis, variables, predicted
  outcome, confidence, success metrics, recommendation), plus `status`,
  `actual_outcome`, and `observed_metrics` reserved for future analytics.
- **`proposals`** — structured requests agents submit for a decision.
- **`decisions`** — the unified decision log (CEO + Compliance verdicts with
  written reasoning).
- **`compliance_reports`** — per-proposal/per-campaign risk reviews
  (compliance score, trademark/copyright/platform risk, brand consistency,
  verdict, corrections). The Compliance Director's learning memory.
- **`ledger`** — costs and revenue (kind, category, amount, campaign/product,
  brand, marketplace). The unified cash record (orders mirror into it).
- **`orders`** — sales with cost breakdown and stored economics (gross
  revenue, gross/net profit, margin, ROI). The Revenue Engine's source of truth.
- **`products`** — the product catalogue (sku, name, production cost, …).
- **`etsy_listings`** / **`listing_stats`** / **`sync_cursors`** — imported Etsy
  listings, dated stat snapshots, and per-resource incremental sync cursors.
