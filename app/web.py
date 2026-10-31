"""All pages and form actions (server-rendered Jinja templates, htmx for small updates).

Routes only parse input, call a service and render or redirect; the rules live in
`app/services/`. Every form POST answers with a redirect (303), so reloading the page
doesn't submit the form again.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from jinja2.runtime import Context
from markupsafe import Markup
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.deps import AdminUser, CurrentUser, OptionalUser, Session
from app.models import Assignment, AwayPeriod, Duty, Slot, Status, Strike, User
from app.services import auth, duties, jobs, strikes, users
from app.services.duties import DutyError
from app.services.login_throttle import WINDOW_SECONDS, login_throttle
from app.services.scheduling import Balance, Role
from app.services.users import UserError

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Shown instead of the name of someone who has moved out (their user is deleted).
FORMER_RESIDENT = "ehemalig"


def render_person(user: User | None, is_me: bool = False) -> Markup:
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
def render_person_for_viewer(context: Context, user: User | None) -> Markup:
    """`render_person` for templates: takes the logged-in user from the page's `user`."""
    viewer = context.get("user")
    is_me = user is not None and viewer is not None and user.id == viewer.id
    return render_person(user, is_me)


@pass_context
def render_role(context: Context, role: Role) -> Markup:
    """A preview stand-in: "Erledigt" (whoever cleans) or "Ausgelassen" (whoever doesn't).

    For an earlier slot that is still open (yesterday's before the deadline), its date
    follows.
    """
    label = "Erledigt" if role.did_it else "Ausgelassen"
    if role.start != context.get("today"):
        label += f" {format_short_date(role.start)}"
    css = "role role-done" if role.did_it else "role role-skip"
    return Markup('<span class="{}"><span aria-hidden="true">{}</span> {}</span>').format(
        css, "✓" if role.did_it else "✗", label
    )


def render_help_link(topic: str) -> Markup:
    """A small "?" after a heading, linking to that topic on the help page."""
    return Markup(
        '<a class="help-link" href="/help#{}" title="Wie funktioniert das?" '
        'aria-label="Wie funktioniert das?">?</a>'
    ).format(topic)


def format_short_date(d: date | None) -> str:
    """E.g. "05.01."; empty for None."""
    return f"{d:%d.%m.}" if d else ""


def format_long_date(d: date | None) -> str:
    """E.g. "05.01.2026"; empty for None."""
    return f"{d:%d.%m.%Y}" if d else ""


def format_date_time(d: datetime | None) -> str:
    """E.g. "05.01.2026 14:30", in the app's time zone; empty for None."""
    return f"{d.astimezone(settings.tz):%d.%m.%Y %H:%M}" if d else ""


def format_weekday(d: date) -> str:
    """The short German weekday, e.g. "Mo"."""
    return ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()]


def format_debt(balance: Balance | None) -> str:
    """A balance's debt with two decimals; "0.00" for someone without a balance yet."""
    return f"{float(balance.debt):.2f}" if balance else "0.00"


def is_role(x: object) -> bool:
    """Template test: whether a preview entry is a `Role` instead of a user id."""
    return isinstance(x, Role)


# How far back the Flursprecher page lists duties.
ADMIN_DAYS = 14
# Rows on "Mein Profil" and the Flursprecher page.
OWN_DUTIES_LIMIT = 30
ADMIN_STRIKES_LIMIT = 50

# The template global is named `render_person`: in templates the viewer is always known.
templates.env.globals["render_person"] = render_person_for_viewer
templates.env.globals["render_help_link"] = render_help_link
templates.env.globals["get_display_name"] = get_display_name
templates.env.globals["render_role"] = render_role
templates.env.tests["role"] = is_role
templates.env.globals["Duty"] = Duty
templates.env.globals["Status"] = Status
templates.env.globals["strike_threshold"] = settings.strike_threshold
templates.env.globals["min_password_length"] = auth.MIN_PASSWORD_LENGTH
templates.env.globals["room_pattern"] = users.ROOM_PATTERN.pattern
templates.env.globals["room_hint"] = users.ROOM_HINT
# Appended to the stylesheet URL, so browsers load it again after it changed instead of
# keeping an old copy from their cache.
templates.env.globals["css_version"] = int(
    (Path(__file__).parent / "static" / "style.css").stat().st_mtime
)
templates.env.filters["short_date"] = format_short_date
templates.env.filters["long_date"] = format_long_date
templates.env.filters["date_time"] = format_date_time
templates.env.filters["debt"] = format_debt
templates.env.filters["weekday"] = format_weekday


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
    """`url` if it's a path on this site, else "/". Stops redirects to other sites.

    Browsers read a backslash like a slash, so `/\\evil.com` would leave the site too.
    """
    if not url or not url.startswith("/") or "\\" in url:
        return "/"
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return "/"
    return url


