"""All pages and form actions (server-rendered Jinja templates, htmx for small updates).

Routes only parse input, call a service and render or redirect; the rules live in
`app/services/`. Every form POST answers with a redirect (303), so reloading the page
doesn't submit the form again.
"""

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import AdminUser, CurrentUser, OptionalUser, Session
from app.models import Assignment, AwayPeriod, Duty, Slot, Status, Strike, User
from app.services import auth, duties, jobs, strikes, users
from app.services.duties import DutyError
from app.services.login_throttle import login_throttle
from app.services.scheduling import Role
from app.services.users import UserError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Shown instead of the name of someone who has moved out (their user is deleted).
FORMER_RESIDENT = "ehemalig"


def render_person_html(user: User | None, is_me: bool = False) -> Markup:
    """A person as HTML: the first name stands out, the room is a quieter tag after it.

    `is_me` highlights the logged-in user's own entry, so they find themselves quickly.
    """
    if user is None:
        return Markup('<span class="muted">{}</span>').format(FORMER_RESIDENT)
    html = Markup('<span class="person-name">{}</span> <span class="room">{}</span>').format(
        user.first_name, user.room_number
    )
    if is_me:
        return Markup('<span class="me">{}</span>').format(html)
    return html


def get_display_name(user: User | None) -> str:
    """`user.display_name`, or `FORMER_RESIDENT` once they moved out."""
    return user.display_name if user else FORMER_RESIDENT


@pass_context
def render_person(context, user: User | None) -> Markup:
    """`render_person_html` for templates: takes the logged-in user from the page's `user`."""
    viewer = context.get("user")
    is_me = user is not None and viewer is not None and user.id == viewer.id
    return render_person_html(user, is_me)


@pass_context
def render_role(context, r: Role) -> Markup:
    """A preview stand-in: "Erledigt" (whoever cleans) or "Ausgelassen" (whoever doesn't).

    For an earlier slot that is still open (yesterday's before the deadline), its date
    follows.
    """
    text = "Erledigt" if r.did_it else "Ausgelassen"
    if r.start != context.get("today"):
        text += f" {r.start:%d.%m.}"
    css = "role role-done" if r.did_it else "role role-skip"
    return Markup('<span class="{}"><span aria-hidden="true">{}</span> {}</span>').format(
        css, "✓" if r.did_it else "✗", text
    )


def render_help_link(topic: str) -> Markup:
    """A small "?" after a heading, linking to that topic on the help page."""
    return Markup(
        '<a class="help-link" href="/help#{}" title="Wie funktioniert das?" '
        'aria-label="Wie funktioniert das?">?</a>'
    ).format(topic)


templates.env.globals["render_person"] = render_person
templates.env.globals["render_help_link"] = render_help_link
templates.env.globals["get_display_name"] = get_display_name
templates.env.globals["render_role"] = render_role
templates.env.tests["role"] = lambda x: isinstance(x, Role)
templates.env.globals["Duty"] = Duty
templates.env.globals["Status"] = Status
templates.env.globals["strike_threshold"] = settings.strike_threshold
templates.env.globals["min_password_length"] = auth.MIN_PASSWORD_LENGTH
# Appended to the stylesheet URL, so browsers load it again after it changed instead of
# keeping an old copy from their cache.
templates.env.globals["css_version"] = int(
    (Path(__file__).parent / "static" / "style.css").stat().st_mtime
)
templates.env.filters["short_date"] = lambda d: f"{d:%d.%m.}" if d else ""
templates.env.filters["long_date"] = lambda d: f"{d:%d.%m.%Y}" if d else ""
templates.env.filters["date_time"] = lambda d: (
    f"{d.astimezone(settings.tz):%d.%m.%Y %H:%M}" if d else ""
)
templates.env.filters["debt"] = lambda balance: f"{float(balance.debt):.2f}" if balance else "0.00"
templates.env.filters["weekday"] = lambda d: ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()]


