"""Residents: creating, pausing, moving out, and away periods."""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Assignment, AwayPeriod, Slot, Status, User
from app.services import duties, scheduling
from app.services.auth import (
    MIN_PASSWORD_LENGTH,
    PASSWORD_TOO_SHORT,
    hash_password,
    normalize_username,
)


class UserError(Exception):
    """Invalid input; the message is German and shown to the user as is."""


# Building letter, floor digit, room 01-99 on that floor: A403.
ROOM_PATTERN = re.compile(r"[A-Z][0-9](0[1-9]|[1-9][0-9])")


def normalize_room(room_number: str) -> str:
    room_number = room_number.strip().upper()
    if not ROOM_PATTERN.fullmatch(room_number):
        raise UserError(
            f"Zimmer „{room_number}“ ungültig: Buchstabe, Stockwerk, Zimmer 01–99, z.B. A403"
        )
    return room_number


async def list_users(session: AsyncSession) -> list[User]:
    users = await session.scalars(select(User))
    return sorted(users, key=lambda u: scheduling.get_room_sort_key(u.room_number))


async def create_user(
    session: AsyncSession,
    *,
    name: str,
    room_number: str,
    username: str,
    password: str,
    is_admin: bool = False,
) -> User:
    """Add a resident. Their duty balance starts at zero."""
    name = name.strip()
    username = normalize_username(username)
    if not (name and room_number.strip() and username and password):
        raise UserError("Alle Felder müssen ausgefüllt sein")
    room_number = normalize_room(room_number)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(PASSWORD_TOO_SHORT)
    if await session.scalar(select(User).where(User.username == username)):
        raise UserError(f"Benutzername „{username}“ ist schon vergeben")
    user = User(
        name=name,
        room_number=room_number,
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
        active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def create_initial_admin(session: AsyncSession) -> User | None:
    """Create the Flursprecher from the `ADMIN_*` settings, if set and there are no users yet.

    Only an empty database counts, so an admin deleted later doesn't come back on restart.
    """
    if not settings.admin_username:
        return None
    if await session.scalar(select(User.id).limit(1)) is not None:
        return None
    return await create_user(
        session,
        name=settings.admin_name,
        room_number=settings.admin_room,
        username=settings.admin_username,
        password=settings.admin_password,
        is_admin=True,
    )


async def set_user_active(session: AsyncSession, user: User, active: bool, today: date) -> None:
    """Pause or resume a resident. A paused resident's open duties go to someone else."""
    user.active = active
    await session.flush()
    if active:
        await duties.fill_current_slots(session, today)
    else:
        await duties.replace_unavailable_people(session, today)


async def delete_user(session: AsyncSession, user: User, today: date) -> None:
    """Moving out. The history stays, anonymised; whoever moves in next is a new user and
    starts from zero (no debt, no strikes). Their open duties go to someone else.
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
    # The database's ON DELETE rules changed rows the session still holds; reload them.
    session.expire_all()
    await duties.replace_unavailable_people(session, today)
    await duties.fill_current_slots(session, today)


async def add_away_period(
    session: AsyncSession, user: User, start: date, end: date, today: date
) -> None:
    """Mark a resident away from `start` to `end`; open duties in that time are reassigned."""
    if end < start:
        raise UserError("Das Ende muss nach dem Anfang liegen")
    if end < today:
        raise UserError("Abwesenheiten in der Vergangenheit ändern nichts mehr")
    session.add(AwayPeriod(user_id=user.id, start_date=start, end_date=end))
    await session.flush()
    # `user.away_periods` may already be loaded without the new period; reload it.
    session.expire_all()
    await duties.replace_unavailable_people(session, today)


async def remove_away_period(session: AsyncSession, away: AwayPeriod) -> None:
    """Delete an away period. Duties already reassigned stay with their new people."""
    await session.delete(away)
    await session.flush()
