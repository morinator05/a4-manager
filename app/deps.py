from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User

Session = Annotated[AsyncSession, Depends(get_session)]


class LoginRequired(Exception):
    pass


async def optional_user(request: Request, session: Session) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    if user is None:
        request.session.clear()
    return user


async def require_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise LoginRequired
    return user


async def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Nur für den Flursprecher")
    return user


OptionalUser = Annotated[User | None, Depends(optional_user)]
CurrentUser = Annotated[User, Depends(require_user)]
AdminUser = Annotated[User, Depends(require_admin)]
