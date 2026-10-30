"""Seed the database with test residents, optionally with a simulated history.

    uv run python scripts/seed.py                  # 15 residents
    uv run python scripts/seed.py -n 20 --password test1234
    uv run python scripts/seed.py --days 30        # plus a simulated month of duties

Usernames are the lowercase first names; existing usernames are skipped, so it is
safe to run more than once. Current slots that nobody has finished yet are picked
again, so everyone gets their share (on startup the app may have created them while
only the Flursprecher existed). Afterwards today's duties are assigned.

`--days N` needs a database without history (`docker compose down -v`). It plays the
last N days through the app's own rules: the daily job at 00:05 and 12:05, each duty
done the same evening, the next morning, or missed (giving strikes and penalty weeks),
depending on how reliable each resident is, and a few absences. Today's kitchen duty
is left open. `--random-seed` gives the same history again.
"""

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import SessionLocal
from app.models import Assignment, Duty, Slot, Status, Strike, User
from app.services import duties, jobs, users

# fmt: off
FIRST_NAMES = [
    "Anna", "Ben", "Clara", "David", "Emma", "Felix", "Greta", "Hannes", "Ida", "Jonas",
    "Kim", "Lukas", "Mia", "Noah", "Olivia", "Paul", "Quinn", "Rosa", "Simon", "Tara",
    "Uwe", "Vera", "Wim", "Xenia", "Yusuf", "Zoe",
]
LAST_NAMES = [
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
    "Schulz", "Hoffmann", "Koch", "Richter", "Klein", "Wolf", "Neumann", "Schwarz",
]
# fmt: on

# Of the duties a resident does, this part is marked the same evening, the rest the next
# morning (still before the deadline).
SAME_DAY = 0.85


def make_datetime(day: date, hour: int, minute: int = 0) -> datetime:
    """`day` at `hour:minute` in the app's time zone."""
    return datetime.combine(day, time(hour, minute), settings.tz)


async def reset_open_slots(session: AsyncSession, today: date) -> int:
    """Delete current and future regular slots where nobody has done anything yet.

    The next daily job creates them again, with everyone who exists by then.
    """
    slots = await session.scalars(
        select(Slot)
        .where(Slot.end_date >= today, Slot.is_penalty.is_(False))
        .options(selectinload(Slot.assignments))
    )
    stale = [s.id for s in slots if all(a.status is Status.pending for a in s.assignments)]
    if stale:
        await session.execute(delete(Slot).where(Slot.id.in_(stale)))
        await session.commit()
        session.expire_all()
    return len(stale)


async def run_daily_job_at(session: AsyncSession, now: datetime) -> None:
    """The daily job as if it ran at `now`; its strikes are dated `now` too."""
    last = await session.scalar(select(func.max(Strike.id))) or 0
    await jobs.run_daily_job(session, now)
    await session.execute(update(Strike).where(Strike.id > last).values(created_at=now))
    await session.commit()
    session.expire_all()


async def mark_done_if_open(session: AsyncSession, assignment_id: int, now: datetime) -> None:
    """The assignment's person marks it done at `now`, if it is still open."""
    a = await duties.get_assignment(session, assignment_id)
    if a.status is not Status.pending or a.slot.claimed_assignment is not None:
        return
    actor = await session.get(User, a.user_id)
    await duties.mark_duty_done(session, a, actor, now)
    await session.commit()
    session.expire_all()


async def simulate_duty_outcome(
    session: AsyncSession,
    duty: Duty,
    day: date,
    now: datetime,
    reliability: dict[int, float],
    rng: random.Random,
    late: dict[date, list[int]],
) -> None:
    """Let one of the slot's people do it now, tomorrow morning or never, by their reliability."""
    slot = await duties.get_slot(session, duty, day)
    if slot is None:
        return
    pending = [a for a in slot.assignments if a.status is Status.pending and a.user_id]
    if not pending:
        return
    doer = rng.choice(pending)
    x = rng.random()
    r = reliability.get(doer.user_id, 0.9)
    if x < r * SAME_DAY:
        await mark_done_if_open(session, doer.id, now)
    elif x < r:
        late.setdefault(now.date() + timedelta(days=1), []).append(doer.id)


