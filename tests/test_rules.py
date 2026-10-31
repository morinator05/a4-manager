"""Rules against a real database: assigning, deadlines, strikes, moving out, web pages."""

import asyncio
import random
from collections import Counter
from datetime import date, datetime, time, timedelta
from fractions import Fraction

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.main import app
from app.models import Assignment, AwayPeriod, Duty, Slot, Status, Strike, User
from app.services import auth, duties, jobs, strikes, users
from app.services.duties import DutyError
from app.services.login_throttle import LoginThrottle
from app.services.scheduling import Role
from app.services.users import UserError
from app.web import render_person

MONDAY = date(2026, 11, 2)
PASSWORD = "secret123"


def make_datetime(day: date, hour: int = 9) -> datetime:
    """`day` at `hour` in the app's time zone."""
    return datetime.combine(day, time(hour), settings.tz)


async def make_users(session, n=15, admin=True):
    """`n` residents p1..pn with `PASSWORD`; p1 is the Flursprecher if `admin`."""
    created = []
    for i in range(1, n + 1):
        created.append(
            await users.create_user(
                session,
                name=f"Person {i}",
                room_number=f"A4{i:02d}",
                username=f"p{i}",
                password=PASSWORD,
                is_admin=admin and i == 1,
            )
        )
    await session.commit()
    return created


async def assign_kitchen_pair(session, day=MONDAY):
    """Run the daily job for `day`; returns the two kitchen assignments and their people."""
    await jobs.run_daily_job(session, make_datetime(day))
    kitchen = await duties.get_slot(session, Duty.kitchen, day)
    # Loaded like the app does, with `.slot`, so `mark_duty_done` never has to load it lazily:
    # that fails in async code and made this helper's tests fail now and then.
    first, second = [await duties.get_assignment(session, a.id) for a in kitchen.assignments]
    return first, second, first.user, second.user


