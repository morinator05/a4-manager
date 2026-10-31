"""The daily job: flag missed duties and pick the people for today and this week."""

import logging
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, lock_schedule
from app.services import duties

log = logging.getLogger(__name__)

# The job runs at this minute shortly after midnight (new day/week) and after the deadline.
JOB_MINUTE = 5


async def run_daily_job(session: AsyncSession, now: datetime) -> None:
    """Strike overdue duties, then make sure today's and this week's people are picked.

    Safe to run any number of times; it also commits. Runs on a schedule, on startup and
    from the Flursprecher page.
    """
    await lock_schedule(session)
    missed = await duties.process_overdue_duties(session, now)
    await duties.fill_current_slots(session, now.date())
    await session.commit()
    if missed:
        log.info("Marked %d assignments as missed", len(missed))


async def fill_missing_slots(session: AsyncSession, today: date) -> None:
    """Pick the people for today and this week if that hasn't happened yet; commits.

    For page views: unlike `run_daily_job`, it never marks duties missed or gives strikes.
    """
    await duties.fill_current_slots(session, today)
    await session.commit()


async def run_scheduled_job() -> None:
    """`run_daily_job` with its own session, for the scheduler (no request around it).

    Errors are logged, not raised: a bad row must not stop the app from starting, and the
    next run tries again.
    """
    try:
        async with SessionLocal() as session:
            await run_daily_job(session, settings.now())
    except Exception:
        log.exception("Daily job failed")
