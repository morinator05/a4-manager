"""The daily job: flag missed duties and pick the people for today and this week."""

import logging
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal
from app.services import duties

log = logging.getLogger(__name__)

# Arbitrary, app-wide id for the Postgres advisory lock: the daily job and the dashboard's
# slot filling never run at the same time.
DAILY_JOB_LOCK_ID = 4004


async def run_daily_job(session: AsyncSession, now: datetime) -> None:
    """Strike overdue duties, then make sure today's and this week's people are picked.

    Safe to run any number of times; it also commits. Runs on a schedule, on startup and
    from the Flursprecher page.
    """
    # The scheduler, startup and page loads may run it at the same time.
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": DAILY_JOB_LOCK_ID})
    missed = await duties.process_overdue_duties(session, now)
    await duties.fill_current_slots(session, now.date())
    await session.commit()
    if missed:
        log.info("Marked %d assignments as missed", len(missed))


async def fill_missing_slots(session: AsyncSession, today: date) -> None:
    """Pick the people for today and this week if that hasn't happened yet; commits.

    For page views: unlike `run_daily_job`, it never marks duties missed or gives strikes.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": DAILY_JOB_LOCK_ID})
    await duties.fill_current_slots(session, today)
    await session.commit()


async def run_scheduled_job() -> None:
    """`run_daily_job` with its own session, for the scheduler (no request around it)."""
    async with SessionLocal() as session:
        await run_daily_job(session, settings.now())
