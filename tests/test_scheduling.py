"""The pure fairness logic, simulated over many days without a database."""

import random
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise

import pytest

from app.services.scheduling import (
    Candidate,
    Role,
    accrue_expected_work,
    credit_done_job,
    get_room_sort_key,
    plan_upcoming_slots,
)

START = date(2026, 1, 5)  # a Monday


def make_one_day_slots(n: int, start: date = START) -> list[tuple[date, date]]:
    """`n` one-day slots from `start`."""
    return [(start + timedelta(days=i),) * 2 for i in range(n)]


def make_candidates(n: int) -> list[Candidate]:
    """`n` candidates with ids 1..n, nobody away."""
    return [Candidate(id=i, room=f"A4.{i}") for i in range(1, n + 1)]


def simulate_days(cands, n_days, start=START, miss_on=None, balances=None, claimer=None):
    """Day-by-day like the real system: one of the picked people claims the job.

    On `miss_on` nobody does it. Returns the balances, the picks and who did each day.
    """
    balances = {} if balances is None else balances
    claimer = claimer or (lambda picked: picked[0])
    picks, doers = [], []
    for d, _ in make_one_day_slots(n_days, start):
        [picked] = plan_upcoming_slots(balances, cands, [(d, d)], 2)
        accrue_expected_work(balances, [c.id for c in cands if c.is_available(d, d)])
        doer = None if d == miss_on or not picked else claimer(picked)
        if doer is not None:
            credit_done_job(balances, doer, d)
        picks.append(picked)
        doers.append(doer)
    return balances, picks, doers


@pytest.mark.parametrize("n_people", [3, 7, 15, 30])
def test_counts_stay_within_one(n_people):
    result = plan_upcoming_slots({}, make_candidates(n_people), make_one_day_slots(365), 2)
    counts = Counter(uid for picked in result for uid in picked)
    assert all(len(set(p)) == 2 for p in result)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_no_double_duty_on_consecutive_days():
    result = plan_upcoming_slots({}, make_candidates(15), make_one_day_slots(60), 2)
    for today, tomorrow in pairwise(result):
        assert not set(today) & set(tomorrow)


def test_away_people_are_skipped_and_not_charged():
    cands = make_candidates(15)
    away_start, away_end = START + timedelta(days=30), START + timedelta(days=89)
    cands[0] = replace(cands[0], away=((away_start, away_end),))
    slots = make_one_day_slots(180)
    result = plan_upcoming_slots({}, cands, slots, 2)

    away_days = {d for d, _ in slots if away_start <= d <= away_end}
    for (d, _), picked in zip(slots, result, strict=True):
        if d in away_days:
            assert 1 not in picked

    # Afterwards the away person isn't punished with a backlog: their rate while present
    # matches everybody else's (2 of 15 per day).
    counts = Counter(uid for picked in result for uid in picked)
    present_days = 180 - len(away_days)
    assert abs(counts[1] - present_days * 2 / 15) <= 1.5


def test_done_counts_stay_even_with_random_claimer():
    rng = random.Random(42)
    _, _, doers = simulate_days(make_candidates(15), 365, claimer=rng.choice)
    counts = Counter(doers)
    assert len(counts) == 15
    assert max(counts.values()) - min(counts.values()) <= 1


def test_lazy_person_is_picked_more_until_they_clean():
    lazy = 1

    def pick_claimer(picked):
        others = [p for p in picked if p != lazy]
        return others[0] if others else picked[0]

    _, picks, doers = simulate_days(make_candidates(15), 120, claimer=pick_claimer)
    times_picked = Counter(uid for p in picks for uid in p)
    # Never cleaning means never getting credit, so they stay on the list every day.
    assert times_picked[lazy] == 120
    hard_workers = Counter(d for d in doers if d != lazy)
    assert max(hard_workers.values()) - min(hard_workers.values()) <= 1


def test_missed_duty_comes_up_again_soon():
    cands = make_candidates(15)
    miss_day = START + timedelta(days=20)
    _, picks, _ = simulate_days(cands, 23, miss_on=miss_day)
    missers = set(picks[20])
    assert missers <= set(picks[21]) | set(picks[22])


def test_new_member_does_not_get_backlog():
    cands = make_candidates(14)
    balances, _, _ = simulate_days(cands, 70)
    newbie = Candidate(id=99, room="A4.15")
    _, picks, _ = simulate_days(cands + [newbie], 15, START + timedelta(days=70), balances=balances)
    assert sum(99 in p for p in picks) <= 3


def test_room_natural_sort():
    assert sorted(["A4.10", "A4.2", "A4.1"], key=get_room_sort_key) == ["A4.1", "A4.2", "A4.10"]


def test_fewer_people_than_needed():
    result = plan_upcoming_slots({}, make_candidates(1), make_one_day_slots(3), 2)
    assert result == [[1], [1], [1]]


def test_open_slot_people_become_roles():
    cands = make_candidates(15)
    balances, _, _ = simulate_days(cands, 30)
    today = START + timedelta(days=30)
    [pair] = plan_upcoming_slots(balances, cands, [(today, today)], 2)
    accrue_expected_work(balances, [c.id for c in cands])

    result = plan_upcoming_slots(
        balances, cands, make_one_day_slots(30, today + timedelta(days=1)), 2, [(today, pair)]
    )
    shown = [u for picked in result for u in picked]
    assert not set(pair) & set(shown)
    skipped, did = Role(today, False), Role(today, True)
    # Without the credit you're up again much sooner than with it.
    assert skipped in shown and did in shown
    assert shown.index(skipped) < shown.index(did)
    assert shown.count(skipped) == shown.count(did) == 1


def test_fresh_start_non_cleaner_is_next():
    cands = make_candidates(15)
    balances = {}
    accrue_expected_work(balances, [c.id for c in cands])
    result = plan_upcoming_slots(
        balances, cands, make_one_day_slots(14, START + timedelta(days=1)), 2, [(START, [1, 2])]
    )
    assert Role(START, False) in result[0]
    # The one who cleans owes the least, so they're not up again within two weeks.
    assert all(Role(START, True) not in picked for picked in result)


def test_being_picked_without_cleaning_does_not_move_you_back():
    cands = make_candidates(4)
    balances = {}
    for _ in range(2):
        accrue_expected_work(balances, [c.id for c in cands])
    # Day 1: 1 and 2 on duty, 1 cleans. Day 2: 3 and 4, 3 cleans.
    credit_done_job(balances, 1, START)
    credit_done_job(balances, 3, START + timedelta(days=1))
    # 2 and 4 owe the same; 2 wasn't pushed back by being on duty first.
    [picked] = plan_upcoming_slots(
        balances, cands, make_one_day_slots(1, START + timedelta(days=2)), 1
    )
    assert picked == [2]


def test_single_person_open_slot_gets_no_roles():
    result = plan_upcoming_slots({}, make_candidates(3), make_one_day_slots(2), 1, [(START, [1])])
    assert all(isinstance(u, int) for picked in result for u in picked)