@pytest.fixture
async def client(session_factory):
    """HTTP client that calls the app directly (no server), keeping the login cookie."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def log_in(client, username):
    """Log in as `username` with the test password."""
    r = await client.post("/login", data={"username": username, "password": PASSWORD})
    assert r.status_code == 303


async def test_daily_job_assigns_kitchen_and_trash(session):
    await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    trash = await duties.get_slot(session, Duty.trash, MONDAY + timedelta(days=3))
    assert len(kitchen.assignments) == 2
    assert len(trash.assignments) == 1
    assert trash.start_date == MONDAY and trash.end_date == MONDAY + timedelta(days=6)

    # Idempotent.
    await jobs.run_daily_job(session, make_datetime(MONDAY, 13))
    assert len((await session.scalars(select(Slot))).all()) == 2


async def test_missed_kitchen_strikes_both(session):
    await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    picked = [a.user_id for a in kitchen.assignments]

    # Before the deadline (next day 12:00) nothing happens.
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=1), 11))
    assert all(a.status is Status.pending for a in kitchen.assignments)

    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=1), 12))
    await session.refresh(kitchen, ["assignments"])
    assert all(a.status is Status.missed for a in kitchen.assignments)
    for uid in picked:
        assert len(await strikes.get_active_strikes(session, uid)) == 1


async def test_three_strikes_give_penalty_in_next_free_week(session):
    people = await make_users(session)
    admin, a, b = people[:3]
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    for user in (a, b):
        for _ in range(3):
            await strikes.issue_strike(
                session, user_id=user.id, reason="x", issued_by_id=admin.id, today=MONDAY
            )
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
    assert len(await strikes.get_active_strikes(session, a.id)) == 0

    # The penalty week replaces the regular Mülldienst.
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=7)))
    slot = await duties.get_slot(session, Duty.trash, MONDAY + timedelta(days=7))
    assert slot.is_penalty and [x.user_id for x in slot.assignments] == [a.id]


async def test_penalty_does_not_count_as_regular_trash(session):
    people = await make_users(session, n=3)
    a = people[1]
    for _ in range(3):
        await strikes.issue_strike(session, user_id=a.id, reason="x", today=MONDAY)
    balances = await duties.load_balances(session, Duty.trash)
    assert balances[a.id].credit == 0


async def test_mark_done_permissions(session):
    people = await make_users(session)
    admin = people[0]
    # Keep the admin off today's duty, so the outsider/admin checks are meaningful.
    await users.add_away_period(session, admin, MONDAY, MONDAY + timedelta(days=1), MONDAY)
    first, second, p1, p2 = await assign_kitchen_pair(session)
    outsider = next(p for p in people if p.id not in {p1.id, p2.id} and not p.is_admin)

    with pytest.raises(DutyError):
        await duties.mark_duty_done(session, first, outsider, make_datetime(MONDAY, 20))
    # Nobody can mark someone else's duty.
    with pytest.raises(DutyError):
        await duties.mark_duty_done(session, first, p2, make_datetime(MONDAY, 20))

    # Too late for a resident, but the Flursprecher can still do it.
    tuesday = MONDAY + timedelta(days=1)
    t_first, _, t1, _ = await assign_kitchen_pair(session, tuesday)
    with pytest.raises(DutyError):
        await duties.mark_duty_done(
            session, t_first, t1, make_datetime(tuesday + timedelta(days=1), 12)
        )
    await duties.mark_duty_done(
        session, t_first, admin, make_datetime(tuesday + timedelta(days=1), 12)
    )
    assert t_first.status is Status.done


async def test_first_claim_closes_the_slot(session):
    await make_users(session)
    first, second, p1, p2 = await assign_kitchen_pair(session)

    await duties.mark_duty_done(session, second, p2, make_datetime(MONDAY, 20))
    assert second.status is Status.done and second.completed_by_id == p2.id
    assert first.status is Status.covered
    with pytest.raises(DutyError):
        await duties.mark_duty_done(session, first, p1, make_datetime(MONDAY, 21))
    await session.commit()

    # Only the one who cleaned gets credit; the partner gets neither credit nor strike.
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p2.id].credit == 1 and balances[p1.id].credit == 0
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=2)))
    assert await strikes.get_active_strikes(session, p1.id) == []


async def test_partners_clicking_at_once_claim_the_slot_once(session, session_factory):
    await make_users(session)
    first, second, p1, p2 = await assign_kitchen_pair(session)
    await session.commit()

    async def claim(assignment_id, user_id):
        """Mark the duty done in its own session, like a separate request."""
        async with session_factory() as s:
            a = await duties.get_assignment(s, assignment_id)
            await duties.mark_duty_done(s, a, await s.get(User, user_id), make_datetime(MONDAY, 20))
            await s.commit()

    results = await asyncio.gather(
        claim(first.id, p1.id), claim(second.id, p2.id), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in results) == ["DutyError", "NoneType"]
    done = await session.scalars(
        select(Assignment).where(
            Assignment.slot_id == first.slot_id, Assignment.status == Status.done
        )
    )
    assert len(done.all()) == 1


async def test_database_allows_one_done_per_slot(session):
    await make_users(session)
    first, second, _, _ = await assign_kitchen_pair(session)
    first.status = second.status = Status.done
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_open_slot_is_shared_for_picking_but_not_for_debt(session):
    await make_users(session)
    _, _, p1, p2 = await assign_kitchen_pair(session)
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].pending == balances[p2.id].pending == Fraction(1, 2)
    # An open slot counts only for picking: nobody gets credit before it's marked done.
    assert balances[p1.id].credit == balances[p2.id].credit == 0

    # Tomorrow is picked before anyone marked today done; today's pair isn't picked again.
    tomorrow = await duties.materialize_slot(session, Duty.kitchen, MONDAY + timedelta(days=1))
    assert {a.user_id for a in tomorrow.assignments}.isdisjoint({p1.id, p2.id})


async def test_preview_shows_open_pair_as_roles(session):
    await make_users(session)
    first, _, p1, p2 = await assign_kitchen_pair(session)

    async def get_shown():
        """The user ids and roles the kitchen preview shows for the next 30 days."""
        preview = await duties.build_preview(
            session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=30)
        )
        return [u for e in preview for u in e.people]

    before = await get_shown()
    # Whoever doesn't clean is up again tomorrow; whoever does, much later.
    assert before[:2].count(Role(MONDAY, False)) == 1
    assert Role(MONDAY, True) in before
    assert p1.id not in before and p2.id not in before

    # Once it's done, the outcome is known and the names are back.
    await duties.mark_duty_done(session, first, p1, make_datetime(MONDAY, 20))
    after = await get_shown()
    assert not any(isinstance(u, Role) for u in after)
    assert p2.id in after


async def test_partner_who_did_not_clean_is_picked_next(session):
    await make_users(session)
    first, _, p1, p2 = await assign_kitchen_pair(session)
    await duties.mark_duty_done(session, first, p1, make_datetime(MONDAY, 20))
    tomorrow = await duties.materialize_slot(session, Duty.kitchen, MONDAY + timedelta(days=1))
    ids = {a.user_id for a in tomorrow.assignments}
    assert p2.id in ids and p1.id not in ids


async def test_dispute_gives_strike_and_removes_credit(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away_period(session, admin, MONDAY, MONDAY, MONDAY)
    first, second, p1, p2 = await assign_kitchen_pair(session)
    await duties.mark_duty_done(session, first, p1, make_datetime(MONDAY, 20))

    with pytest.raises(DutyError):
        await duties.dispute_duty(session, first, p2, make_datetime(MONDAY, 21))
    await duties.dispute_duty(session, first, admin, make_datetime(MONDAY, 21))
    await session.commit()

    assert first.status is Status.missed and first.disputed_at is not None
    assert second.status is Status.covered
    [strike] = await strikes.get_active_strikes(session, p1.id)
    assert strike.issued_by_id == admin.id and strike.assignment_id == first.id
    balances = await duties.load_balances(session, Duty.kitchen)
    assert balances[p1.id].credit == 0
    # The deadline passing later doesn't add a second strike.
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=2)))
    assert len(await strikes.get_active_strikes(session, p1.id)) == 1


async def test_admin_marking_missed_done_revokes_strike(session):
    people = await make_users(session)
    admin = people[0]
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=1), 13))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    a = await duties.get_assignment(session, kitchen.assignments[0].id)
    assert a.status is Status.missed
    await duties.mark_duty_done(session, a, admin, make_datetime(MONDAY + timedelta(days=2)))
    assert len(await strikes.get_active_strikes(session, a.user_id)) == 0


async def test_away_keeps_running_duty_but_skips_later_ones(session):
    await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    leaving = kitchen.assignments[0].user
    leaving_id = leaving.id

    # Reporting an absence at the last minute doesn't get you out of today's duty.
    await users.add_away_period(session, leaving, MONDAY, MONDAY + timedelta(days=3), MONDAY)
    await session.commit()
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    assert leaving_id in [a.user_id for a in kitchen.assignments]

    preview = await duties.build_preview(
        session, Duty.kitchen, MONDAY + timedelta(days=1), MONDAY + timedelta(days=3)
    )
    assert all(leaving_id not in e.people for e in preview)


async def test_delete_user_keeps_history_and_replaces(session):
    people = await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    trash = await duties.get_slot(session, Duty.trash, MONDAY)
    gone = trash.assignments[0].user
    gone_id = gone.id
    admin = next(p for p in people if p is not gone)
    await users.delete_user(session, gone, admin, MONDAY)
    await session.commit()
    trash = await duties.get_slot(session, Duty.trash, MONDAY)
    assert [a.user_id for a in trash.assignments] != [gone_id]
    assert len(trash.assignments) == 1


async def test_month_of_daily_jobs_is_fair(session):
    """30 days, one random person of each pair cleans: everybody cleans 2 times."""
    rng = random.Random(7)
    await make_users(session)
    day = MONDAY
    for _ in range(30):
        await jobs.run_daily_job(session, make_datetime(day))
        slot = await duties.get_slot(session, Duty.kitchen, day)
        claim = await duties.get_assignment(session, rng.choice(slot.assignments).id)
        await duties.mark_duty_done(session, claim, claim.user, make_datetime(day, 20))
        await session.commit()
        day += timedelta(days=1)
    done_by = await session.scalars(
        select(Assignment.user_id)
        .join(Assignment.slot)
        .where(Slot.duty == Duty.kitchen, Assignment.status == Status.done)
    )
    counts = Counter(done_by).values()
    assert len(counts) == 15 and max(counts) - min(counts) <= 1


async def test_dashboard_fills_in_slots_but_gives_no_strikes(session, client):
    await make_users(session)
    two_days_ago = settings.today() - timedelta(days=2)
    await jobs.run_daily_job(session, make_datetime(two_days_ago))
    overdue = await duties.get_slot(session, Duty.kitchen, two_days_ago)

    assert (await client.get("/")).status_code == 200
    assert await duties.get_slot(session, Duty.kitchen, settings.today()) is not None
    await session.refresh(overdue, ["assignments"])
    assert all(a.status is Status.pending for a in overdue.assignments)
    assert not any(
        [await strikes.get_active_strikes(session, a.user_id) for a in overdue.assignments]
    )

    # Striking stays with the daily job.
    await jobs.run_daily_job(session, settings.now())
    await session.refresh(overdue, ["assignments"])
    assert all(a.status is Status.missed for a in overdue.assignments)


async def test_web_permissions(session, client):
    await make_users(session, n=3)
    r = await client.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = await client.get("/")
    assert r.status_code == 200 and "Küchendienst" in r.text

    await log_in(client, "p2")
    assert (await client.get("/admin")).status_code == 403
    assert (await client.get("/me")).status_code == 200

    await client.post("/logout")
    await log_in(client, "p1")
    assert (await client.get("/admin")).status_code == 200
    r = await client.post(
        "/admin/users",
        data={"name": "Neu", "room_number": "A420", "username": "neu", "password": "pw123456"},
    )
    assert r.status_code == 303
    assert "Neu" in (await client.get("/admin")).text


async def test_help_page(session, client):
    await make_users(session, n=3)
    r = await client.get("/help")
    assert r.status_code == 200 and "Küchendienst" in r.text
    assert "Für den Flursprecher" not in r.text

    await log_in(client, "p2")
    assert "Für den Flursprecher" not in (await client.get("/help")).text
    await client.post("/logout")
    await log_in(client, "p1")
    assert "Für den Flursprecher" in (await client.get("/help")).text


async def test_understaffed_slot_is_topped_up(session):
    # The daily job ran before anyone lived on the floor.
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    assert (await duties.get_slot(session, Duty.kitchen, MONDAY)).assignments == []

    await make_users(session, n=5)
    await jobs.run_daily_job(session, make_datetime(MONDAY, 10))
    assert len((await duties.get_slot(session, Duty.kitchen, MONDAY)).assignments) == 2
    assert len((await duties.get_slot(session, Duty.trash, MONDAY)).assignments) == 1


async def test_excusing_a_disputed_duty_clears_the_dispute(session, client):
    people = await make_users(session, n=4)
    admin = people[0]
    first, _, claimer, _ = await assign_kitchen_pair(session, settings.today())
    await duties.mark_duty_done(session, first, claimer, settings.now())
    await duties.dispute_duty(session, first, admin, settings.now())
    await duties.excuse_duty(session, first, admin, settings.now())
    await session.commit()

    assert first.status is Status.excused and first.disputed_at is None
    assert await strikes.get_active_strikes(session, claimer.id) == []
    await log_in(client, "p2")
    text = (await client.get("/history")).text
    assert "entschuldigt" in text and "beanstandet" not in text


async def test_history_page_and_dispute(session, client):
    await make_users(session, n=4)
    first, _, p1, _ = await assign_kitchen_pair(session, settings.today())
    await duties.mark_duty_done(session, first, p1, settings.now())
    await session.commit()

    assert (await client.get("/history")).status_code == 303  # login required
    await log_in(client, "p2")
    r = await client.get("/history")
    assert r.status_code == 200 and str(render_person(p1)) in r.text and "Beanstanden" not in r.text
    assert (await client.post(f"/admin/assignments/{first.id}/dispute")).status_code == 403

    await client.post("/logout")
    await log_in(client, "p1")
    r = await client.get("/history")
    assert "Beanstanden" in r.text
    r = await client.post(f"/admin/assignments/{first.id}/dispute?next=/history")
    assert r.status_code == 303
    assert "beanstandet" in (await client.get("/history")).text


async def test_top_up_skips_slot_that_is_already_done(session):
    # Only one resident: they get today's kitchen duty alone and do it.
    [first] = await make_users(session, n=1)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    a = await duties.get_assignment(session, kitchen.assignments[0].id)
    await duties.mark_duty_done(session, a, first, make_datetime(MONDAY, 10))
    await session.commit()

    # A resident added later must not be put on the finished slot (and struck for it).
    newbie = await users.create_user(
        session, name="Neu", room_number="A420", username="neu", password=PASSWORD
    )
    await duties.fill_current_slots(session, MONDAY)
    await session.commit()
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    assert [x.user_id for x in kitchen.assignments] == [first.id]

    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=1), 13))
    assert await strikes.get_active_strikes(session, newbie.id) == []


async def test_web_errors_do_not_break_the_page(session, client):
    people = await make_users(session, n=4)
    first, _, p1, _ = await assign_kitchen_pair(session, settings.today())
    outsider = next(p for p in people if p.id != p1.id and not p.is_admin)

    # Clicking someone else's card button reloads the page instead of nesting it in the card.
    await log_in(client, outsider.username)
    r = await client.post(f"/assignments/{first.id}/done?card=1", headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.headers["HX-Refresh"] == "true"

    await client.post("/logout")
    await log_in(client, "p1")
    r = await client.post("/admin/assignments/999999/excuse")
    assert r.status_code == 303 and r.headers["location"] == "/admin"


async def test_login_is_blocked_after_too_many_failures(session, client, monkeypatch):
    monkeypatch.setattr("app.web.login_throttle", LoginThrottle(max_failures=3))
    await make_users(session, n=2)
    for _ in range(3):
        r = await client.post("/login", data={"username": "p2", "password": "wrong"})
        assert "Benutzername oder Passwort falsch" in (await client.get(r.headers["location"])).text

    # Even the right password is refused while blocked.
    r = await client.post("/login", data={"username": "p2", "password": PASSWORD})
    assert "Zu viele Fehlversuche" in (await client.get(r.headers["location"])).text
    assert (await client.get("/me")).status_code == 303
    # Other users can still log in.
    await log_in(client, "p1")
    assert (await client.get("/me")).status_code == 200


async def test_short_passwords_are_rejected(session):
    with pytest.raises(UserError):
        await users.create_user(
            session, name="A", room_number="A401", username="a", password="short"
        )


async def test_initial_admin_only_on_empty_database(session, monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "")  # whatever the local .env says
    assert await users.create_initial_admin(session) is None
    for key, value in dict(username="Chef", password=PASSWORD, name="Chef", room="A401").items():
        monkeypatch.setattr(settings, f"admin_{key}", value)
    admin = await users.create_initial_admin(session)
    assert admin.is_admin and admin.username == "chef"
    await session.commit()
    # Not again once there are users, e.g. after the admin was deleted and others were added.
    assert await users.create_initial_admin(session) is None


async def test_moving_out_does_not_change_the_others_balance(session):
    people = await make_users(session, n=5)
    day = MONDAY
    for _ in range(10):
        await jobs.run_daily_job(session, make_datetime(day))
        slot = await duties.get_slot(session, Duty.kitchen, day)
        claim = await duties.get_assignment(session, slot.assignments[0].id)
        await duties.mark_duty_done(session, claim, claim.user, make_datetime(day, 20))
        await session.commit()
        day += timedelta(days=1)

    leaving = people[1]
    staying_ids = [p.id for p in people if p is not leaving]
    before = await duties.load_balances(session, Duty.kitchen)
    # Move out on the last scheduled day, so no new slot changes the balances.
    await users.delete_user(session, leaving, people[0], day - timedelta(days=1))
    await session.commit()
    after = await duties.load_balances(session, Duty.kitchen)
    assert [after[uid].debt for uid in staying_ids] == [before[uid].debt for uid in staying_ids]

    # The new resident of that room starts with a clean slate.
    newbie = await users.create_user(
        session, name="Neu", room_number=leaving.room_number, username="neu", password=PASSWORD
    )
    await session.commit()
    balances = await duties.load_balances(session, Duty.kitchen)
    assert newbie.id not in balances
    assert await strikes.get_active_strikes(session, newbie.id) == []


async def test_moving_out_drops_open_penalty_week(session):
    people = await make_users(session)
    leaving = people[1]
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    for _ in range(3):
        await strikes.issue_strike(session, user_id=leaving.id, reason="x", today=MONDAY)
    await session.commit()

    await users.delete_user(session, leaving, people[0], MONDAY)
    await session.commit()
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=7)))
    slot = await duties.get_slot(session, Duty.trash, MONDAY + timedelta(days=7))
    assert not slot.is_penalty and len(slot.assignments) == 1


async def test_own_name_is_highlighted(session, client):
    people = await make_users(session, n=3)
    await log_in(client, "p2")
    text = (await client.get("/stats")).text
    assert str(render_person(people[1], is_me=True)) in text
    assert str(render_person(people[0])) in text


async def test_stats_colour_strikes(session, client):
    people = await make_users(session, n=3)
    await strikes.issue_strike(session, user_id=people[1].id, reason="x", today=MONDAY)
    for _ in range(2):
        await strikes.issue_strike(session, user_id=people[2].id, reason="x", today=MONDAY)
    await session.commit()
    text = (await client.get("/stats")).text
    assert '"strikes ok">0 / 3' in text
    assert '"strikes warn">1 / 3' in text
    assert '"strikes bad">2 / 3' in text


def freeze_time(monkeypatch, now: datetime) -> None:
    """Make `settings.now()` and `settings.today()` return `now`, for the web pages."""
    monkeypatch.setattr(type(settings), "now", lambda self: now)


async def give_strikes(session, user, n):
    """`n` strikes by hand, on `MONDAY`."""
    for _ in range(n):
        await strikes.issue_strike(session, user_id=user.id, reason="x", today=MONDAY)
    await session.commit()


async def get_penalty_weeks(session, user_id):
    """The user's penalty slots, by date."""
    return list(
        await session.scalars(
            select(Slot)
            .join(Slot.assignments)
            .where(Slot.is_penalty.is_(True), Assignment.user_id == user_id)
            .order_by(Slot.start_date)
        )
    )


