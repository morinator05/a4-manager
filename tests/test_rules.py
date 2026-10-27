import random
from datetime import date, datetime, time, timedelta
from fractions import Fraction

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Assignment, Duty, Slot, Status
from app.services import duties, jobs, strikes, users
from app.services.duties import DutyError

MONDAY = date(2026, 11, 2)


def at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour), settings.tz)


async def make_users(session, n=15, admin=True):
    created = []
    for i in range(1, n + 1):
        created.append(
            await users.create(
                session,
                name=f"Person {i}",
                room_number=f"{i}",
                username=f"p{i}",
                password="secret123",
                is_admin=admin and i == 1,
            )
        )
    await session.commit()
    return created


async def test_tick_assigns_kitchen_and_trash(session):
    await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    trash = await duties.get_slot(session, Duty.trash, MONDAY + timedelta(days=3))
    assert len(kitchen.assignments) == 2
    assert len(trash.assignments) == 1
    assert trash.start_date == MONDAY and trash.end_date == MONDAY + timedelta(days=6)

    # Idempotent.
    await jobs.daily_tick(session, at(MONDAY, 13))
    assert len((await session.scalars(select(Slot))).all()) == 2


async def test_missed_kitchen_strikes_both(session):
    await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    picked = [a.user_id for a in kitchen.assignments]

    # Before the deadline (next day 12:00) nothing happens.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 11))
    assert all(a.status is Status.pending for a in kitchen.assignments)

    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 12))
    await session.refresh(kitchen, ["assignments"])
    assert all(a.status is Status.missed for a in kitchen.assignments)
    for uid in picked:
        assert len(await strikes.active_strikes(session, uid)) == 1


async def test_three_strikes_give_penalty_in_next_free_week(session):
    people = await make_users(session)
    admin, a, b = people[0], people[1], people[2]
    await jobs.daily_tick(session, at(MONDAY))
    for _ in range(3):
        await strikes.issue(session, user_id=a.id, reason="x", issued_by_id=admin.id, today=MONDAY)
    for _ in range(3):
        await strikes.issue(session, user_id=b.id, reason="x", issued_by_id=admin.id, today=MONDAY)
    await session.commit()

    penalties = (
        await session.scalars(
            select(Slot).where(Slot.is_penalty.is_(True)).order_by(Slot.start_date)
        )
    ).all()
    assert [p.start_date for p in penalties] == [
        MONDAY + timedelta(days=7),
        MONDAY + timedelta(days=14),
    ]
    assert len(await strikes.active_strikes(session, a.id)) == 0

    # The penalty week replaces the regular Mülldienst.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=7)))
    slot = await duties.get_slot(session, Duty.trash, MONDAY + timedelta(days=7))
    assert slot.is_penalty and [x.user_id for x in slot.assignments] == [a.id]


async def test_penalty_does_not_count_as_regular_trash(session):
    people = await make_users(session, n=3)
    a = people[1]
    for _ in range(3):
        await strikes.issue(session, user_id=a.id, reason="x", today=MONDAY)
    balances = await duties.load_balances(session, Duty.trash)
    assert balances[a.id].credit == 0


async def kitchen_pair(session, people, day=MONDAY):
    await jobs.daily_tick(session, at(day))
    kitchen = await duties.get_slot(session, Duty.kitchen, day)
    by_id = {p.id: p for p in people}
    first, second = kitchen.assignments
    return kitchen, first, second, by_id[first.user_id], by_id[second.user_id]


async def test_mark_done_permissions(session):
    people = await make_users(session)
    admin = people[0]
    # Keep the admin off today's duty, so the outsider/admin checks are meaningful.
    await users.add_away(session, admin, MONDAY, MONDAY + timedelta(days=1), MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    outsider = next(p for p in people if p.id not in {p1.id, p2.id} and not p.is_admin)

    with pytest.raises(DutyError):
        await duties.mark_done(session, first, outsider, at(MONDAY, 20))
    # Nobody can mark someone else's duty.
    with pytest.raises(DutyError):
        await duties.mark_done(session, first, p2, at(MONDAY, 20))

    # Too late for a resident, but the Flursprecher can still do it.
    tuesday = MONDAY + timedelta(days=1)
    _, t_first, _, t1, _ = await kitchen_pair(session, people, tuesday)
    with pytest.raises(DutyError):
        await duties.mark_done(session, t_first, t1, at(tuesday + timedelta(days=1), 12))
    await duties.mark_done(session, t_first, admin, at(tuesday + timedelta(days=1), 12))
    assert t_first.status is Status.done


async def test_first_claim_closes_the_slot(session):
    people = await make_users(session)
    kitchen, first, second, p1, p2 = await kitchen_pair(session, people)

    await duties.mark_done(session, second, p2, at(MONDAY, 20))
    assert second.status is Status.done and second.completed_by_id == p2.id
    assert first.status is Status.covered
    with pytest.raises(DutyError):
        await duties.mark_done(session, first, p1, at(MONDAY, 21))
    await session.commit()

    # Only the one who cleaned gets credit; the partner gets neither credit nor strike.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p2.id].credit == 1 and balances[p1.id].credit == 0
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2)))
    assert await strikes.active_strikes(session, p1.id) == []


async def test_open_slot_shares_credit_until_claimed(session):
    people = await make_users(session)
    _, _, _, p1, p2 = await kitchen_pair(session, people)
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].credit == balances[p2.id].credit == Fraction(1, 2)


