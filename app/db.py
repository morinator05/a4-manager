"""Database engine and sessions."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url)
# expire_on_commit=False: objects stay readable after commit, without another query
# (an implicit reload would fail in async code).
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request; closed (and rolled back if not committed) afterwards."""
    async with SessionLocal() as session:
        yield session
