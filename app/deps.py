"""Route dependencies: the database session and the logged-in user.

A route parameter `user: AdminUser` loads the logged-in user and checks that they are the
Flursprecher before the route body runs.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User

Session = Annotated[AsyncSession, Depends(get_session)]


class LoginRequired(Exception):
    """Nobody is logged in; `main.py` turns this into a redirect to the login page."""


async def get_optional_user(request: Request, session: Session) -> User | None:
    """A login session of a since deleted user is cleared."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    if user is None:
        request.session.clear()
    return user


async def require_user(user: Annotated[User | None, Depends(get_optional_user)]) -> User:
    if user is None:
        raise LoginRequired
    return user


async def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Nur für den Flursprecher")
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]
CurrentUser = Annotated[User, Depends(require_user)]
AdminUser = Annotated[User, Depends(require_admin)]
