# Episteme

Self-hosted, LLM-powered personalized science newsfeed. Ingests news from many
sources, processes it overnight with a local LLM, and produces a healthy, finite,
learning-focused feed of newly written articles.

Full design: [episteme-architecture.md](episteme-architecture.md).
**Current state: Phase 1** — ingestion (RSS + full-article extraction) and a minimal
feed UI over raw source items. No LLM processing yet.

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
├── models.py        SQLAlchemy models (Source, SourceItem)
├── db.py            engine + init
├── seeds.py         initial source list
├── bootstrap.py     one-shot schema/seed (compose `migrate` service)
├── ingest/          source adapters (base protocol, registry, rss)
├── worker/          procrastinate app + ingestion tasks
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
