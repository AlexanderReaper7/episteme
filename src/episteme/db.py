from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import settings

engine = create_async_engine(settings.sqlalchemy_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from . import models

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(models.Base.metadata.create_all)
