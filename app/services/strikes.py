"""Strikes, and the penalty Mülldienst week they turn into."""

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import lock_schedule
from app.models import Assignment, AwayPeriod, Duty, Slot, Status, Strike, User
from app.services import scheduling


async def get_strikes_for_assignment(session: AsyncSession, assignment_id: int) -> list[Strike]:
    """All strikes given for one duty, active or not."""
    return list(await session.scalars(select(Strike).where(Strike.assignment_id == assignment_id)))


async def get_active_strikes(session: AsyncSession, user_id: int) -> list[Strike]:
    """The user's strikes that still count, oldest first."""
    return list(
        await session.scalars(
            select(Strike)
            .where(Strike.user_id == user_id, Strike.is_active)
            .order_by(Strike.created_at, Strike.id)
        )
    )


async def count_active_strikes(session: AsyncSession) -> dict[int, int]:
    """Number of active strikes per user id; users without any are left out."""
    rows = await session.execute(
        select(Strike.user_id, func.count()).where(Strike.is_active).group_by(Strike.user_id)
    )
    return {user_id: n for user_id, n in rows}


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
    await lock_schedule(session)
    strike = Strike(
        user_id=user_id,
        reason=reason,
        auto=auto,
        assignment_id=assignment_id,
        issued_by_id=issued_by_id,
    )
    session.add(strike)
    await session.flush()
    await add_penalty_week_if_due(session, user_id, today)
    return strike


async def revoke_strike(session: AsyncSession, strike: Strike, now: datetime) -> None:
    """Take a strike back, so it no longer counts.

    If it was already paid off by a penalty week that hasn't started yet, that week is
    cancelled, so the other strikes it paid off count again (and may still add up to one).
    """
    if strike.revoked_at is not None:
        return
    await lock_schedule(session)
    today = now.date()
    if strike.redeemed_by_assignment_id is not None:
        penalty = await session.scalar(
            select(Assignment)
            .where(Assignment.id == strike.redeemed_by_assignment_id)
            .options(selectinload(Assignment.slot).selectinload(Slot.assignments))
        )
        if penalty.status is Status.pending and penalty.slot.start_date > today:
            await cancel_penalty_week(session, penalty.slot)
    strike.revoked_at = now
    await session.flush()
    await add_penalty_week_if_due(session, strike.user_id, today)


async def revoke_strikes_for_assignment(
    session: AsyncSession, assignment_id: int, now: datetime
) -> None:
    """Take back the strikes given for this duty (it was done or excused after all)."""
    for s in await get_strikes_for_assignment(session, assignment_id):
        await revoke_strike(session, s, now)


async def get_open_penalty_weeks(session: AsyncSession, user_id: int, today: date) -> list[Slot]:
    """The user's penalty weeks that aren't over and not done yet, with their people loaded."""
    return list(
        await session.scalars(
            select(Slot)
            .join(Slot.assignments)
            .where(
                Slot.is_penalty.is_(True),
                Slot.end_date >= today,
                Assignment.user_id == user_id,
                Assignment.status == Status.pending,
            )
            .options(selectinload(Slot.assignments))
        )
    )


async def cancel_penalty_week(session: AsyncSession, slot: Slot) -> None:
    """Delete a penalty week; the strikes it paid off count again.

    That week gets a regular Mülldienst instead, once it comes.
    """
    for a in slot.assignments:
        for s in await session.scalars(
            select(Strike).where(Strike.redeemed_by_assignment_id == a.id)
        ):
            s.redeemed_by_assignment_id = None
    await session.delete(slot)
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


async def add_penalty_week_if_due(session: AsyncSession, user_id: int, today: date) -> None:
    """Once the user has `strike_threshold` active strikes, add a penalty Mülldienst week.

    The penalty week replaces the regular Mülldienst of that week. The oldest strikes are
    marked as paid off by it, so they no longer count. Paused residents get theirs when
    they are resumed.
    """
    await lock_schedule(session)
    active = await get_active_strikes(session, user_id)
    if len(active) < settings.strike_threshold:
        return
    user = await session.get(User, user_id)
    if user is None or not user.active:
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
