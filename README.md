# Episteme

Self-hosted, LLM-powered personalized newsfeed. Ingests news from many
sources, processes it overnight with a local LLM, and produces a healthy, finite,
learning-focused feed of newly written articles.

Full design: [episteme-architecture.md](episteme-architecture.md).
**Current state: Phases 1–2 built.** Phase 1 = ingestion (RSS + full-article
extraction) and the feed UI; Phase 2 = the overnight LLM pipeline (embed → cluster →
triage → write) producing generated articles above an aggregation stream, verified
end-to-end against a local llama-server. Next: Phase 3 (personalization & feedback).
See CLAUDE.md for the live operational state and how to run the pipeline.

## Quickstart

```sh
cp .env.example .env      # set POSTGRES_PASSWORD
docker compose up --build
```

Open <http://127.0.0.1:8200>. The worker ingests all seeded feeds every 30 minutes
(`INGEST_CRON` to change). To trigger a run immediately:

```sh
docker compose exec worker procrastinate --app=episteme.worker.app.app defer episteme.ingest_all '{}'
```

## Layout

```
src/episteme/
├── config.py        settings (env / .env)
├── models.py        SQLAlchemy models (Source, SourceItem, Story, Article)
├── db.py            engine + init
├── seeds.py         initial source list
├── bootstrap.py     one-shot schema/seed (compose `migrate` service)
├── ingest/          source adapters (base protocol, registry, rss) + polite HTTP
├── llm/             LLM gateway (role→model), structured-output schemas
├── worker/          procrastinate app, ingestion + pipeline tasks
└── web/             FastAPI app, Jinja2 templates, htmx feed UI
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
DATABASE_URL=postgresql://episteme:<password>@localhost:5432/episteme uvicorn episteme.web.app:app --reload --port 8200
```

(For local Postgres access, add `ports: ["127.0.0.1:5432:5432"]` to the `db` service
or use a compose override file.)
