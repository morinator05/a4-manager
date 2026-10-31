"""The database connection, a session per request, and the lock around schedule changes."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url)
# expire_on_commit=False: objects stay readable after commit, without another query
# (an implicit reload would fail in async code).
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Arbitrary, app-wide id for the Postgres advisory lock taken by `lock_schedule`.
SCHEDULE_LOCK_ID = 4004


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request; closed (and rolled back if not committed) afterwards."""
    async with SessionLocal() as session:
        yield session


async def lock_schedule(session: AsyncSession) -> None:
    """Wait until no other transaction is changing the schedule; held until commit/rollback.

    Everything that creates slots, puts people on them, changes an assignment's status or
    gives strikes takes it before changing anything, so two requests (or a request and the
    daily job) never interleave. Taking it again in the same transaction is a no-op.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": SCHEDULE_LOCK_ID})
