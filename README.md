# A4 Manager

Duty manager for dorm floor A4: Küchendienst (2 people on it per day, one of them cleans), Mülldienst (1 person per week), strikes.

## How duties are assigned

Each duty period ("slot": a day for kitchen, a Monday–Sunday week for trash) is one job.
`n` people are put on it (kitchen 2, trash 1), and whoever marks it done first does it.
Every resident who is **available** for the slot (active and not away) accrues an equal share
`1 / available` of *expected* work. A resident's **debt** is `expected − duties actually done`.
The `n` residents with the highest debt are picked (tie-break: longest since their last duty, then room).

- Away time is fair automatically: you accrue nothing while away, so you don't owe extra afterwards.
- New residents start at debt 0, with no backlog.
- A missed duty gives no credit, so you come up again soon. It also gives every assignee a strike.
- The partner who didn't clean ("nicht nötig") gets no credit and no strike, so they come up again sooner.
- The number of residents is not fixed. Any number works.

The logic is in `app/services/scheduling.py` (pure functions, see `tests/test_scheduling.py`).

## Rules

- Duties must be marked done by **12:00 the day after** the slot ends (`DEADLINE_HOUR`, `DEADLINE_DAYS_AFTER`).
  After that, the assignment counts as *missed* and each assignee gets an automatic strike.
- Each resident marks their own duty. Once one of the two has marked it, the other can't anymore.
  The Flursprecher can mark anyone's.
- **Verlauf** (history) shows who claimed which duty and when. If someone claimed it but didn't
  actually clean, the Flursprecher clicks **Beanstanden**: the entry becomes missed, the claimer
  loses the credit and gets a strike.
- The Flursprecher (admin) can mark any duty done or excuse it. Both remove the automatic strike.
- **3 active strikes** (`STRIKE_THRESHOLD`) add a penalty Mülldienst in the next free week. That week has no regular
  Mülldienst, and the penalty doesn't count toward fairness. The 3 strikes are then marked "abgearbeitet".
- If someone marks themselves away during a duty that is still open, they are replaced automatically.
  The same happens when they are deactivated or deleted.
- Deactivate residents to pause them. Delete them when they move out. Their past duties stay in the history
  (as "ehemalig") and still count for the past, so nobody else inherits their share. Open penalty weeks are dropped.
- Someone new moves into a room: add them as a new resident (don't rename the old account). They start at
  debt 0 with no strikes, independent of what the previous resident did.

A daily job runs at 00:05 and 12:05 (Europe/Berlin). It flags missed duties, then picks today's kitchen people
and this week's trash person. The Flursprecher page can also trigger it.

## Setup

```bash
cp .env.example .env               # set SECRET_KEY (openssl rand -hex 32), the app won't start without it
docker compose up -d db
uv sync
uv run alembic upgrade head
uv run python scripts/create_admin.py   # first Flursprecher
uv run fastapi dev app/main.py          # http://localhost:8000
```

The Flursprecher adds all other residents on `/admin`.

Run everything in Docker: `docker compose --profile app up --build`.

## Tests

```bash
uv run pytest   # needs `docker compose up -d db`; creates the a4_test database if missing
```

## Layout

- `app/services/`: business logic (scheduling, duties, strikes, users, daily job)
- `app/web.py`: routes (htmx + Jinja templates in `app/templates/`)
- `app/models.py`: SQLAlchemy models; migrations in `migrations/`

The routes are thin wrappers around the services, so a JSON API (e.g. for a bot) can be added later.
