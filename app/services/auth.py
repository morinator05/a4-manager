"""Passwords (Argon2id hashes) and checking a login."""

from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

MIN_PASSWORD_LENGTH = 8

_hasher = PasswordHash.recommended()
# Checked when the username doesn't exist, so a wrong username takes as long as a wrong
# password and response times don't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("no-such-user")


def normalize_username(username: str) -> str:
    """Usernames are case-insensitive and stored in lower case."""
    return username.strip().lower()


def hash_password(password: str) -> str:
    """Hash a password for storing (Argon2id, with a random salt)."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against its stored hash."""
    return _hasher.verify(password, password_hash)


async def authenticate_user(session: AsyncSession, username: str, password: str) -> User | None:
    """The user with these credentials, or None."""
    user = await session.scalar(select(User).where(User.username == normalize_username(username)))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
