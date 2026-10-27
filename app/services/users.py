"""Residents: creating, pausing, moving out, and away periods."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Assignment, AwayPeriod, Slot, Status, User
from app.services import duties, scheduling
from app.services.auth import MIN_PASSWORD_LENGTH, PASSWORD_TOO_SHORT, hash_password


class UserError(Exception):
    """Invalid input; the message is German and shown to the user as is."""


async def list_users(session: AsyncSession) -> list[User]:
    """All users, sorted by room."""
    users = await session.scalars(select(User))
    return sorted(users, key=lambda u: scheduling.room_key(u.room_number))


async def create(
    session: AsyncSession,
    *,
    name: str,
    room_number: str,
    username: str,
    password: str,
    is_admin: bool = False,
) -> User:
    """Add a resident. Their duty balance starts at zero.

    Raises:
        UserError: if a field is empty, the password is too short or the username is taken.
    """
    username = username.strip().lower()
    if not (name.strip() and room_number.strip() and username and password):
        raise UserError("Alle Felder müssen ausgefüllt sein")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(PASSWORD_TOO_SHORT)
    if await session.scalar(select(User).where(User.username == username)):
        raise UserError(f"Benutzername „{username}“ ist schon vergeben")
    user = User(
        name=name.strip(),
        room_number=room_number.strip(),
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
        active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def set_active(session: AsyncSession, user: User, active: bool, today: date) -> None:
    """Pause or resume a resident. A paused resident's open duties go to someone else."""
    user.active = active
    await session.flush()
    if active:
        await duties.fill_current(session, today)
    else:
        await duties.reconcile(session, today)


async def delete(session: AsyncSession, user: User, today: date) -> None:
    """Moving out. The person's history stays anonymised; whoever moves in next is a new
    user and starts from zero (no debt, no strikes).

    Their open duties go to someone else.
    """
    # Open penalty weeks go with the person, so those weeks get a regular Mülldienst again.
    open_penalties = await session.scalars(
        select(Slot)
        .join(Slot.assignments)
        .where(
            Slot.is_penalty.is_(True),
            Slot.end_date >= today,
            Assignment.user_id == user.id,
            Assignment.status == Status.pending,
        )
    )
    for slot in open_penalties.all():
        await session.delete(slot)
    await session.delete(user)
    await session.flush()
    session.expire_all()
    await duties.reconcile(session, today)
    await duties.fill_current(session, today)


async def add_away(session: AsyncSession, user: User, start: date, end: date, today: date) -> None:
    """Mark a resident away from `start` to `end`; open duties in that time are reassigned.

    Raises:
        UserError: if the period is reversed or already over.
    """
    if end < start:
        raise UserError("Das Ende muss nach dem Anfang liegen")
    if end < today:
        raise UserError("Abwesenheiten in der Vergangenheit ändern nichts mehr")
    session.add(AwayPeriod(user_id=user.id, start_date=start, end_date=end))
    await session.flush()
    session.expire_all()
    await duties.reconcile(session, today)


async def remove_away(session: AsyncSession, away: AwayPeriod) -> None:
    """Delete an away period. Duties already reassigned stay with their new people."""
    await session.delete(away)
    await session.flush()
