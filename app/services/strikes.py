from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Assignment, AwayPeriod, Duty, Slot, Strike, User
from app.services import scheduling


async def for_assignment(session: AsyncSession, assignment_id: int) -> list[Strike]:
    return list(await session.scalars(select(Strike).where(Strike.assignment_id == assignment_id)))


async def active_strikes(session: AsyncSession, user_id: int) -> list[Strike]:
    return list(
        await session.scalars(
            select(Strike)
            .where(
                Strike.user_id == user_id,
                Strike.revoked_at.is_(None),
                Strike.redeemed_by_assignment_id.is_(None),
            )
            .order_by(Strike.created_at, Strike.id)
        )
    )


async def issue(
    session: AsyncSession,
    *,
    user_id: int,
    reason: str,
    today: date,
    auto: bool = False,
    assignment_id: int | None = None,
    issued_by_id: int | None = None,
) -> Strike:
    strike = Strike(
        user_id=user_id,
        reason=reason,
        auto=auto,
        assignment_id=assignment_id,
        issued_by_id=issued_by_id,
    )
    session.add(strike)
    await session.flush()
    await check_threshold(session, user_id, today)
    return strike


async def revoke(session: AsyncSession, strike: Strike, now: datetime) -> None:
    strike.revoked_at = now
    await session.flush()


async def next_free_penalty_week(session: AsyncSession, user_id: int, today: date) -> date:
    """First week after the current one without a trash slot where the user isn't away."""
    taken = set(
        await session.scalars(
            select(Slot.start_date).where(Slot.duty == Duty.trash, Slot.start_date > today)
        )
    )
    away = [
        (a.start_date, a.end_date)
        for a in await session.scalars(select(AwayPeriod).where(AwayPeriod.user_id == user_id))
    ]
    candidate = scheduling.Candidate(id=user_id, room="", away=tuple(away))
    week = scheduling.week_start(today) + timedelta(days=7)
    while week in taken or not candidate.is_available(week, week + timedelta(days=6)):
        week += timedelta(days=7)
    return week


async def check_threshold(session: AsyncSession, user_id: int, today: date) -> Assignment | None:
    """Turn `strike_threshold` active strikes into a penalty Mülldienst week."""
    active = await active_strikes(session, user_id)
    if len(active) < settings.strike_threshold:
        return None
    user = await session.get(User, user_id)
    if user is None:
        return None

    week = await next_free_penalty_week(session, user_id, today)
    assignment = Assignment(user_id=user_id)
    session.add(
        Slot(
            duty=Duty.trash,
            start_date=week,
            end_date=week + timedelta(days=6),
            is_penalty=True,
            assignments=[assignment],
        )
    )
    await session.flush()
    for s in active[: settings.strike_threshold]:
        s.redeemed_by_assignment_id = assignment.id
    await session.flush()
    return assignment
