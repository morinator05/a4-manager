"""Fair duty assignment ("deficit" scheduling).

Every slot (a day of kitchen duty, a week of trash duty) is one job. `n` people are put
on it (kitchen: 2, trash: 1), but only one of them does it: whoever marks it done first.
All users available for that slot accrue an equal share `1 / len(available)` of
*expected* work. A user's debt is `expected - credit`, where credit counts the jobs they
actually did. The `n` users with the highest debt are picked.

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
    expected: Fraction = Fraction(0)
    credit: Fraction = Fraction(0)
    last_duty: date | None = None

    @property
    def debt(self) -> Fraction:
        return self.expected - self.credit


@dataclass(frozen=True)
class Candidate:
    id: int
    room: str
    away: tuple[tuple[date, date], ...] = ()

    def is_available(self, start: date, end: date) -> bool:
        return not any(a_start <= end and start <= a_end for a_start, a_end in self.away)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def room_key(room: str) -> tuple:
    """Natural sort: "A4.2" before "A4.10"."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", room))


def accrue(balances: dict[int, Balance], eligible: Iterable[int]) -> None:
    """Spread one job's worth of expected work over everybody available for it."""
    eligible = list(eligible)
    if not eligible:
        return
    share = Fraction(1, len(eligible))
    for uid in eligible:
        balances.setdefault(uid, Balance()).expected += share


def pick(
    balances: dict[int, Balance], eligible: Iterable[int], n: int, rooms: dict[int, str]
) -> list[int]:
    """Choose the `n` eligible users that owe the most. Does not modify balances."""

    def key(uid: int):
        b = balances.get(uid, Balance())
        return (-b.debt, b.last_duty or date.min, room_key(rooms.get(uid, "")), uid)

    return sorted(eligible, key=key)[:n]


def credit(
    balances: dict[int, Balance], uid: int, on: date, amount: Fraction = Fraction(1)
) -> None:
    b = balances.setdefault(uid, Balance())
    b.credit += amount
    b.last_duty = on if b.last_duty is None else max(b.last_duty, on)


def plan(
    balances: dict[int, Balance],
    candidates: list[Candidate],
    slots: list[tuple[date, date]],
    n: int,
) -> list[list[int]]:
    """Simulate upcoming slots; nobody knows who will claim, so each picked person gets 1/n."""
    balances = copy.deepcopy(balances)
    rooms = {c.id: c.room for c in candidates}
    result = []
    for start, end in slots:
        eligible = [c.id for c in candidates if c.is_available(start, end)]
        accrue(balances, eligible)
        chosen = pick(balances, eligible, n, rooms)
        for uid in chosen:
            credit(balances, uid, start, Fraction(1, len(chosen)))
        result.append(chosen)
    return result