def build_login_url(next_url: str) -> str:
    """The login page, going back to `next_url` (made safe) afterwards."""
    return f"/login?{urlencode({'next': get_safe_next(next_url)})}"


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
        "today": now.date(),
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
        minutes = WINDOW_SECONDS // 60
        flash_message(
            request, f"Zu viele Fehlversuche, bitte in {minutes} Minuten nochmal versuchen", "error"
        )
        return redirect_to(build_login_url(next))

    user = await auth.authenticate_user(session, username, password)
    if user is None:
        login_throttle.record_failure(throttle_key)
        flash_message(request, "Benutzername oder Passwort falsch", "error")
        return redirect_to(build_login_url(next))
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
    """Today's duties and the preview; visible without login.

    Earlier duties that can still be marked done (yesterday's before the deadline) come
    first, so nobody gets a strike for clicking a day late.
    """
    now = settings.now()
    today = now.date()
    slots = {duty: await duties.get_slot(session, duty, today) for duty in Duty}
    if None in slots.values():
        await jobs.fill_missing_slots(session, today)
        slots = {duty: await duties.get_slot(session, duty, today) for duty in Duty}
    late = await duties.load_late_slots(session, now)

    tomorrow = today + timedelta(days=1)
    horizon = today + timedelta(days=settings.preview_days)
    kitchen_preview = await duties.build_preview(session, Duty.kitchen, tomorrow, horizon)
    return render_page(
        request,
        "dashboard.html",
        user,
        cards=[build_duty_card(slot, slot.duty, user) for slot in late]
        + [build_duty_card(slot, duty, user) for duty, slot in slots.items()],
        kitchen_preview=kitchen_preview,
        kitchen_later=duties.find_roles_after_preview(kitchen_preview),
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
        .limit(OWN_DUTIES_LIMIT)
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
    """Delete one of your own away periods."""
    try:
        await users.delete_away_period(session, user, away_id)
        await session.commit()
        flash_message(request, "Abwesenheit gelöscht")
    except UserError as e:
        flash_message(request, str(e), "error")
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
    try:
        await users.change_password(session, user, old_password, new_password)
        await session.commit()
        flash_message(request, "Passwort geändert")
    except UserError as e:
        flash_message(request, str(e), "error")
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

# Key in the stats counts for penalty weeks, next to the `Status` values.
PENALTY = "penalty"


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
    # (user id, duty, status) -> number of assignments; penalty weeks count as PENALTY.
    counts = Counter(
        (uid, duty, PENALTY if is_penalty else status) for uid, duty, is_penalty, status in rows
    )
    return render_page(
        request,
        "stats.html",
        user,
        users=all_users,
        balances=balances,
        counts=counts,
        penalty=PENALTY,
        active=await strikes.count_active_strikes(session),
    )


# --- help -------------------------------------------------------------------


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
        job_minute=jobs.JOB_MINUTE,
    )


# --- admin ------------------------------------------------------------------


@router.get("/admin")
async def show_admin(request: Request, session: Session, user: AdminUser):
    """Flursprecher page: residents, strikes and recent duties."""
    all_users = await users.list_users(session)
    since = settings.today() - timedelta(days=ADMIN_DAYS)
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
        .limit(ADMIN_STRIKES_LIMIT)
    )
    return render_page(
        request,
        "admin/index.html",
        user,
        users=all_users,
        slots=list(slots),
        strikes=list(all_strikes),
        admin_days=ADMIN_DAYS,
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
    try:
        await users.update_user(
            session,
            target,
            user,
            name=name,
            room_number=room_number,
            password=password,
            is_admin=is_admin,
        )
        await session.commit()
        flash_message(request, f"{target.display_name} gespeichert")
    except UserError as e:
        flash_message(request, str(e), "error")
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
    name = target.display_name
    try:
        await users.delete_user(session, target, user, settings.today())
        await session.commit()
        flash_message(request, f"{name} gelöscht")
    except UserError as e:
        flash_message(request, str(e), "error")
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
async def run_daily_job_now(request: Request, session: Session, user: AdminUser):
    """Run the daily job now instead of waiting for the schedule."""
    await jobs.run_daily_job(session, settings.now())
    flash_message(request, "Tagesjob ausgeführt")
    return redirect_to("/admin")


@router.get("/healthz")
async def check_health(session: Session):
    """For the container health check: the app answers and reaches the database."""
    await session.execute(text("SELECT 1"))
    return {"ok": True}
