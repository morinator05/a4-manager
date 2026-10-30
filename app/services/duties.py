"""Database side of the duty schedule: creating slots, marking done, missed duties.

Slots are only created ("materialized") for the current day and week. Future ones are just
simulated for the preview, so they always reflect the latest balances and away periods.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Assignment, Duty, Slot, SlotEligible, Status, User
from app.services import scheduling, strikes
from app.services.scheduling import Balance, Candidate, Role


class DutyError(Exception):
    """An action on a duty isn't allowed; the message is German and shown as is."""


def get_people_per_slot(duty: Duty) -> int:
    if duty is Duty.kitchen:
        return settings.kitchen_people_per_day
    return settings.trash_people_per_week


def get_slot_bounds(duty: Duty, day: date) -> tuple[date, date]:
    """First and last day of the slot containing `day`: the day itself, or its Mon-Sun week."""
    if duty is Duty.kitchen:
        return day, day
    return scheduling.get_week_bounds(day)


def describe_slot(slot: Slot) -> str:
    """E.g. "Küchendienst am 05.01.2026", for strike reasons."""
    return f"{slot.duty.label} am {slot.start_date:%d.%m.%Y}"


def get_deadline(slot: Slot) -> datetime:
    """Until when the slot can be marked done; after that it counts as missed."""
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


def earns_credit(slot: Slot, a: Assignment) -> bool:
    """Only whoever did the slot earns credit, and penalty slots never give any."""
    return not slot.is_penalty and a.status is Status.done


def get_pending_share(slot: Slot, a: Assignment) -> Fraction:
    """While nobody has marked a slot done, its pending people share the job.

    Counts only for picking (`Balance.pending`), so tomorrow's pick doesn't choose them
    again before anyone claimed it; their debt doesn't change.
    """
    if slot.is_penalty or a.status is not Status.pending:
        return Fraction(0)
    if slot.claimed_assignment is not None:
        return Fraction(0)
    return Fraction(1, sum(x.status is Status.pending for x in slot.assignments))


async def load_balances(session: AsyncSession, duty: Duty) -> dict[int, Balance]:
    """Every user's fairness balance for `duty`, computed from all slots so far.

    Balances are never stored; they are always recomputed from the history.
    """
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
            scheduling.accrue_expected_work(balances, [e.user_id for e in slot.eligible])
        for a in slot.assignments:
            if a.user_id is None:
                continue
            if earns_credit(slot, a):
                scheduling.credit_done_job(balances, a.user_id, slot.start_date)
            scheduling.reserve_open_share(balances, a.user_id, get_pending_share(slot, a))
    return balances


async def get_slot(session: AsyncSession, duty: Duty, day: date) -> Slot | None:
    """The slot containing `day`, with its people loaded, or None if not created yet."""
    start, _ = get_slot_bounds(duty, day)
    return await session.scalar(
        select(Slot)
        .where(Slot.duty == duty, Slot.start_date == start)
        .options(selectinload(Slot.assignments).selectinload(Assignment.user))
    )


async def materialize_slot(session: AsyncSession, duty: Duty, day: date) -> Slot:
    """Create the slot containing `day` and pick its people.

    An existing slot that has fewer people than needed (e.g. it was created before
    enough residents existed) is topped up.
    """
    existing = await get_slot(session, duty, day)
    n = get_people_per_slot(duty)
    if existing is not None:
        is_claimed = existing.claimed_assignment is not None
        if existing.is_penalty or is_claimed or len(existing.assignments) >= n:
            return existing
        await _top_up_slot(session, existing, n - len(existing.assignments))
        return await get_slot(session, duty, day)

    start, end = get_slot_bounds(duty, day)
    candidates = await load_candidates(session)
    rooms = {c.id: c.room for c in candidates}
    eligible = [c.id for c in candidates if c.is_available(start, end)]

    balances = await load_balances(session, duty)
    scheduling.accrue_expected_work(balances, eligible)
    chosen = scheduling.pick_most_indebted(balances, eligible, n, rooms)

    slot = Slot(duty=duty, start_date=start, end_date=end, is_penalty=False)
    slot.eligible = [SlotEligible(user_id=uid) for uid in eligible]
    slot.assignments = [Assignment(user_id=uid) for uid in chosen]
    session.add(slot)
    await session.flush()
    return await get_slot(session, duty, day)


async def _top_up_slot(session: AsyncSession, slot: Slot, missing: int) -> None:
    """Add `missing` people to an existing slot, including residents added since."""
    candidates = await load_candidates(session)
    # `get_slot` doesn't load `eligible`, and async code can't load it lazily.
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
    for uid in scheduling.pick_most_indebted(balances, pool, count, rooms):
        slot.assignments.append(Assignment(user_id=uid))
    await session.flush()


async def fill_current_slots(session: AsyncSession, today: date) -> None:
    """Make sure today's kitchen duty and this week's trash duty have their people."""
    for duty in Duty:
        await materialize_slot(session, duty, today)


async def replace_unavailable_people(session: AsyncSession, today: date) -> None:
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


