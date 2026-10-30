"""Test setup: a separate `a4_test` database, recreated empty for every test."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://a4:a4@localhost:5432/a4_test")
os.environ["RUN_SCHEDULER"] = "false"
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-0123456789")

import asyncpg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db
from app.config import settings
from app.models import Base

# An exported DATABASE_URL wins over the default above, and every test drops all tables.
if not make_url(settings.database_url).database.endswith("_test"):
    pytest.exit("DATABASE_URL must point at a *_test database: the tests drop all tables")


async def ensure_test_database() -> None:
    """Create the test database if it doesn't exist yet."""
    url = make_url(settings.database_url)
    params = dict(user=url.username, password=url.password, host=url.host, port=url.port)
    try:
        conn = await asyncpg.connect(**params, database=url.database)
    except asyncpg.InvalidCatalogNameError:
        conn = await asyncpg.connect(**params, database="postgres")
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    await conn.close()


@pytest.fixture
async def session_factory(monkeypatch):
    """Empty tables for each test; web requests use them too (via `db.SessionLocal`)."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        await ensure_test_database()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except OSError:
        if os.environ.get("CI"):  # in CI a missing database is an error, not a skip
            raise
        pytest.skip("test database not reachable (docker compose up -d db)")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", factory)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    """A database session for the test."""
    async with session_factory() as s:
        yield s
