# ONASSIS — Autonomous Content Engine (v0.1)

ONASSIS is an autonomous content engine for lifestyle brands. This is
**Version 0.1**: a small, production-quality foundation that proves out
one end-to-end loop and is built to extend.

## What v0.1 does

Every morning the pipeline runs automatically:

1. **Content Director** creates a daily content **brief** (theme, tone,
   audience, objective, keywords).
2. **Content Creator** uses that brief to generate:
   - 5 Pinterest posts
   - 3 Instagram captions
   - 2 Facebook posts
   - 3 image prompts
3. **Publisher** and **Analytics Agent** are wired in as **placeholders**
   (no publishing yet).

Everything is stored in **SQLite**. Content generation is template-based
and deterministic, so the project runs with **no API key and no network**.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # optional — tweak settings/secrets

python main.py --once         # run the pipeline once
python main.py --show-last    # print the latest brief + its content (JSON)
python main.py                # start the daily scheduler (long-running)
python tests/test_pipeline.py # smoke test
```

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
| `onassis/orchestrator.py` | Defines the daily pipeline and owns the agents. |
| `onassis/scheduler.py` | Runs the pipeline daily at a fixed time. |
| `onassis/agents/base.py` | `BaseAgent` — the agent framework foundation. |
| `onassis/agents/content_director.py` | Builds the daily brief (working). |
| `onassis/agents/content_creator.py` | Generates platform content (working). |
| `onassis/agents/publisher.py` | Publishing (placeholder). |
| `onassis/agents/analytics.py` | Performance reporting (placeholder). |
| `tests/test_pipeline.py` | End-to-end smoke test. |

## Configuration

Settings live in `config.yaml`; secrets and per-environment overrides
live in `.env` (git-ignored). **Environment variables always win** over
`config.yaml`. Notable knobs:

- `brand.content_pillars` — themes the Director rotates through daily.
- `content_targets` — how many of each content type the Creator makes.
  Change a number here and the output adapts; no code change needed.
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
- **An LLM-backed Creator/Director** replaces the `_generate_*` helpers
  (read `ANTHROPIC_API_KEY` from config) without touching anything else.
- **Storage** is isolated in `database.py`; swapping SQLite for Postgres
  is a localized change.

## Database schema

- **`briefs`** — one row per daily brief (theme, tone, audience,
  objective, keywords, plus the full brief as JSON for forward-compat).
- **`content_items`** — many rows per brief (platform, content_type,
  title, body, metadata, status), linked via `brief_id`.