async def simulate_history(session: AsyncSession, days: int, rng: random.Random) -> None:
    """Play the last `days` days, ending now (see the module docstring)."""
    now = settings.now()
    today = now.date()
    start = today - timedelta(days=days)
    await session.execute(
        update(User)
        .where(User.created_at > make_datetime(start, 0))
        .values(created_at=make_datetime(start, 0))
    )
    residents = list(await session.scalars(select(User).where(User.active.is_(True))))
    reliability = {u.id: rng.uniform(0.75, 0.98) for u in residents}

    # A few absences, one of them usually around today.
    for u in rng.sample([u for u in residents if not u.is_admin], k=min(3, len(residents) - 1)):
        away_start = start + timedelta(days=rng.randrange(days + 4))
        away_end = away_start + timedelta(days=rng.randrange(4, 10))
        await users.add_away_period(session, u, away_start, away_end, start)
    await session.commit()

    late: dict[date, list[int]] = {}
    day = start
    while day <= today:
        steps = [
            (make_datetime(day, 0, 5), "job"),
            (make_datetime(day, 9), "late"),
            (make_datetime(day, 12, 5), "job"),
            (make_datetime(day, 18), "trash"),
            (make_datetime(day, 20), "kitchen"),
        ]
        for when, step in steps:
            if when >= now:
                break
            if step == "job":
                await run_daily_job_at(session, when)
            elif step == "late":
                for assignment_id in late.pop(day, []):
                    await mark_done_if_open(session, assignment_id, when)
            elif step == "trash" and duties.get_slot_bounds(Duty.trash, day)[1] == day:
                await simulate_duty_outcome(session, Duty.trash, day, when, reliability, rng, late)
            elif step == "kitchen" and day < today:
                await simulate_duty_outcome(
                    session, Duty.kitchen, day, when, reliability, rng, late
                )
        day += timedelta(days=1)

    counts = dict(
        (
            await session.execute(
                select(Assignment.status, func.count()).group_by(Assignment.status)
            )
        ).all()
    )
    strikes = await session.scalar(select(func.count(Strike.id)))
    summary = ", ".join(
        f"{n} {s.value}" for s, n in sorted(counts.items(), key=lambda x: x[0].value)
    )
    print(f"Simulated {days} days from {start:%d.%m.%Y}: {summary}; {strikes} strikes")


async def main(count: int, password: str, days: int, seed: int | None) -> None:
    """Add the test residents, optionally simulate a history, then run the daily job."""
    if count > len(FIRST_NAMES):
        sys.exit(f"At most {len(FIRST_NAMES)} residents supported")
    async with SessionLocal() as session:
        reopened = await reset_open_slots(session, settings.today())
        if days and await session.scalar(select(func.count(Slot.id))):
            sys.exit("--days needs a database without history: docker compose down -v")
        existing = set(await session.scalars(select(User.username)))
        added = 0
        for i in range(count):
            first = FIRST_NAMES[i]
            username = first.lower()
            if username in existing:
                continue
            await users.create_user(
                session,
                name=f"{first} {LAST_NAMES[i % len(LAST_NAMES)]}",
                room_number=f"A4{i + 1:02d}",
                username=username,
                password=password,
            )
            added += 1
        await session.commit()
        if days:
            await simulate_history(session, days, random.Random(seed))
        await jobs.run_daily_job(session, settings.now())
    print(f"{added} residents added ({count - added} already existed), password: {password}")
    if reopened:
        print(f"{reopened} open slots picked again")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--count", type=int, default=15)
    parser.add_argument("--password", default="passwort123")
    parser.add_argument("--days", type=int, default=0, help="simulate this many past days")
    parser.add_argument("--random-seed", type=int, help="same number, same history")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.password, args.days, args.random_seed))