async def test_dispute_gives_strike_and_removes_credit(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away(session, admin, MONDAY, MONDAY, MONDAY)
    _, first, second, p1, p2 = await kitchen_pair(session, people)
    await duties.mark_done(session, first, p1, at(MONDAY, 20))

    with pytest.raises(DutyError):
        await duties.dispute(session, first, p2, at(MONDAY, 21))
    await duties.dispute(session, first, admin, at(MONDAY, 21))
    await session.commit()

    assert first.status is Status.missed and first.disputed_at is not None
    assert second.status is Status.covered
    [strike] = await strikes.active_strikes(session, p1.id)
    assert strike.issued_by_id == admin.id and strike.assignment_id == first.id
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].credit == 0
    # The deadline passing later doesn't add a second strike.
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=2)))
    assert len(await strikes.active_strikes(session, p1.id)) == 1


async def test_admin_marking_missed_done_revokes_strike(session):
    people = await make_users(session)
    admin = people[0]
    await jobs.daily_tick(session, at(MONDAY))
    await jobs.daily_tick(session, at(MONDAY + timedelta(days=1), 13))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    a = await duties.get_assignment(session, kitchen.assignments[0].id)
    assert a.status is Status.missed
    await duties.mark_done(session, a, admin, at(MONDAY + timedelta(days=2)))
    assert len(await strikes.active_strikes(session, a.user_id)) == 0


async def test_away_replaces_pending_assignment(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    leaving_id = kitchen.assignments[0].user_id
    leaving = next(p for p in people if p.id == leaving_id)

    await users.add_away(session, leaving, MONDAY, MONDAY + timedelta(days=3), MONDAY)
    await session.commit()
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    ids = [a.user_id for a in kitchen.assignments]
    assert len(ids) == 2 and leaving_id not in ids

    preview = await duties.preview(session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=3))
    assert all(leaving_id not in e.user_ids for e in preview)


async def test_delete_user_keeps_history_and_replaces(session):
    people = await make_users(session)
    await jobs.daily_tick(session, at(MONDAY))
    trash = await duties.get_slot(session, Duty.trash, MONDAY)
    gone_id = trash.assignments[0].user_id
    gone = next(p for p in people if p.id == gone_id)
    await users.delete(session, gone, MONDAY)
    await session.commit()
    trash = await duties.get_slot(session, Duty.trash, MONDAY)
    assert [a.user_id for a in trash.assignments] != [gone_id]
    assert len(trash.assignments) == 1


async def test_month_of_ticks_is_fair(session):
    """30 days, one random person of each pair cleans: everybody cleans 2 times."""
    rng = random.Random(7)
    await make_users(session)
    day = MONDAY
    for _ in range(30):
        await jobs.daily_tick(session, at(day))
        slot = await duties.get_slot(session, Duty.kitchen, day)
        claim = await duties.get_assignment(session, rng.choice(slot.assignments).id)
        await duties.mark_done(session, claim, claim.user, at(day, 20))
        await session.commit()
        day += timedelta(days=1)
    rows = (
        await session.execute(
            select(Assignment.user_id)
            .join(Assignment.slot)
            .where(Slot.duty == Duty.kitchen, Assignment.status == Status.done)
        )
    ).scalars().all()
    counts = [rows.count(u) for u in set(rows)]
    assert len(set(rows)) == 15 and max(counts) - min(counts) <= 1


@pytest.fixture
async def client(session_factory):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def login(client, username):
    r = await client.post("/login", data={"username": username, "password": "secret123"})
    assert r.status_code == 303


async def test_web_permissions(session, client):
    await make_users(session, n=3)
    r = await client.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/")
    assert r.status_code == 200 and "Küchendienst" in r.text

    await login(client, "p2")
    assert (await client.get("/admin")).status_code == 403
    assert (await client.get("/me")).status_code == 200

    await client.post("/logout")
    await login(client, "p1")
    assert (await client.get("/admin")).status_code == 200
    r = await client.post(
        "/admin/users",
        data={"name": "Neu", "room_number": "9", "username": "neu", "password": "pw123456"},
    )
    assert r.status_code == 303
    assert "Neu" in (await client.get("/admin")).text


async def test_understaffed_slot_is_topped_up(session):
    # The tick ran before anyone lived on the floor.
    await jobs.daily_tick(session, at(MONDAY))
    assert (await duties.get_slot(session, Duty.kitchen, MONDAY)).assignments == []

    await make_users(session, n=5)
    await jobs.daily_tick(session, at(MONDAY, 10))
    assert len((await duties.get_slot(session, Duty.kitchen, MONDAY)).assignments) == 2
    assert len((await duties.get_slot(session, Duty.trash, MONDAY)).assignments) == 1


async def test_history_page_and_dispute(session, client):
    people = await make_users(session, n=4)
    _, first, _, p1, _ = await kitchen_pair(session, people, settings.today())
    await duties.mark_done(session, first, p1, settings.now())
    await session.commit()

    assert (await client.get("/history")).status_code == 303  # login required
    await login(client, "p2")
    r = await client.get("/history")
    assert r.status_code == 200 and p1.display_name in r.text and "Beanstanden" not in r.text
    assert (await client.post(f"/admin/assignments/{first.id}/dispute")).status_code == 403

    await client.post("/logout")
    await login(client, "p1")
    r = await client.get("/history")
    assert "Beanstanden" in r.text
    r = await client.post(f"/admin/assignments/{first.id}/dispute?next=/history")
    assert r.status_code == 303
    assert "beanstandet" in (await client.get("/history")).text
