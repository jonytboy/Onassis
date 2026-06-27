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
| `POST /campaign/create` | Run the full generation pipeline; returns a summary. |
| `GET /campaigns` | All campaigns (the dashboard). |
| `GET /campaign/{id}` | One campaign with all assets + its prediction. |
| `GET /campaign/latest` | The most recently generated campaign, with assets. |
| `GET /dashboard` | The profit-first company dashboard. |

`POST /campaign/create` returns exactly:

```json
{ "campaign_id": 17, "status": "completed", "assets_created": 13, "duration_seconds": 47 }
```

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
| `onassis/api.py` | FastAPI service — thin REST layer over the existing services. |
| `onassis/orchestrator.py` | Defines the daily pipeline and owns the agents. |
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
  brand, marketplace). The Profit Engine's source of truth for the dashboard.
