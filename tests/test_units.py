"""Small helpers that need no database."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import User
from app.services import users
from app.services.login_throttle import LoginThrottle
from app.services.users import UserError
from app.web import get_safe_next


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


@pytest.mark.parametrize(
    "url",
    [None, "", "https://evil.com", "//evil.com", "/\\evil.com", "\\\\evil.com", "evil.com"],
)
def test_unsafe_next_goes_home(url):
    assert get_safe_next(url) == "/"


@pytest.mark.parametrize("url", ["/", "/history", "/history?until=2026-11-02", "/me#strikes"])
def test_safe_next_is_kept(url):
    assert get_safe_next(url) == url


def test_short_secret_key_is_refused():
    with pytest.raises(ValidationError):
        Settings(secret_key="too-short")
    with pytest.raises(ValidationError):
        Settings(secret_key="change-me-to-a-long-random-string-0123456789")


def test_settings_out_of_range_are_refused():
    key = "x" * 32
    with pytest.raises(ValidationError):
        Settings(secret_key=key, deadline_hour=24)
    with pytest.raises(ValidationError):
        Settings(secret_key=key, kitchen_people_per_day=0)
