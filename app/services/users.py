from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AwayPeriod, User
from app.services import duties, scheduling
from app.services.auth import hash_password


class UserError(Exception):
    pass


async def list_users(session: AsyncSession) -> list[User]:
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
    username = username.strip().lower()
    if not (name.strip() and room_number.strip() and username and password):
        raise UserError("Alle Felder müssen ausgefüllt sein")
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
    user.active = active
    await session.flush()
    if active:
        await duties.fill_current(session, today)
    else:
        await duties.reconcile(session, today)


async def delete(session: AsyncSession, user: User, today: date) -> None:
    await session.delete(user)
    await session.flush()
    session.expire_all()
    await duties.reconcile(session, today)


async def add_away(session: AsyncSession, user: User, start: date, end: date, today: date) -> None:
    if end < start:
        raise UserError("Das Ende muss nach dem Anfang liegen")
    if end < today:
        raise UserError("Abwesenheiten in der Vergangenheit ändern nichts mehr")
    session.add(AwayPeriod(user_id=user.id, start_date=start, end_date=end))
    await session.flush()
    session.expire_all()
    await duties.reconcile(session, today)


async def remove_away(session: AsyncSession, away: AwayPeriod) -> None:
    await session.delete(away)
    await session.flush()