async def test_marking_the_partner_done_keeps_the_dispute_strike(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away_period(session, admin, MONDAY, MONDAY, MONDAY)
    first, second, claimer, _ = await assign_kitchen_pair(session)
    await duties.mark_duty_done(session, first, claimer, make_datetime(MONDAY, 20))
    await duties.dispute_duty(session, first, admin, make_datetime(MONDAY, 21))
    await duties.mark_duty_done(session, second, admin, make_datetime(MONDAY, 22))
    await session.commit()

    assert first.status is Status.missed and first.disputed_at is not None
    assert second.status is Status.done
    assert len(await strikes.get_active_strikes(session, claimer.id)) == 1


async def test_only_open_or_missed_duties_can_be_excused(session):
    people = await make_users(session)
    admin = people[0]
    await users.add_away_period(session, admin, MONDAY, MONDAY, MONDAY)
    first, second, claimer, _ = await assign_kitchen_pair(session)
    await duties.mark_duty_done(session, first, claimer, make_datetime(MONDAY, 20))
    for a in (first, second):  # done and covered
        with pytest.raises(DutyError):
            await duties.excuse_duty(session, a, admin, make_datetime(MONDAY, 21))
    assert first.status is Status.done


async def test_disputing_twice_at_once_gives_one_strike(session, session_factory):
    people = await make_users(session)
    admin = people[0]
    await users.add_away_period(session, admin, MONDAY, MONDAY, MONDAY)
    first, _, claimer, _ = await assign_kitchen_pair(session)
    await duties.mark_duty_done(session, first, claimer, make_datetime(MONDAY, 20))
    await session.commit()

    async def dispute():
        """Dispute in its own session, like a second browser tab."""
        async with session_factory() as s:
            a = await duties.get_assignment(s, first.id)
            await duties.dispute_duty(s, a, await s.get(User, admin.id), make_datetime(MONDAY, 21))
            await s.commit()

    results = await asyncio.gather(dispute(), dispute(), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["DutyError", "NoneType"]
    assert len(await strikes.get_active_strikes(session, claimer.id)) == 1


async def test_yesterdays_duty_can_be_marked_done_until_the_deadline(session, client, monkeypatch):
    await make_users(session)
    first, _, p1, _ = await assign_kitchen_pair(session)
    await session.commit()
    freeze_time(monkeypatch, make_datetime(MONDAY + timedelta(days=1), 8))

    await log_in(client, p1.username)
    r = await client.get("/")
    assert f'id="card-{first.slot_id}"' in r.text and "noch offen" in r.text
    r = await client.post(f"/assignments/{first.id}/done?card=1", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Erledigt von" in r.text

    # After the deadline the card is gone (the daily job marks it missed).
    freeze_time(monkeypatch, make_datetime(MONDAY + timedelta(days=2), 13))
    assert f'id="card-{first.slot_id}"' not in (await client.get("/")).text


async def test_late_done_cancels_the_penalty_week_of_its_strike(session):
    people = await make_users(session)
    admin = people[0]
    first, _, p1, _ = await assign_kitchen_pair(session)
    await give_strikes(session, p1, 2)
    # The missed duty gives the third strike, and with it a penalty week.
    await jobs.run_daily_job(session, make_datetime(MONDAY + timedelta(days=1), 13))
    assert len(await get_penalty_weeks(session, p1.id)) == 1

    first = await duties.get_assignment(session, first.id)
    await duties.mark_duty_done(
        session, first, admin, make_datetime(MONDAY + timedelta(days=1), 14)
    )
    await session.commit()
    assert await get_penalty_weeks(session, p1.id) == []
    assert len(await strikes.get_active_strikes(session, p1.id)) == 2


async def test_revoking_a_paid_off_strike_cancels_the_penalty_week(session, client):
    people = await make_users(session, n=4)
    await give_strikes(session, people[1], 3)
    [penalty] = await get_penalty_weeks(session, people[1].id)
    assert penalty.start_date > settings.today()
    strike = (await session.scalars(select(Strike).where(Strike.user_id == people[1].id))).first()

    await log_in(client, "p1")
    r = await client.post(f"/admin/strikes/{strike.id}/revoke")
    assert r.status_code == 303
    assert await get_penalty_weeks(session, people[1].id) == []
    assert len(await strikes.get_active_strikes(session, people[1].id)) == 2


async def test_absence_moves_the_penalty_week(session):
    people = await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    await give_strikes(session, people[1], 3)
    [penalty] = await get_penalty_weeks(session, people[1].id)
    week = penalty.start_date

    await users.add_away_period(session, people[1], week, week + timedelta(days=2), MONDAY)
    await session.commit()
    [moved] = await get_penalty_weeks(session, people[1].id)
    assert moved.start_date > week
    assert await strikes.get_active_strikes(session, people[1].id) == []


async def test_pausing_cancels_the_penalty_week_until_resumed(session):
    people = await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    await give_strikes(session, people[1], 3)

    await users.set_user_active(session, people[1], False, MONDAY)
    await session.commit()
    assert await get_penalty_weeks(session, people[1].id) == []
    assert len(await strikes.get_active_strikes(session, people[1].id)) == 3
    # A strike while paused doesn't add one either.
    await give_strikes(session, people[1], 1)
    assert await get_penalty_weeks(session, people[1].id) == []

    await users.set_user_active(session, people[1], True, MONDAY)
    await session.commit()
    assert len(await get_penalty_weeks(session, people[1].id)) == 1
    assert len(await strikes.get_active_strikes(session, people[1].id)) == 1


async def test_paused_people_get_no_share_of_the_current_slots(session):
    people = await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    picked = {a.user_id for a in kitchen.assignments}
    idle = next(p for p in people if p.id not in picked)

    await users.set_user_active(session, idle, False, MONDAY)
    await session.commit()
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    await session.refresh(kitchen, ["eligible"])
    assert idle.id not in {e.user_id for e in kitchen.eligible}


async def test_pausing_replaces_open_duties_and_resuming_fills_in(session):
    people = await make_users(session, n=2, admin=False)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    await users.set_user_active(session, people[0], False, MONDAY)
    await session.commit()
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    assert [a.user_id for a in kitchen.assignments] == [people[1].id]

    await users.set_user_active(session, people[0], True, MONDAY)
    await session.commit()
    kitchen = await duties.get_slot(session, Duty.kitchen, MONDAY)
    assert {a.user_id for a in kitchen.assignments} == {p.id for p in people}


async def test_dashboard_with_two_people_per_trash_week(session, client, monkeypatch):
    monkeypatch.setattr(settings, "trash_people_per_week", 2)
    await make_users(session, n=6)
    r = await client.get("/")
    assert r.status_code == 200 and "Mülldienst – Vorschau" in r.text


async def test_trash_deadline_is_monday_noon_after_the_week(session):
    await make_users(session)
    await jobs.run_daily_job(session, make_datetime(MONDAY))
    trash = await duties.get_slot(session, Duty.trash, MONDAY)
    next_monday = MONDAY + timedelta(days=7)
    assert duties.get_deadline(trash) == make_datetime(next_monday, settings.deadline_hour)

    await jobs.run_daily_job(session, make_datetime(next_monday, settings.deadline_hour - 1))
    await session.refresh(trash, ["assignments"])
    assert trash.assignments[0].status is Status.pending
    await jobs.run_daily_job(session, make_datetime(next_monday, settings.deadline_hour))
    await session.refresh(trash, ["assignments"])
    assert trash.assignments[0].status is Status.missed


async def test_away_period_must_be_sensible(session):
    [p] = await make_users(session, n=1)
    with pytest.raises(UserError):
        await users.add_away_period(session, p, MONDAY, MONDAY - timedelta(days=1), MONDAY)
    with pytest.raises(UserError):
        await users.add_away_period(
            session, p, MONDAY - timedelta(days=5), MONDAY - timedelta(days=1), MONDAY
        )


async def test_nobody_can_delete_someone_elses_away_period(session, client):
    people = await make_users(session, n=3)
    await users.add_away_period(
        session, people[2], MONDAY, MONDAY + timedelta(days=1), MONDAY - timedelta(days=1)
    )
    await session.commit()
    [away] = (await session.scalars(select(AwayPeriod))).all()

    await log_in(client, "p2")
    await client.post(f"/me/away/{away.id}/delete")
    assert (await session.scalars(select(AwayPeriod))).all() != []


async def test_change_password(session):
    [p] = await make_users(session, n=1)
    with pytest.raises(UserError):
        await users.change_password(session, p, "wrong-password", "new-password")
    with pytest.raises(UserError):
        await users.change_password(session, p, PASSWORD, "short")
    await users.change_password(session, p, PASSWORD, "new-password")
    assert await auth.authenticate_user(session, p.username, "new-password") == p


async def test_flursprecher_cannot_drop_their_own_role_or_account(session, client):
    people = await make_users(session, n=2)
    await log_in(client, "p1")
    await client.post(f"/admin/users/{people[0].id}/edit", data={"name": "", "room_number": ""})
    await client.post(f"/admin/users/{people[0].id}/delete")
    await session.refresh(people[0])
    assert people[0].is_admin and people[0].name == "Person 1"

    with pytest.raises(UserError):
        await users.delete_user(session, people[0], people[0], MONDAY)
