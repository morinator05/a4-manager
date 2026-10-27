"""Database side of the duty schedule: materializing slots, marking done, missed duties."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Assignment, Duty, Slot, SlotEligible, Status, User
from app.services import scheduling, strikes
from app.services.scheduling import Balance, Candidate


class DutyError(Exception):
    pass


def people_per_slot(duty: Duty) -> int:
    """How many people are put on a slot. Only one of them has to actually do it."""
    if duty is Duty.kitchen:
        return settings.kitchen_people_per_day
    return settings.trash_people_per_week


def slot_bounds(duty: Duty, day: date) -> tuple[date, date]:
    if duty is Duty.kitchen:
        return day, day
    start = scheduling.week_start(day)
    return start, start + timedelta(days=6)


def deadline(slot: Slot) -> datetime:
    day = slot.end_date + timedelta(days=settings.deadline_days_after)
    return datetime.combine(day, time(settings.deadline_hour), settings.tz)


async def load_candidates(session: AsyncSession) -> list[Candidate]:
    users = await session.scalars(
        select(User).where(User.active.is_(True)).options(selectinload(User.away_periods))
    )
    return [
        Candidate(
            id=u.id,
            room=u.room_number,
            away=tuple((a.start_date, a.end_date) for a in u.away_periods),
        )
        for u in users
    ]


def slot_credit(slot: Slot, a: Assignment) -> Fraction:
    """Fairness credit an assignment earns.

    Whoever did it gets 1, the others 0. While a slot is still open, its pending people
    share the job, so tomorrow's pick doesn't choose them again before anyone claimed it.
    """
    if slot.is_penalty:
        return Fraction(0)
    if a.status is Status.done:
        return Fraction(1)
    if a.status is Status.pending and not any(x.status is Status.done for x in slot.assignments):
        return Fraction(1, sum(x.status is Status.pending for x in slot.assignments))
    return Fraction(0)


async def load_balances(session: AsyncSession, duty: Duty) -> dict[int, Balance]:
    balances: dict[int, Balance] = {}

    slots = (
        await session.scalars(
            select(Slot)
            .where(Slot.duty == duty)
            .options(selectinload(Slot.eligible), selectinload(Slot.assignments))
        )
    ).all()
    for slot in slots:
        if not slot.is_penalty:
            scheduling.accrue(balances, [e.user_id for e in slot.eligible])
        for a in slot.assignments:
            if a.user_id is None:
                continue
            b = balances.setdefault(a.user_id, Balance())
            b.last_duty = (
                slot.start_date if b.last_duty is None else max(b.last_duty, slot.start_date)
            )
            b.credit += slot_credit(slot, a)
    return balances


async def get_slot(session: AsyncSession, duty: Duty, day: date) -> Slot | None:
    start, _ = slot_bounds(duty, day)
    return await session.scalar(
        select(Slot)
        .where(Slot.duty == duty, Slot.start_date == start)
        .options(selectinload(Slot.assignments).selectinload(Assignment.user))
    )


async def materialize(session: AsyncSession, duty: Duty, day: date) -> Slot:
    """Create the slot containing `day` and pick its people.

    An existing slot that has fewer people than needed (e.g. it was created before
    enough residents existed) is topped up.
    """
    existing = await get_slot(session, duty, day)
    n = people_per_slot(duty)
    if existing is not None:
        already_done = any(a.status is Status.done for a in existing.assignments)
        if existing.is_penalty or already_done or len(existing.assignments) >= n:
            return existing
        await _top_up(session, existing, n - len(existing.assignments))
        return await get_slot(session, duty, day)

    start, end = slot_bounds(duty, day)
    candidates = await load_candidates(session)
    rooms = {c.id: c.room for c in candidates}
    eligible = [c.id for c in candidates if c.is_available(start, end)]

    balances = await load_balances(session, duty)
    scheduling.accrue(balances, eligible)
    chosen = scheduling.pick(balances, eligible, n, rooms)

    slot = Slot(duty=duty, start_date=start, end_date=end, is_penalty=False)
    slot.eligible = [SlotEligible(user_id=uid) for uid in eligible]
    slot.assignments = [Assignment(user_id=uid) for uid in chosen]
    session.add(slot)
    await session.flush()
    return await get_slot(session, duty, day)


async def _top_up(session: AsyncSession, slot: Slot, missing: int) -> None:
    candidates = await load_candidates(session)
    await session.refresh(slot, ["eligible"])
    known = {e.user_id for e in slot.eligible}
    for c in candidates:
        if c.id not in known and c.is_available(slot.start_date, slot.end_date):
            slot.eligible.append(SlotEligible(user_id=c.id))
    await session.flush()
    await _assign_from_eligible(session, slot, missing, {c.id: c.room for c in candidates})


async def _assign_from_eligible(
    session: AsyncSession, slot: Slot, count: int, rooms: dict[int, str]
) -> None:
    """Put the `count` most indebted eligible users that aren't on `slot` yet on it."""
    balances = await load_balances(session, slot.duty)
    taken = {a.user_id for a in slot.assignments}
    pool = [e.user_id for e in slot.eligible if e.user_id is not None and e.user_id not in taken]
    for uid in scheduling.pick(balances, pool, count, rooms):
        slot.assignments.append(Assignment(user_id=uid))
    await session.flush()


