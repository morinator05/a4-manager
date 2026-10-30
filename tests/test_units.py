"""Small helpers that need no database."""

import pytest

from app.models import User
from app.services import users
from app.services.login_throttle import LoginThrottle
from app.services.users import UserError


def test_display_name_uses_first_name_and_room():
    assert User(name="Anna Müller", room_number="A401").display_name == "Anna (A401)"
    assert User(name="Ben", room_number="A402").display_name == "Ben (A402)"


def test_room_numbers():
    assert users.parse_room_number(" a403 ") == "A403"
    for valid in ["A401", "A499", "B110"]:
        assert users.parse_room_number(valid) == valid
    for invalid in ["A40", "A400", "A.421", "A4.1", "A4100", "4403", "AA403", ""]:
        with pytest.raises(UserError):
            users.parse_room_number(invalid)


def test_login_throttle_window():
    now = [0.0]
    throttle = LoginThrottle(max_failures=2, window_seconds=60, clock=lambda: now[0])
    throttle.record_failure("k")
    throttle.record_failure("k")
    assert throttle.is_blocked("k")
    now[0] = 61
    assert not throttle.is_blocked("k")
    throttle.record_failure("k")
    throttle.reset_failures("k")
    assert not throttle.is_blocked("k")