def flash_message(request: Request, message: str, kind: str = "ok") -> None:
    """Queue a message for the next rendered page. `kind` is "ok" or "error"."""
    # Assign a new list: the session only notices assignments, not in-place changes.
    request.session["flash"] = [*request.session.get("flash", []), [kind, message]]


def render_page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    """Render a full page; adds the user, pending messages and today's date."""
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name, {"user": user, "messages": messages, "today": settings.today(), **ctx}
    )


def redirect_to(url: str) -> RedirectResponse:
    """Redirect after a form POST; 303 makes the browser follow it with a GET."""
    return RedirectResponse(url, status_code=303)


def get_safe_next(url: str | None) -> str:
    """`url` if it's a path on this site, else "/". Stops redirects to other sites."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


async def get_or_404[T](session: AsyncSession, model: type[T], row_id: int) -> T:
    """The row with this id, or a 404 page."""
    row = await session.get(model, row_id)
    if row is None:
        raise HTTPException(404)
    return row


def build_duty_card(slot: Slot | None, duty: Duty, user: User | None) -> dict:
    """Template variables for `partials/duty_card.html`.

    `can_mark` is a function, so the template can ask for each person on the card
    whether the logged-in user may mark it done.
    """
    now = settings.now()
    return {
        "slot": slot,
        "duty": duty,
        "deadline": duties.get_deadline(slot) if slot else None,
        "can_mark": (lambda a: user is not None and duties.can_mark_done(a, user, now)),
    }


# --- auth -------------------------------------------------------------------


@router.get("/login")
async def show_login(request: Request, user: OptionalUser, next: str = "/"):
    """Login form; `next` is where to go after logging in."""
    if user:
        return redirect_to(get_safe_next(next))
    return render_page(request, "login.html", None, next=next)


@router.post("/login")
async def log_in(
    request: Request,
    session: Session,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    """Log in. Too many failed attempts block the username from that IP for a while."""
    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{auth.normalize_username(username)}@{client_ip}"
    if login_throttle.is_blocked(throttle_key):
        flash_message(
            request, "Zu viele Fehlversuche, bitte in 15 Minuten nochmal versuchen", "error"
        )
        return redirect_to(f"/login?next={get_safe_next(next)}")

    user = await auth.authenticate_user(session, username, password)
    if user is None:
        login_throttle.record_failure(throttle_key)
        flash_message(request, "Benutzername oder Passwort falsch", "error")
        return redirect_to(f"/login?next={get_safe_next(next)}")
    login_throttle.reset_failures(throttle_key)
    request.session["user_id"] = user.id
    return redirect_to(get_safe_next(next))


@router.post("/logout")
async def log_out(request: Request):
    """Log out and go back to the dashboard."""
    request.session.clear()
    return redirect_to("/")


# --- dashboard --------------------------------------------------------------


@router.get("/")
async def show_dashboard(request: Request, session: Session, user: OptionalUser):
    """Today's duties and the preview; visible without login."""
    today = settings.today()
    slots = {duty: await duties.get_slot(session, duty, today) for duty in Duty}
    if None in slots.values():
        await jobs.run_daily_tick(session, settings.now())
        slots = {duty: await duties.get_slot(session, duty, today) for duty in Duty}

    tomorrow = today + timedelta(days=1)
    horizon = today + timedelta(days=settings.preview_days)
    kitchen_preview = await duties.build_preview(session, Duty.kitchen, tomorrow, horizon)
    roles = [u for e in kitchen_preview for u in e.user_ids if isinstance(u, Role)]
    # Whoever does today's duty is usually up only after the plan ends; say so below it.
    later = [
        r
        for r in dict.fromkeys(replace(u, did_it=True) for u in roles if not u.did_it)
        if r not in roles
    ]
    return render_page(
        request,
        "dashboard.html",
        user,
        cards=[build_duty_card(slot, duty, user) for duty, slot in slots.items()],
        kitchen_preview=kitchen_preview,
        kitchen_later=later,
        trash_preview=await duties.build_preview(session, Duty.trash, tomorrow, horizon),
        users_by_id={u.id: u for u in await session.scalars(select(User))},
    )


