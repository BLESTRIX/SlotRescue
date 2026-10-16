from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://slotrescue:slotrescue@127.0.0.1:5432/slotrescue"
    redis_url: str = "redis://localhost:6379/0"
    business_timezone: str = "Asia/Karachi"
    offer_lifetime_seconds: int = 900
    cookie_secure: bool = False
    base_url: str = "http://localhost:8000"
    secret_key: str = "development-only-change-before-production-32-characters"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mail_from: str = "appointments@slotrescue.test"
    session_hours: int = 168
    job_lease_seconds: int = 120
    job_max_attempts: int = 5

    @field_validator("business_timezone")
    @classmethod
    def valid_zone(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def safe_settings(self):
        if min(self.offer_lifetime_seconds, self.session_hours, self.job_lease_seconds) < 1:
            raise ValueError("Lifetimes must be positive")
        if self.app_env == "production":
            if self.secret_key.startswith("development-") or len(self.secret_key) < 32:
                raise ValueError("Set a random SECRET_KEY of at least 32 characters in production")
            if not self.cookie_secure or not self.base_url.startswith("https://"):
                raise ValueError("Production requires HTTPS BASE_URL and COOKIE_SECURE=true")
        return self


@lru_cache
def settings():
    return Settings()
