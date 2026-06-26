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
2. **Content Creator** uses that brief to generate (also via the LLM):
   - 5 Pinterest posts
   - 3 Instagram captions
   - 2 Facebook posts
   - 3 cinematic image prompts
3. **Publisher** and **Analytics Agent** are wired in as **placeholders**
   (no publishing yet).

Everything is stored in **SQLite**.

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
| `onassis/database.py` | SQLite layer: schema + all queries (`briefs`, `content_items`). |
| `onassis/llm.py` | Anthropic API wrapper: prompt → schema-validated JSON. |
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

- **`briefs`** — one row per daily brief (theme, tone, audience,
  objective, keywords, plus the full brief as JSON for forward-compat).
- **`content_items`** — many rows per brief (platform, content_type,
  title, body, metadata, status), linked via `brief_id`.