async def process_overdue_duties(session: AsyncSession, now: datetime) -> list[Assignment]:
    """Mark pending duties past their deadline as missed and strike the assignees."""
    pending = (
        await session.scalars(
            select(Assignment)
            .where(Assignment.status == Status.pending)
            .options(selectinload(Assignment.slot))
        )
    ).all()
    missed = [a for a in pending if get_deadline(a.slot) <= now]
    for a in missed:
        a.status = Status.missed
        if a.user_id is not None:
            await strikes.issue_strike(
                session,
                user_id=a.user_id,
                reason=f"{describe_slot(a.slot)} nicht eingetragen",
                auto=True,
                assignment_id=a.id,
                today=now.date(),
            )
    await session.flush()
    return missed


async def get_assignment(session: AsyncSession, assignment_id: int) -> Assignment:
    """The assignment with its slot and the slot's other people loaded."""
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
    """Whether `actor` may mark `a` as done now.

    Residents: only their own open duty, from its first day until the deadline.
    Flursprecher: any duty at any time, as long as nobody on the slot has done it.
    """
    if a.slot.claimed_assignment is not None:
        return False
    if actor.is_admin:
        return True
    if a.status is not Status.pending:
        return False
    if not (a.slot.start_date <= now.date() and now < get_deadline(a.slot)):
        return False
    return a.user_id == actor.id


async def _revoke_strikes(session: AsyncSession, a: Assignment, now: datetime) -> None:
    """Take back the strikes given for this duty (it was done or excused after all)."""
    for s in await strikes.get_strikes_for_assignment(session, a.id):
        if s.is_active:
            s.revoked_at = now


async def mark_duty_done(session: AsyncSession, a: Assignment, actor: User, now: datetime) -> None:
    """`a`'s person claims the slot; everybody else on it is off the hook.

    Also revokes strikes already given for the slot (the Flursprecher may mark it done
    after the deadline).
    """
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


async def excuse_duty(session: AsyncSession, a: Assignment, actor: User, now: datetime) -> None:
    """Admin only: nobody is blamed, but the person gets no credit either."""
    if not actor.is_admin:
        raise DutyError("Nur der Flursprecher kann Dienste entschuldigen")
    await _revoke_strikes(session, a, now)
    a.status = Status.excused
    a.completed_at = now
    a.completed_by_id = actor.id
    await session.flush()


async def dispute_duty(session: AsyncSession, a: Assignment, actor: User, now: datetime) -> None:
    """Admin only: it was marked done but not actually done. The claimer gets a strike.

    The claimer loses the credit; the partner stays "covered" and gets nothing.
    """
    if not actor.is_admin:
        raise DutyError("Nur der Flursprecher kann Dienste beanstanden")
    if a.status is not Status.done:
        raise DutyError("Nur erledigte Dienste können beanstandet werden")
    a.status = Status.missed
    a.disputed_at = now
    await session.flush()
    if a.user_id is not None:
        await strikes.issue_strike(
            session,
            user_id=a.user_id,
            reason=f"{describe_slot(a.slot)} als erledigt eingetragen, aber nicht gemacht",
            assignment_id=a.id,
            issued_by_id=actor.id,
            today=now.date(),
        )


@dataclass
class PreviewEntry:
    """One row of the dashboard preview."""

    start: date
    end: date
    # User ids; `Role`s stand for the people on a slot that isn't done yet.
    user_ids: list[int | Role]
    is_penalty: bool = False


async def build_preview(
    session: AsyncSession, duty: Duty, start: date, end: date
) -> list[PreviewEntry]:
    """Expected schedule for the slots starting between `start` and `end`.

    Slots that already exist (e.g. penalty weeks) are shown as they are; the others are
    simulated with `scheduling.plan_upcoming_slots`. The people on earlier slots that nobody
    has marked done yet (e.g. today's kitchen pair) show up as "did it" / "didn't do it"
    `Role`s.
    """
    n = get_people_per_slot(duty)
    step = timedelta(days=1 if duty is Duty.kitchen else 7)
    # Trash weeks start on Monday: skip the week that is already running on `start`.
    first, _ = get_slot_bounds(duty, start)
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
    to_simulate: list[PreviewEntry] = []
    day = first
    while day <= end:
        slot_start, slot_end = get_slot_bounds(duty, day)
        if day in existing:
            s = existing[day]
            entry = PreviewEntry(
                start=slot_start,
                end=slot_end,
                user_ids=[a.user_id for a in s.assignments if a.user_id is not None],
                is_penalty=s.is_penalty,
            )
        else:
            entry = PreviewEntry(start=slot_start, end=slot_end, user_ids=[])
            to_simulate.append(entry)
        entries.append(entry)
        day += step

    earlier = await session.scalars(
        select(Slot)
        .where(
            Slot.duty == duty,
            Slot.is_penalty.is_(False),
            Slot.start_date < first,
            Slot.assignments.any(Assignment.status == Status.pending),
        )
        .options(selectinload(Slot.assignments))
    )
    open_slots = [
        (s.start_date, [a.user_id for a in s.assignments if a.status is Status.pending])
        for s in earlier
        if s.claimed_assignment is None
    ]

    planned = scheduling.plan_upcoming_slots(
        await load_balances(session, duty),
        await load_candidates(session),
        [(e.start, e.end) for e in to_simulate],
        n,
        open_slots,
    )
    for entry, user_ids in zip(to_simulate, planned, strict=True):
        entry.user_ids = user_ids
    return entries
