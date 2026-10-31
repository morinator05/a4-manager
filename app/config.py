"""App settings, read from environment variables or `.env` (names are case-insensitive)."""

from datetime import date, datetime
from functools import cached_property
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings; each field is read from the environment variable of the same name."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://a4:a4@localhost:5432/a4"
    # Signs the login cookie: whoever knows it can log in as anyone. No usable default on purpose.
    secret_key: str = ""
    timezone: str = "Europe/Berlin"
    # A duty must be marked done by this hour, `deadline_days_after` days after it ended.
    deadline_hour: int = Field(12, ge=0, le=23)
    deadline_days_after: int = Field(1, ge=0)
    # People put on each slot; only one of them has to do it.
    kitchen_people_per_day: int = Field(2, ge=1)
    trash_people_per_week: int = Field(1, ge=1)
    # Active strikes that turn into a penalty Mülldienst week.
    strike_threshold: int = Field(3, ge=1)
    # How far ahead the dashboard preview looks.
    preview_days: int = Field(14, ge=1)
    # Send the login cookie over HTTPS only; turn on when the app is served behind TLS.
    session_https_only: bool = False
    # Off in tests, so no background job touches the test database.
    run_scheduler: bool = True
    # Optional first Flursprecher, created on startup while there are no users at all.
    admin_username: str = ""
    admin_password: str = ""
    admin_name: str = ""
    admin_room: str = ""

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        """Refuse a short key, or the `change-me…` placeholder from `.env.example`."""
        if len(value) < 32 or value.startswith("change-me"):
            raise ValueError(
                "SECRET_KEY must be a random string of at least 32 characters, "
                "e.g. from `openssl rand -hex 32`"
            )
        return value

    @cached_property
    def tz(self) -> ZoneInfo:
        """The app's time zone: all dates and deadlines are in it, not the server's."""
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """The current time in the app's time zone."""
        return datetime.now(self.tz)

    def today(self) -> date:
        """Today's date in the app's time zone, not the server's."""
        return self.now().date()


settings = Settings()