@router.post("/assignments/{assignment_id}/done")
async def mark_duty_done(request: Request, session: Session, user: CurrentUser, assignment_id: int):
    """Mark a duty as done, from the dashboard card or the Flursprecher page."""
    # The dashboard card posts with `card=1` and swaps only itself with the response.
    is_card_request = bool(request.headers.get("HX-Request") and request.query_params.get("card"))
    try:
        a = await duties.get_assignment(session, assignment_id)
        await duties.mark_duty_done(session, a, user, settings.now())
        await session.commit()
    except DutyError as e:
        flash_message(request, str(e), "error")
        if is_card_request:
            # A redirect would put the whole page into the card; reload so the flash shows.
            return Response(headers={"HX-Refresh": "true"})
        return redirect_to(get_safe_next(request.query_params.get("next")))
    if is_card_request:
        slot = await duties.get_slot(session, a.slot.duty, a.slot.start_date)
        return templates.TemplateResponse(
            request,
            "partials/duty_card.html",
            {**build_duty_card(slot, slot.duty, user), "user": user},
        )
    flash_message(request, "Als erledigt eingetragen")
    return redirect_to(get_safe_next(request.query_params.get("next")))


# --- personal page ----------------------------------------------------------


@router.get("/me")
async def show_own_page(request: Request, session: Session, user: CurrentUser):
    """Own page: away periods, password, strikes and recent duties."""
    aways = await session.scalars(
        select(AwayPeriod)
        .where(AwayPeriod.user_id == user.id, AwayPeriod.end_date >= settings.today())
        .order_by(AwayPeriod.start_date)
    )
    my_strikes = await session.scalars(
        select(Strike).where(Strike.user_id == user.id).order_by(Strike.created_at.desc())
    )
    recent_duties = await session.scalars(
        select(Assignment)
        .join(Assignment.slot)
        .where(Assignment.user_id == user.id)
        .options(selectinload(Assignment.slot))
        .order_by(Slot.start_date.desc())
        .limit(30)
    )
    return render_page(
        request,
        "me.html",
        user,
        aways=list(aways),
        strikes=list(my_strikes),
        recent_duties=list(recent_duties),
    )


@router.post("/me/away")
async def add_away_period(
    request: Request,
    session: Session,
    user: CurrentUser,
    start_date: Annotated[date, Form()],
    end_date: Annotated[date, Form()],
):
    """Add an away period for yourself."""
    try:
        await users.add_away_period(session, user, start_date, end_date, settings.today())
        await session.commit()
        flash_message(request, "Abwesenheit eingetragen")
    except UserError as e:
        flash_message(request, str(e), "error")
    return redirect_to("/me")


@router.post("/me/away/{away_id}/delete")
async def delete_away_period(request: Request, session: Session, user: CurrentUser, away_id: int):
    """Delete an away period (your own, or anyone's as Flursprecher)."""
    away = await get_or_404(session, AwayPeriod, away_id)
    if away.user_id != user.id and not user.is_admin:
        raise HTTPException(404)
    await users.remove_away_period(session, away)
    await session.commit()
    flash_message(request, "Abwesenheit gelöscht")
    return redirect_to("/me")


