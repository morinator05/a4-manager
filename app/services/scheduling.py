"""Fair duty assignment ("deficit" scheduling).

Every slot (a day of kitchen duty, a week of trash duty) is one job. `n` people are put
on it (kitchen: 2, trash: 1), but only one of them does it: whoever marks it done first.
All users available for that slot accrue an equal share `1 / len(available)` of
*expected* work. A user's debt is `expected - credit`, where credit counts the jobs they
actually did. The `n` users with the highest debt are picked, not counting their share of
slots they are on that nobody has marked done yet (see `Balance.pending`).

Away or inactive users accrue nothing, so being away is automatically fair, and new
users start at debt 0 instead of inheriting a backlog. Missed duties give no credit,
so the person comes up again soon. The same goes for the partner who didn't clean.

The preview can't know who of an open slot's people will mark it done, so it replaces
them by two `Role`s: whoever does it (with the credit) and whoever doesn't (without).

Everything in here is pure (no database) so it can be tested and simulated cheaply.
"""

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction


@dataclass
class Balance:
    """One user's fairness account for one duty.

    Fractions instead of floats, so shares like 1/15 add up exactly.
    """

    # Sum of the user's shares of all slots they were available for.
    expected: Fraction = Fraction(0)
    # Jobs actually done (1 each).
    credit: Fraction = Fraction(0)
    # Shares of the open slots the user is on (nobody marked them done yet). Counts only for
    # picking: the next slot is picked before today's is done, and today's people shouldn't
    # be picked again just because they haven't marked it yet.
    pending: Fraction = Fraction(0)
    # Start of the latest slot they did; used to break ties. Being put on a slot without
    # doing it doesn't count, so it doesn't move you back in the queue.
    last_done: date | None = None

    @property
    def debt(self) -> Fraction:
        return self.expected - self.credit

    @property
    def priority(self) -> Fraction:
        """The highest are picked next."""
        return self.debt - self.pending


@dataclass(frozen=True)
class Candidate:
    """An active user, as input for picking."""

    id: int
    room: str
    away: tuple[tuple[date, date], ...] = ()

    def is_available(self, start: date, end: date) -> bool:
        """True if no away period overlaps the days `start`..`end` (both included)."""
        return not any(a_start <= end and start <= a_end for a_start, a_end in self.away)


@dataclass(frozen=True)
class Role:
    """Preview stand-in for whoever does (or doesn't do) an open slot.

    Shows where each outcome puts the slot's people, instead of guessing a name.
    """

    # First day of the open slot.
    start: date
    did_it: bool


def week_start(d: date) -> date:
    """The Monday of `d`'s week."""
    return d - timedelta(days=d.weekday())


def room_key(room: str) -> tuple:
    """Natural sort: "A4.2" before "A4.10"."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", room))


def accrue(balances: dict[int, Balance], eligible: Iterable[int | None]) -> None:
    """Spread one job's worth of expected work over everybody available for it.

    `None` is someone who has moved out since: they still count for the size of the share,
    but their share is dropped instead of being passed on to the others.
    """
    eligible = list(eligible)
    if not eligible:
        return
    share = Fraction(1, len(eligible))
    for uid in eligible:
        if uid is not None:
            balances.setdefault(uid, Balance()).expected += share


def pick(
    balances: dict[int, Balance], eligible: Iterable[int], n: int, rooms: dict[int, str]
) -> list[int]:
    """Choose the `n` eligible users that owe the most. Does not modify balances."""

    def key(uid: int):
        b = balances.get(uid, Balance())
        # Highest priority first; on a tie, whoever cleaned longest ago (never = first), then
        # by room, so the result is always the same for the same input.
        return (-b.priority, b.last_done or date.min, room_key(rooms.get(uid, "")), uid)

    return sorted(eligible, key=key)[:n]


def credit(balances: dict[int, Balance], uid: int, on: date) -> None:
    """Book one done job for `uid` on the slot starting `on`."""
    b = balances.setdefault(uid, Balance())
    b.credit += 1
    b.last_done = on if b.last_done is None else max(b.last_done, on)


def reserve(balances: dict[int, Balance], uid: int, amount: Fraction) -> None:
    """Book `amount` of a not yet done job for `uid`."""
    balances.setdefault(uid, Balance()).pending += amount


def _replace_by_roles(
    balances: dict[int, Balance],
    candidates: list[Candidate],
    start: date,
    member_ids: Iterable[int],
    roles: dict[int, Role],
) -> list[Candidate]:
    """Swap an open slot's people for one doer and `len(members) - 1` non-doers.

    Each role gets the members' average balance as it will be after the slot: the doer
    with the credit (and the slot as last done), the others unchanged, nothing pending.
    A role is away whenever any member is, since it could be any of them. Roles get
    negative ids, added to `roles`. Changes `balances` in place and returns the new
    candidates.
    """
    ids = set(member_ids)
    members = [c for c in candidates if c.id in ids]
    if len(members) < 2:
        return candidates
    before = [balances.pop(c.id, Balance()) for c in members]
    avg_expected = sum((b.expected for b in before), Fraction(0)) / len(before)
    avg_credit = sum((b.credit for b in before), Fraction(0)) / len(before)
    last_done = max((b.last_done for b in before if b.last_done), default=None)
    room = min((c.room for c in members), key=room_key)
    away = tuple(period for c in members for period in c.away)

    result = [c for c in candidates if c.id not in ids]
    for did_it in [True] + [False] * (len(members) - 1):
        rid = -(len(roles) + 1)
        roles[rid] = Role(start, did_it)
        balances[rid] = Balance(
            expected=avg_expected,
            credit=avg_credit + (1 if did_it else 0),
            last_done=start if did_it else last_done,
        )
        result.append(Candidate(rid, room, away))
    return result


def plan(
    balances: dict[int, Balance],
    candidates: list[Candidate],
    slots: list[tuple[date, date]],
    n: int,
    open_slots: Iterable[tuple[date, list[int]]] = (),
) -> list[list[int | Role]]:
    """Simulate who will be picked for upcoming slots, for the preview.

    Nobody knows yet who will claim each job, so every picked person reserves an equal
    part of it. `balances` is copied, not changed.

    `open_slots` are the real slots nobody has marked done yet, as (first day, people on
    it). Their people are replaced by `Role`s, so the preview shows when they're up again
    if they do the job and if they don't. A role is shown only once: after that it's a
    person whose next outcome is unknown again, so it leaves the simulation.

    Returns:
        One list of picked user ids (or `Role`s) per entry in `slots`, in the same order.
    """
    balances = copy.deepcopy(balances)
    roles: dict[int, Role] = {}
    for start, member_ids in open_slots:
        candidates = _replace_by_roles(balances, candidates, start, member_ids, roles)
    rooms = {c.id: c.room for c in candidates}
    result = []
    for start, end in slots:
        eligible = [c.id for c in candidates if c.is_available(start, end)]
        accrue(balances, eligible)
        chosen = pick(balances, eligible, n, rooms)
        for uid in chosen:
            reserve(balances, uid, Fraction(1, len(chosen)))
        result.append([roles.get(uid, uid) for uid in chosen])
        candidates = [c for c in candidates if c.id not in roles or c.id not in chosen]
    return result
