# Episteme

A self-hosted, single-user newsfeed. It reads RSS feeds and a few scraped sites around the clock, and every night a local LLM groups what came in into stories, decides which ones deserve an article, researches and writes them, and reviews its own rendered output before publishing. The feed is one column: the written articles and short cards for everything else, ranked by the reader's interests and decayed by age.

Everything runs on one machine. The models are served by an OpenAI-compatible endpoint on the host (llama.cpp behind [InferMux](https://github.com/AlexanderReaper7/InferMux) here), so no reading habit or source text leaves the house except the fetches themselves.

## What it does

- **Ingests** RSS and Atom feeds and extracts the full article text with trafilatura. Every request goes through one function, `ingest/http.py:polite_get`, which applies one global throttle, per-host spacing, conditional GET and 429 cooldowns.
- **Clusters** items into stories by embedding similarity in Postgres with pgvector, so ten reports of one event become one story.
- **Triages** each story with a small model: write an article, keep it as an aggregate card, or drop it.
- **Writes** an article per approved story in an agentic tool loop. The writer can search through SearXNG, fetch pages through an SSRF guard, and demote a story it finds too thin. Its output is a schema-constrained draft of typed sections (prose, quiz, chart, diagram, timeline, glossary, video), never markup. Sources and further reading come from the database, never from model text.
- **Reviews** the result: headless Chromium renders and screenshots the post, and the vision-capable model critiques and edits it in bounded rounds.
- **Ranks** the feed against the reader's feedback (topic weights, liked and disliked embedding centroids, source weights) with hard blocks applied first.
- **Yields the GPU.** A warden on the host watches the card, and when a game starts it tells Episteme to pause. Episteme stops at the next unit of work and hands the VRAM back.

Around that sit an assistant panel that can search the archive and propose new stories (every write it proposes needs the reader's approval), narration through Fish Audio, push notifications through ntfy, and an admin dashboard showing every LLM call, job and pipeline run with per-story provenance.

## How it is built

| Layer | Choice |
|---|---|
| Web | FastAPI, Jinja2 templates and htmx; no SPA |
| Data | Postgres with pgvector, SQLAlchemy 2 async, Alembic |
| Jobs | Procrastinate, on the same Postgres, so there is no broker |
| LLM | Any OpenAI-compatible endpoint, reached by role (`main`, `fast`, `embed`, `chat`) through one gateway module |
| Browser | Playwright, for the QA screenshots |
| Deploy | Docker Compose: `db`, a one-shot `migrate`, `web` and `worker` |

The code is about 23k lines of Python with 14k lines of tests (`uv run pytest`, no database needed).

## Where to read further

- [docs/architecture.md](docs/architecture.md) is the original specification: data model, pipeline stages, feed composition and roadmap.
- [docs/decisions/](docs/decisions/README.md) holds about sixty decision records. Each one states the rule, what was measured, and what was rejected and why. Read these to find out why something is the way it is.
- [GLOSSARY.md](GLOSSARY.md) defines every term the code and the documents rely on.
- [docs/verification.md](docs/verification.md) lists what is built and tested but has not yet been watched running, and the known gaps in output quality.
- [CLAUDE.md](CLAUDE.md) is the map for coding agents working in the repository: commands, operational facts and the rules that span files.

## Quickstart

```sh
cp .env.example .env      # set POSTGRES_PASSWORD, and LLM_BASE_URL if the models are not on :5001
docker compose up --build
```

Open <http://127.0.0.1:8200> for the feed and <http://127.0.0.1:8200/admin> for the dashboard. The worker ingests every seeded feed every 30 minutes (`INGEST_CRON`) and runs the pipeline nightly (`PIPELINE_CRON`). To start either now:

```sh
curl -X POST http://127.0.0.1:8200/api/jobs/defer/ingest_all    # or run_pipeline
```

## Layout

```text
src/episteme/
├── config.py        settings, from the environment or .env
├── models.py        SQLAlchemy models
├── bootstrap.py     schema upgrade and seeding (the compose `migrate` service)
├── migrations/      Alembic
├── ingest/          source adapters, and polite HTTP in two transport modes
├── llm/             the gateway, structured-output schemas, the agent tool loop,
│                    the assistant, call logging
├── research/        the writer's tools: SearXNG search, SSRF-guarded page fetch
├── recommend/       topic vocabulary, interest profile, scoring, blocks
├── correspondents/  plugins that file finished posts (Matsedel: lunch menus)
├── worker/          Procrastinate tasks, the pipeline, the QA stage
├── tts/             narration
├── bench/           LLM benchmarks run from the dashboard
└── web/             feed, article pages, /api/* JSON, /admin
```

Adding a source type: implement the `SourceAdapter` protocol in a new module under `ingest/`, decorate it with `@register`, import it in `ingest/__init__.py`, and add a `Source` row with that `type_name`.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/), and `uv.lock` is committed.

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

To run the web app outside Docker against the compose database:

```sh
docker compose up -d db migrate
DATABASE_URL=postgresql://episteme:<password>@localhost:5433/episteme uv run uvicorn episteme.web.app:app --reload --port 8200
```

The compose `db` service is published on `127.0.0.1:5433` for direct access, for example from pgAdmin, with the credentials from `.env`.

## License

[AGPL-3.0-only](LICENSE).