@router.post("/me/password")
async def change_password(
    request: Request,
    session: Session,
    user: CurrentUser,
    old_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    """Change your own password."""
    if not auth.verify_password(old_password, user.password_hash):
        flash_message(request, "Altes Passwort falsch", "error")
    elif len(new_password) < auth.MIN_PASSWORD_LENGTH:
        flash_message(request, auth.PASSWORD_TOO_SHORT, "error")
    else:
        user.password_hash = auth.hash_password(new_password)
        await session.commit()
        flash_message(request, "Passwort geändert")
    return redirect_to("/me")


# --- history ----------------------------------------------------------------

HISTORY_DAYS = 28


@router.get("/history")
async def show_history(
    request: Request, session: Session, user: CurrentUser, until: date | None = None
):
    """Who claimed which duty, `HISTORY_DAYS` days up to `until`."""
    today = settings.today()
    until = min(until or today, today)
    since = until - timedelta(days=HISTORY_DAYS - 1)
    slots = await session.scalars(
        select(Slot)
        .where(Slot.start_date <= until, Slot.end_date >= since)
        .options(
            selectinload(Slot.assignments).selectinload(Assignment.user),
            selectinload(Slot.assignments).selectinload(Assignment.completed_by),
        )
        .order_by(Slot.start_date.desc(), Slot.duty)
    )
    return render_page(
        request,
        "history.html",
        user,
        slots=list(slots),
        since=since,
        until=until,
        older=since - timedelta(days=1),
        newer=min(until + timedelta(days=HISTORY_DAYS), today) if until < today else None,
    )


# --- stats ------------------------------------------------------------------


@router.get("/stats")
async def show_stats(request: Request, session: Session, user: OptionalUser):
    """Per resident: duties done and missed, debt and strikes; visible without login."""
    all_users = await users.list_users(session)
    balances = {d: await duties.load_balances(session, d) for d in Duty}
    rows = await session.execute(
        select(Assignment.user_id, Slot.duty, Slot.is_penalty, Assignment.status).join(
            Assignment.slot
        )
    )
    # (user id, duty, status) -> number of assignments; penalty weeks count as "penalty".
    counts = Counter(
        (uid, duty, "penalty" if is_penalty else status) for uid, duty, is_penalty, status in rows
    )
    active = {u.id: len(await strikes.get_active_strikes(session, u.id)) for u in all_users}
    return render_page(
        request,
        "stats.html",
        user,
        users=all_users,
        balances=balances,
        counts=counts,
        active=active,
    )


# --- admin ------------------------------------------------------------------


@router.get("/help")
async def show_help(request: Request, user: OptionalUser):
    """How the duties work, for new residents; visible without login."""
    return render_page(
        request,
        "help.html",
        user,
        kitchen_people=settings.kitchen_people_per_day,
        trash_people=settings.trash_people_per_week,
        deadline_hour=settings.deadline_hour,
        deadline_days_after=settings.deadline_days_after,
        preview_days=settings.preview_days,
    )


@router.get("/admin")
async def show_admin(request: Request, session: Session, user: AdminUser):
    """Flursprecher page: residents, strikes and recent duties."""
    all_users = await users.list_users(session)
    since = settings.today() - timedelta(days=14)
    slots = await session.scalars(
        select(Slot)
        .where(Slot.end_date >= since)
        .options(selectinload(Slot.assignments).selectinload(Assignment.user))
        .order_by(Slot.start_date.desc(), Slot.duty)
    )
    all_strikes = await session.scalars(
        select(Strike)
        .options(selectinload(Strike.user), selectinload(Strike.issued_by))
        .order_by(Strike.created_at.desc())
        .limit(50)
    )
    return render_page(
        request,
        "admin/index.html",
        user,
        users=all_users,
        slots=list(slots),
        strikes=list(all_strikes),
    )


@router.post("/admin/users")
async def create_resident(
    request: Request,
    session: Session,
    user: AdminUser,
    name: Annotated[str, Form()],
    room_number: Annotated[str, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    is_admin: Annotated[bool, Form()] = False,
):
    """Add a resident and put them on today's duties if people are missing."""
    try:
        new = await users.create_user(
            session,
            name=name,
            room_number=room_number,
            username=username,
            password=password,
            is_admin=is_admin,
        )
        await duties.fill_current_slots(session, settings.today())
        await session.commit()
        flash_message(request, f"{new.display_name} hinzugefügt")
    except UserError as e:
        flash_message(request, str(e), "error")
    return redirect_to("/admin")


@router.post("/admin/users/{user_id}/edit")
async def edit_resident(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: int,
    name: Annotated[str, Form()],
    room_number: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
    is_admin: Annotated[bool, Form()] = False,
):
    """Change a resident's name, room, password or admin role (not your own role)."""
    target = await get_or_404(session, User, user_id)
    if password and len(password) < auth.MIN_PASSWORD_LENGTH:
        flash_message(request, auth.PASSWORD_TOO_SHORT, "error")
        return redirect_to("/admin")
    if room_number.strip():
        try:
            target.room_number = users.normalize_room(room_number)
        except UserError as e:
            flash_message(request, str(e), "error")
            return redirect_to("/admin")
    target.name = name.strip() or target.name
    if target.id != user.id:
        target.is_admin = is_admin
    if password:
        target.password_hash = auth.hash_password(password)
    await session.commit()
    flash_message(request, f"{target.display_name} gespeichert")
    return redirect_to("/admin")


@router.post("/admin/users/{user_id}/active")
async def toggle_resident_active(request: Request, session: Session, user: AdminUser, user_id: int):
    """Pause or resume a resident."""
    target = await get_or_404(session, User, user_id)
    await users.set_user_active(session, target, not target.active, settings.today())
    await session.commit()
    flash_message(
        request, f"{target.display_name} ist jetzt {'aktiv' if target.active else 'inaktiv'}"
    )
    return redirect_to("/admin")


@router.post("/admin/users/{user_id}/delete")
async def delete_resident(request: Request, session: Session, user: AdminUser, user_id: int):
    """Delete a resident who moved out (not yourself)."""
    target = await get_or_404(session, User, user_id)
    if target.id == user.id:
        flash_message(request, "Du kannst dich nicht selbst löschen", "error")
        return redirect_to("/admin")
    name = target.display_name
    await users.delete_user(session, target, settings.today())
    await session.commit()
    flash_message(request, f"{name} gelöscht")
    return redirect_to("/admin")


@router.post("/admin/strikes")
async def issue_strike(
    request: Request,
    session: Session,
    user: AdminUser,
    user_id: Annotated[int, Form()],
    reason: Annotated[str, Form()],
):
    """Give a strike by hand; the daily job gives the ones for missed duties."""
    target = await get_or_404(session, User, user_id)
    await strikes.issue_strike(
        session,
        user_id=target.id,
        reason=reason.strip() or "–",
        issued_by_id=user.id,
        today=settings.today(),
    )
    await session.commit()
    flash_message(request, f"Strike an {target.display_name} vergeben")
    return redirect_to("/admin")


@router.post("/admin/strikes/{strike_id}/revoke")
async def revoke_strike(request: Request, session: Session, user: AdminUser, strike_id: int):
    """Take a strike back from the Flursprecher page."""
    strike = await get_or_404(session, Strike, strike_id)
    await strikes.revoke_strike(session, strike, settings.now())
    await session.commit()
    flash_message(request, "Strike zurückgenommen")
    return redirect_to("/admin")


@router.post("/admin/assignments/{assignment_id}/excuse")
async def excuse_duty(request: Request, session: Session, user: AdminUser, assignment_id: int):
    """Excuse a duty: no strike, no credit."""
    try:
        a = await duties.get_assignment(session, assignment_id)
        await duties.excuse_duty(session, a, user, settings.now())
        await session.commit()
        flash_message(request, "Dienst entschuldigt")
    except DutyError as e:
        flash_message(request, str(e), "error")
    return redirect_to("/admin")


@router.post("/admin/assignments/{assignment_id}/dispute")
async def dispute_duty(request: Request, session: Session, user: AdminUser, assignment_id: int):
    """Dispute a duty marked done but not done: the claimer gets a strike."""
    try:
        a = await duties.get_assignment(session, assignment_id)
        await duties.dispute_duty(session, a, user, settings.now())
        await session.commit()
        flash_message(request, f"Beanstandet, {get_display_name(a.user)} bekommt einen Strike")
    except DutyError as e:
        flash_message(request, str(e), "error")
    return redirect_to(get_safe_next(request.query_params.get("next") or "/history"))


@router.post("/admin/tick")
async def run_daily_job(request: Request, session: Session, user: AdminUser):
    """Run the daily job now instead of waiting for the schedule."""
    await jobs.run_daily_tick(session, settings.now())
    flash_message(request, "Tagesjob ausgeführt")
    return redirect_to("/admin")
