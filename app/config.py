"""App settings, read from environment variables or `.env` (names are case-insensitive)."""

from datetime import date, datetime
from functools import cached_property
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://a4:a4@localhost:5432/a4"
    # Signs the login cookie: whoever knows it can log in as anyone. No usable default on purpose.
    secret_key: str = ""
    timezone: str = "Europe/Berlin"
    # A duty must be marked done by this hour, `deadline_days_after` days after it ended.
    deadline_hour: int = 12
    deadline_days_after: int = 1
    # People put on each slot; only one of them has to do it.
    kitchen_people_per_day: int = 2
    trash_people_per_week: int = 1
    # Active strikes that turn into a penalty Mülldienst week.
    strike_threshold: int = 3
    # How far ahead the dashboard preview looks.
    preview_days: int = 14
    # Off in tests, so no background job touches the test database.
    run_scheduler: bool = True
    # Optional first Flursprecher, created on startup while there are no users at all.
    admin_username: str = ""
    admin_password: str = ""
    admin_name: str = ""
    admin_room: str = ""

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_random(cls, value: str) -> str:
        """`change-me…` is the placeholder from `.env.example`."""
        if len(value) < 32 or value.startswith("change-me"):
            raise ValueError(
                "SECRET_KEY must be a random string of at least 32 characters, "
                "e.g. from `openssl rand -hex 32`"
            )
        return value

    @cached_property
    def tz(self) -> ZoneInfo:
        """All dates and deadlines are in this zone, not the server's."""
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()


settings = Settings()
