# Episteme

Self-hosted, LLM-powered personalized newsfeed. Ingests news from many
sources, processes it overnight with a local LLM, and produces a healthy, finite,
learning-focused feed of newly written articles.

Full design: [episteme-architecture.md](episteme-architecture.md).
**Current state: Phases 1–2 built and live-verified.** Phase 1 = ingestion (RSS +
full-article extraction) and the feed UI; Phase 2 = the overnight LLM pipeline
(embed → cluster → triage → research → write) producing generated articles above an
aggregation stream, verified end-to-end against a local llama-server — including a
bounded research agent (SearXNG search + guarded page fetches) that grounds each
article, plus full observability: every LLM call logged to the database, a JSON API
under `/api/*`, and an admin dashboard at `/admin` with per-story provenance.
Next: Phase 2.5 (writer-led agentic write + vision QA + post/feature renames — see
spec §12). See CLAUDE.md for the live operational state and how to run the pipeline.

## Quickstart

```sh
cp .env.example .env      # set POSTGRES_PASSWORD
docker compose up --build
```

Open <http://127.0.0.1:8200> (feed) and <http://127.0.0.1:8200/admin> (dashboard).
The worker ingests all seeded feeds every 30 minutes (`INGEST_CRON` to change) and
runs the LLM pipeline nightly (`PIPELINE_CRON`). To trigger either immediately:

```sh
curl -X POST http://127.0.0.1:8200/api/jobs/defer/ingest_all    # or run_pipeline
```

## Layout

```text
src/episteme/
├── config.py        settings (env / .env)
├── models.py        SQLAlchemy models (Source, SourceItem, Story, Article,
│                    LlmCall, PipelineRun)
├── db.py            engine + init
├── seeds.py         initial source list
├── bootstrap.py     one-shot schema/seed (compose `migrate` service)
├── ingest/          source adapters (base protocol, registry, rss) + polite HTTP
│                    (two transport modes: polite / TLS-impersonate)
├── llm/             gateway (role→model), structured-output schemas, research
│                    agent tool loop, call logging (llm/observe.py)
├── research/        agent tools: SearXNG search + SSRF-guarded page fetch
├── worker/          procrastinate app, ingestion + pipeline tasks
└── web/             FastAPI app: feed UI (Jinja2 + htmx), /api/* JSON routes,
                     /admin dashboard + per-story provenance
```

Adding a source type: implement the `SourceAdapter` protocol in a new module under
`ingest/`, decorate with `@register`, import it in `ingest/__init__.py`, and add a
`Source` row with that `type_name`.

## Development

```sh
python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e .[dev]
pytest
ruff check .
```

Run the web app locally against the compose database:

```sh
docker compose up -d db migrate
DATABASE_URL=postgresql://episteme:<password>@localhost:5433/episteme uvicorn episteme.web.app:app --reload --port 8200
```

(The compose `db` service is published to the host at `127.0.0.1:5433` for direct
access, e.g. from pgAdmin — credentials in `.env`.)
