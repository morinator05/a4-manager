"""Residents: creating, pausing, moving out, and away periods."""

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import lock_schedule
from app.models import AwayPeriod, User
from app.services import auth, duties, scheduling, strikes


class UserError(Exception):
    """Invalid input; the message is German and shown to the user as is."""


# Building letter, floor digit, room 01-99 on that floor: A403. Also the forms' `pattern`,
# which is why lower case is in it.
ROOM_PATTERN = re.compile(r"[A-Za-z][0-9](0[1-9]|[1-9][0-9])")
ROOM_HINT = "Buchstabe, Stockwerk, Zimmer 01–99, z.B. A403"


def parse_room_number(room_number: str) -> str:
    """The room number trimmed and in upper case; `UserError` unless it looks like A403."""
    room_number = room_number.strip().upper()
    if not ROOM_PATTERN.fullmatch(room_number):
        raise UserError(f"Zimmer „{room_number}“ ungültig: {ROOM_HINT}")
    return room_number


def validate_new_password(password: str) -> None:
    """`UserError` if the password is too short."""
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        raise UserError(f"Das Passwort muss mindestens {auth.MIN_PASSWORD_LENGTH} Zeichen haben")


async def list_users(session: AsyncSession) -> list[User]:
    """All users, sorted by room number."""
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
    username = auth.normalize_username(username)
    if not (name and room_number.strip() and username and password):
        raise UserError("Alle Felder müssen ausgefüllt sein")
    room_number = parse_room_number(room_number)
    validate_new_password(password)
    if await session.scalar(select(User).where(User.username == username)):
        raise UserError(f"Benutzername „{username}“ ist schon vergeben")
    user = User(
        name=name,
        room_number=room_number,
        username=username,
        password_hash=auth.hash_password(password),
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


async def update_user(
    session: AsyncSession,
    target: User,
    actor: User,
    *,
    name: str,
    room_number: str,
    password: str,
    is_admin: bool,
) -> None:
    """The Flursprecher changes a resident; empty fields stay as they are.

    Nobody can change their own Flursprecher role, so there is always one left.
    """
    if password:
        validate_new_password(password)
    if room_number.strip():
        target.room_number = parse_room_number(room_number)
    target.name = name.strip() or target.name
    if target.id != actor.id:
        target.is_admin = is_admin
    if password:
        target.password_hash = auth.hash_password(password)
    await session.flush()


async def change_password(
    session: AsyncSession, user: User, old_password: str, new_password: str
) -> None:
    """A resident changes their own password; the old one must be right."""
    if not auth.verify_password(old_password, user.password_hash):
        raise UserError("Altes Passwort falsch")
    validate_new_password(new_password)
    user.password_hash = auth.hash_password(new_password)
    await session.flush()


async def set_user_active(session: AsyncSession, user: User, active: bool, today: date) -> None:
    """Pause or resume a resident.

    A paused resident's open duties go to someone else, and an open penalty week is
    cancelled until they are resumed; then it is planned again.
    """
    await lock_schedule(session)
    user.active = active
    await session.flush()
    if active:
        await duties.fill_current_slots(session, today)
        await strikes.add_penalty_week_if_due(session, user.id, today)
    else:
        for slot in await strikes.get_open_penalty_weeks(session, user.id, today):
            await strikes.cancel_penalty_week(session, slot)
        await duties.replace_unavailable_people(session, today)


async def delete_user(session: AsyncSession, user: User, actor: User, today: date) -> None:
    """Moving out: the history stays, anonymised, and their open duties go to someone else.

    Whoever moves in next is a new user and starts from zero (no debt, no strikes). Nobody
    can delete themselves, so there is always a Flursprecher left.
    """
    if user.id == actor.id:
        raise UserError("Du kannst dich nicht selbst löschen")
    await lock_schedule(session)
    for slot in await strikes.get_open_penalty_weeks(session, user.id, today):
        await strikes.cancel_penalty_week(session, slot)
    await session.delete(user)
    await session.flush()
    # The database's ON DELETE rules changed rows the session still holds; reload them.
    session.expire_all()
    await duties.replace_unavailable_people(session, today)
    await duties.fill_current_slots(session, today)


async def add_away_period(
    session: AsyncSession, user: User, start: date, end: date, today: date
) -> None:
    """Mark a resident away from `start` to `end`; they aren't picked for that time.

    Duties that already started stay theirs (the Flursprecher can excuse them), so nobody
    can drop a duty at the last minute. A penalty week in that time moves to a later week.
    """
    if end < start:
        raise UserError("Das Ende muss nach dem Anfang liegen")
    if end < today:
        raise UserError("Abwesenheiten in der Vergangenheit ändern nichts mehr")
    await lock_schedule(session)
    session.add(AwayPeriod(user_id=user.id, start_date=start, end_date=end))
    await session.flush()
    clashing = [
        slot
        for slot in await strikes.get_open_penalty_weeks(session, user.id, today)
        if slot.start_date > today
        and scheduling.is_away_during([(start, end)], slot.start_date, slot.end_date)
    ]
    for slot in clashing:
        await strikes.cancel_penalty_week(session, slot)
    if clashing:
        await strikes.add_penalty_week_if_due(session, user.id, today)


async def delete_away_period(session: AsyncSession, user: User, away_id: int) -> None:
    """Delete one of the user's own away periods. Duties already reassigned stay as they are."""
    away = await session.get(AwayPeriod, away_id)
    if away is None or away.user_id != user.id:
        raise UserError("Abwesenheit nicht gefunden")
    await session.delete(away)
    await session.flush()
