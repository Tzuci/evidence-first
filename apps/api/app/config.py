"""Application settings via pydantic-settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    STORAGE_LOCAL_ROOT: str = "/storage"
    LOG_LEVEL: str = "info"
    MAX_COST_PER_TASK: float = 0.0
    PROVIDERS_ENABLED: str = "mock"

    # Dev defaults: who acts when there is no real auth in MVP-0.
    DEV_TENANT_SLUG: str = "dev"
    DEV_USER_EMAIL: str = "dev@local"
    DEV_PROJECT_NAME: str = "default"

    # Redis stream where task lifecycle events are published.
    EVENTS_TASK_CREATED_STREAM: str = "app.events.task_created"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings