# A4 Manager

Web app for the duties on dorm floor A4: Küchendienst, Mülldienst and strikes.

## How it works

**Duties**
- Küchendienst: every day, 2 people are put on it, one of them cleans.
- Mülldienst: every Monday to Sunday week, 1 person.
- Whoever did it marks it done ("Erledigt"). The other person on the slot is then "nicht nötig".
- Deadline: 12:00 the day after the duty ended. After that it counts as missed.

**Who is picked**

Every slot is one job. Everybody who is available for it (active and not away) owes an equal
share of it. A resident's debt is their shares minus the jobs they actually did. The people
with the highest debt are picked next.
- Being away is fair: you owe nothing for that time.
- New residents start at debt 0.
- A missed duty gives no credit, so you come up again soon.
- Ties go to whoever had their last duty longest ago.

**Strikes**
- A missed duty gives every assigned person a strike.
- The Flursprecher can give strikes by hand and take them back.
- 3 active strikes give a penalty Mülldienst week (the next free one). It replaces the regular
  Mülldienst of that week and doesn't count for fairness. The 3 strikes are then paid off.

**Flursprecher (admin)**
- Adds, pauses and deletes residents.
- Can mark any duty done or excuse it (no strike, no credit).
- "Beanstanden" in the history: marked done but not done. The claimer loses the credit and gets a strike.

**Residents**
- Away, paused or deleted residents are replaced on their open duties automatically.
- Moving out: delete the resident. Their history stays (as "ehemalig") and nobody inherits their share.
- Moving in: add a new resident, don't rename the old account. They start from zero.

**Daily job**: runs at 00:05 and 12:05. It marks missed duties and picks today's and this
week's people. The Flursprecher page can run it by hand.

All numbers (people per slot, deadline, strike limit) are settings in `.env`, see `.env.example`.

## Setup

```bash
cp .env.example .env                    # set SECRET_KEY: openssl rand -hex 32
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run python scripts/create_admin.py   # first Flursprecher
uv run fastapi dev app/main.py          # http://localhost:8000
```

Everything in Docker: `docker compose --profile app up --build`.
Test residents: `uv run python scripts/seed.py`.

## Tests

```bash
uv run pytest   # needs the database from `docker compose up -d db`
```

## Code

- `app/services/scheduling.py`: the fairness logic, without database (start reading here)
- `app/services/`: the other rules (duties, strikes, users, login, daily job)
- `app/web.py`: pages and form actions; templates in `app/templates/`
- `app/models.py`: database tables; migrations in `migrations/`
