import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.deps import LoginRequired
from app.services.jobs import scheduled_tick
from app.web import router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if settings.run_scheduler:
        scheduler = AsyncIOScheduler(timezone=settings.tz)
        # Shortly after midnight (new day/week) and right after the deadline hour.
        for hour in sorted({0, settings.deadline_hour}):
            scheduler.add_job(scheduled_tick, CronTrigger(hour=hour, minute=5, timezone=settings.tz))
        scheduler.start()
        await scheduled_tick()
    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title="A4 Manager", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="a4_session",
    same_site="lax",
    max_age=60 * 60 * 24 * 90,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
