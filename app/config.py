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
    deadline_hour: int = 12
    deadline_days_after: int = 1
    kitchen_people_per_day: int = 2
    trash_people_per_week: int = 1
    strike_threshold: int = 3
    preview_days: int = 14
    run_scheduler: bool = True

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_random(cls, value: str) -> str:
        if len(value) < 32 or value.startswith("change-me"):
            raise ValueError(
                "SECRET_KEY must be a random string of at least 32 characters, "
                "e.g. from `openssl rand -hex 32`"
            )
        return value

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()


settings = Settings()
