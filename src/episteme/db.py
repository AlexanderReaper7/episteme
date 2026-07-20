from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import settings

engine = create_async_engine(settings.sqlalchemy_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Schema creation lives in episteme/migrations/ (Alembic), applied by bootstrap.
# There is deliberately no create_all here: it cannot alter existing tables, so
# having both it and migrations would mean two sources of truth for the schema
# that silently disagree on any database that is not brand new.
