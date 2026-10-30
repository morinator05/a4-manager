"""Strikes, and the penalty Mülldienst week they turn into."""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Assignment, AwayPeriod, Duty, Slot, Strike, User
from app.services import scheduling


async def get_strikes_for_assignment(session: AsyncSession, assignment_id: int) -> list[Strike]:
    """All strikes given for one duty, active or not."""
    return list(await session.scalars(select(Strike).where(Strike.assignment_id == assignment_id)))


async def get_active_strikes(session: AsyncSession, user_id: int) -> list[Strike]:
    """The user's strikes that still count, oldest first."""
    return list(
        await session.scalars(
            select(Strike)
            .where(
                Strike.user_id == user_id,
                # The same rule as `Strike.is_active`, as SQL.
                Strike.revoked_at.is_(None),
                Strike.redeemed_by_assignment_id.is_(None),
            )
            .order_by(Strike.created_at, Strike.id)
        )
    )


async def issue_strike(
    session: AsyncSession,
    *,
    user_id: int,
    reason: str,
    today: date,
    auto: bool = False,
    assignment_id: int | None = None,
    issued_by_id: int | None = None,
) -> Strike:
    """Give a strike; if it is the user's `strike_threshold`-th, a penalty week follows.

    `today` is used to find the penalty week: the first free one after this week.
    """
    strike = Strike(
        user_id=user_id,
        reason=reason,
        auto=auto,
        assignment_id=assignment_id,
        issued_by_id=issued_by_id,
    )
    session.add(strike)
    await session.flush()
    await check_strike_threshold(session, user_id, today)
    return strike


async def revoke_strike(session: AsyncSession, strike: Strike, now: datetime) -> None:
    """Take a strike back, so it no longer counts."""
    strike.revoked_at = now
    await session.flush()


async def find_free_penalty_week(session: AsyncSession, user_id: int, today: date) -> date:
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
    week = scheduling.get_week_start(today) + timedelta(days=7)
    while week in taken or scheduling.is_away_during(away, *scheduling.get_week_bounds(week)):
        week += timedelta(days=7)
    return week


async def check_strike_threshold(session: AsyncSession, user_id: int, today: date) -> None:
    """Turn `strike_threshold` active strikes into a penalty Mülldienst week.

    The penalty week replaces the regular Mülldienst of that week. The oldest strikes are
    marked as paid off by it, so they no longer count.
    """
    active = await get_active_strikes(session, user_id)
    if len(active) < settings.strike_threshold:
        return
    if await session.get(User, user_id) is None:
        return

    week = await find_free_penalty_week(session, user_id, today)
    start, end = scheduling.get_week_bounds(week)
    assignment = Assignment(user_id=user_id)
    session.add(
        Slot(
            duty=Duty.trash,
            start_date=start,
            end_date=end,
            is_penalty=True,
            assignments=[assignment],
        )
    )
    await session.flush()
    for s in active[: settings.strike_threshold]:
        s.redeemed_by_assignment_id = assignment.id
    await session.flush()
