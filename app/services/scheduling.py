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
    # Start of the latest slot they were put on; used to break ties.
    last_duty: date | None = None

    @property
    def debt(self) -> Fraction:
        """How much work the user owes; changes only when a duty is marked done."""
        return self.expected - self.credit

    @property
    def priority(self) -> Fraction:
        """The debt minus open shares; the highest are picked next."""
        return self.debt - self.pending


@dataclass(frozen=True)
class Candidate:
    """A user who can be picked (active), with the periods they are away."""

    id: int
    room: str
    away: tuple[tuple[date, date], ...] = ()

    def is_available(self, start: date, end: date) -> bool:
        """True if no away period overlaps the days `start`..`end` (both included)."""
        return not any(a_start <= end and start <= a_end for a_start, a_end in self.away)


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
        # Highest priority first; on a tie, whoever had their last duty longest ago (never =
        # first), then by room, so the result is always the same for the same input.
        return (-b.priority, b.last_duty or date.min, room_key(rooms.get(uid, "")), uid)

    return sorted(eligible, key=key)[:n]


def credit(
    balances: dict[int, Balance], uid: int, on: date, amount: Fraction = Fraction(1)
) -> None:
    """Book `amount` of done work for `uid` on the slot starting `on`."""
    b = balances.setdefault(uid, Balance())
    b.credit += amount
    b.last_duty = on if b.last_duty is None else max(b.last_duty, on)


def reserve(balances: dict[int, Balance], uid: int, on: date, amount: Fraction) -> None:
    """Book `amount` of a not yet done job for `uid` on the slot starting `on`."""
    b = balances.setdefault(uid, Balance())
    b.pending += amount
    b.last_duty = on if b.last_duty is None else max(b.last_duty, on)


def plan(
    balances: dict[int, Balance],
    candidates: list[Candidate],
    slots: list[tuple[date, date]],
    n: int,
) -> list[list[int]]:
    """Simulate who will be picked for upcoming slots, for the preview.

    Nobody knows yet who will claim each job, so every picked person reserves an equal
    part of it. `balances` is copied, not changed.

    Returns:
        One list of picked user ids per entry in `slots`, in the same order.
    """
    balances = copy.deepcopy(balances)
    rooms = {c.id: c.room for c in candidates}
    result = []
    for start, end in slots:
        eligible = [c.id for c in candidates if c.is_available(start, end)]
        accrue(balances, eligible)
        chosen = pick(balances, eligible, n, rooms)
        for uid in chosen:
            reserve(balances, uid, start, Fraction(1, len(chosen)))
        result.append(chosen)
    return result