async def fill_current(session: AsyncSession, today: date) -> None:
    """Make sure today's kitchen duty and this week's trash duty have their people."""
    for duty in Duty:
        await materialize(session, duty, today)


async def reconcile(session: AsyncSession, today: date) -> None:
    """Replace people in current/upcoming slots who became unavailable.

    Called after someone marks themselves away, is deactivated or deleted.
    """
    candidates = {c.id: c for c in await load_candidates(session)}
    rooms = {uid: c.room for uid, c in candidates.items()}

    def is_available(user_id: int | None, slot: Slot) -> bool:
        candidate = candidates.get(user_id)
        return candidate is not None and candidate.is_available(slot.start_date, slot.end_date)

    slots = (
        await session.scalars(
            select(Slot)
            .where(Slot.end_date >= today, Slot.is_penalty.is_(False))
            .options(selectinload(Slot.eligible), selectinload(Slot.assignments))
        )
    ).all()
    for slot in slots:
        gone = [
            a
            for a in slot.assignments
            if a.status is Status.pending and not is_available(a.user_id, slot)
        ]
        if not gone:
            continue
        for a in gone:
            slot.assignments.remove(a)
            await session.delete(a)
        slot.eligible = [e for e in slot.eligible if is_available(e.user_id, slot)]
        await session.flush()
        await _assign_from_eligible(session, slot, len(gone), rooms)


async def process_overdue(session: AsyncSession, now: datetime) -> list[Assignment]:
    """Mark pending duties past their deadline as missed and strike the assignees."""
    pending = (
        await session.scalars(
            select(Assignment)
            .join(Assignment.slot)
            .where(Assignment.status == Status.pending)
            .options(selectinload(Assignment.slot))
        )
    ).all()
    missed = [a for a in pending if deadline(a.slot) <= now]
    for a in missed:
        a.status = Status.missed
        if a.user_id is not None:
            label = a.slot.duty.label
            await strikes.issue(
                session,
                user_id=a.user_id,
                reason=f"{label} am {a.slot.start_date:%d.%m.%Y} nicht eingetragen",
                auto=True,
                assignment_id=a.id,
                today=now.date(),
            )
    await session.flush()
    return missed


async def get_assignment(session: AsyncSession, assignment_id: int) -> Assignment:
    a = await session.scalar(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .options(
            selectinload(Assignment.slot)
            .selectinload(Slot.assignments)
            .selectinload(Assignment.user)
        )
    )
    if a is None:
        raise DutyError("Dienst nicht gefunden")
    return a


