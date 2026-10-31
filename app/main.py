"""The FastAPI app: middleware, static files, routes and the daily background job."""

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import SessionLocal
from app.deps import LoginRequired
from app.services import users
from app.services.jobs import JOB_MINUTE, run_scheduled_job
from app.web import router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up on startup, run the daily job while the app is up.

    Creates the first Flursprecher if configured, then runs the daily job now and on a
    schedule.
    """
    async with SessionLocal() as session:
        # Invalid ADMIN_* settings stop the startup, like a missing SECRET_KEY does.
        admin = await users.create_initial_admin(session)
        if admin:
            await session.commit()
            log.info("Created Flursprecher @%s", admin.username)
    scheduler = None
    if settings.run_scheduler:
        scheduler = AsyncIOScheduler(timezone=settings.tz)
        # Shortly after midnight (new day/week) and right after the deadline hour.
        for hour in sorted({0, settings.deadline_hour}):
            trigger = CronTrigger(hour=hour, minute=JOB_MINUTE, timezone=settings.tz)
            # A run that is late (busy event loop, suspended machine) still happens, once.
            scheduler.add_job(run_scheduled_job, trigger, misfire_grace_time=3600, coalesce=True)
        scheduler.start()
        await run_scheduled_job()
    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title="A4 Manager", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="a4_session",
    same_site="lax",
    https_only=settings.session_https_only,
    max_age=int(timedelta(days=90).total_seconds()),
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)


@app.exception_handler(LoginRequired)
async def redirect_to_login(request: Request, exc: LoginRequired):
    """Send people who aren't logged in to the login page, and back here afterwards."""
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?{urlencode({'next': here})}", status_code=303)
