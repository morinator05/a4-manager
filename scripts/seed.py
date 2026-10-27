"""Seed the database with test residents.

    uv run python scripts/seed.py                  # 15 residents
    uv run python scripts/seed.py -n 20 --password test1234

Usernames are the lowercase first names; existing usernames are skipped, so it is
safe to run more than once. Afterwards today's duties are assigned.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.services import jobs, users  # noqa: E402

# fmt: off
FIRST_NAMES = [
    "Anna", "Ben", "Clara", "David", "Emma", "Felix", "Greta", "Hannes", "Ida", "Jonas",
    "Kim", "Lukas", "Mia", "Noah", "Olivia", "Paul", "Quinn", "Rosa", "Simon", "Tara",
    "Uwe", "Vera", "Wim", "Xenia", "Yusuf", "Zoe",
]
LAST_NAMES = [
    "Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker",
    "Schulz", "Hoffmann", "Koch", "Richter", "Klein", "Wolf", "Neumann", "Schwarz",
]
# fmt: on


async def main(count: int, password: str) -> None:
    if count > len(FIRST_NAMES):
        sys.exit(f"At most {len(FIRST_NAMES)} residents supported")
    async with SessionLocal() as session:
        existing = set(await session.scalars(select(User.username)))
        added = 0
        for i in range(count):
            first = FIRST_NAMES[i]
            username = first.lower()
            if username in existing:
                continue
            await users.create(
                session,
                name=f"{first} {LAST_NAMES[i % len(LAST_NAMES)]}",
                room_number=f"A4{i + 1:02d}",
                username=username,
                password=password,
            )
            added += 1
        await session.commit()
        await jobs.daily_tick(session, settings.now())
    print(f"{added} residents added ({count - added} already existed), password: {password}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--count", type=int, default=15)
    parser.add_argument("--password", default="passwort123")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.password))