def can_mark_done(a: Assignment, actor: User, now: datetime) -> bool:
    # Only one person does a slot: once somebody claimed it, it's closed.
    if any(x.status is Status.done for x in a.slot.assignments):
        return False
    if actor.is_admin:
        return True
    if a.status is not Status.pending:
        return False
    if not (a.slot.start_date <= now.date() and now < deadline(a.slot)):
        return False
    # Residents can only mark their own duty; the Flursprecher can mark anyone's.
    return a.user_id == actor.id


async def _revoke_strikes(session: AsyncSession, a: Assignment, now: datetime) -> None:
    for s in await strikes.for_assignment(session, a.id):
        if s.is_active:
            s.revoked_at = now


async def mark_done(session: AsyncSession, a: Assignment, actor: User, now: datetime) -> None:
    """`a`'s person claims the slot; everybody else on it is off the hook."""
    if not can_mark_done(a, actor, now):
        raise DutyError("Du kannst diesen Dienst nicht abhaken")
    await _revoke_strikes(session, a, now)
    a.status = Status.done
    a.completed_at = now
    a.completed_by_id = actor.id
    a.disputed_at = None
    for other in a.slot.assignments:
        if other is not a and other.status in (Status.pending, Status.missed):
            await _revoke_strikes(session, other, now)
            other.status = Status.covered
    await session.flush()


async def excuse(session: AsyncSession, a: Assignment, actor: User, now: datetime) -> None:
    """Admin only: nobody is blamed, but the person gets no credit either."""
    if not actor.is_admin:
        raise DutyError("Nur der Flursprecher kann Dienste entschuldigen")
    await _revoke_strikes(session, a, now)
    a.status = Status.excused
    a.completed_at = now
    a.completed_by_id = actor.id
    await session.flush()


async def dispute(session: AsyncSession, a: Assignment, actor: User, now: datetime) -> None:
    """Admin only: it was marked done but not actually done. The claimer gets a strike."""
    if not actor.is_admin:
        raise DutyError("Nur der Flursprecher kann Dienste beanstanden")
    if a.status is not Status.done:
        raise DutyError("Nur erledigte Dienste können beanstandet werden")
    a.status = Status.missed
    a.disputed_at = now
    await session.flush()
    if a.user_id is not None:
        await strikes.issue(
            session,
            user_id=a.user_id,
            reason=(
                f"{a.slot.duty.label} am {a.slot.start_date:%d.%m.%Y} als erledigt "
                "eingetragen, aber nicht gemacht"
            ),
            assignment_id=a.id,
            issued_by_id=actor.id,
            today=now.date(),
        )


@dataclass
class PreviewEntry:
    start: date
    end: date
    user_ids: list[int]
    is_penalty: bool = False


async def preview(session: AsyncSession, duty: Duty, start: date, end: date) -> list[PreviewEntry]:
    """Expected schedule for slots starting in [start, end] that don't exist yet."""
    n = people_per_slot(duty)
    step = timedelta(days=1 if duty is Duty.kitchen else 7)
    # Trash weeks start on Monday: skip the week that is already running on `start`.
    first, _ = slot_bounds(duty, start)
    if first < start:
        first += step

    existing = {
        s.start_date: s
        for s in await session.scalars(
            select(Slot)
            .where(Slot.duty == duty, Slot.start_date >= first, Slot.start_date <= end)
            .options(selectinload(Slot.assignments))
        )
    }
    entries: list[PreviewEntry] = []
    todo: list[tuple[date, date]] = []
    day = first
    while day <= end:
        bounds = slot_bounds(duty, day)
        if day in existing:
            s = existing[day]
            entries.append(
                PreviewEntry(*bounds, [a.user_id for a in s.assignments if a.user_id], s.is_penalty)
            )
        else:
            todo.append(bounds)
            entries.append(PreviewEntry(*bounds, []))
        day += step

    # plan() returns one list of user ids per `todo` slot, in the same order.
    planned = iter(
        scheduling.plan(
            await load_balances(session, duty), await load_candidates(session), todo, n
        )
    )
    for e in entries:
        if (e.start, e.end) in todo:
            e.user_ids = next(planned)
    return entries
