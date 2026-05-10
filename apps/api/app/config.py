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

    # ------------------------------------------------------------------
    # Redis Streams (event producers)
    # ------------------------------------------------------------------
    # task.created events (Phase 8.3 / 8.4 pipeline). Pre-existing.
    EVENTS_TASK_CREATED_STREAM: str = "app.events.task_created"

    # published_answer.withdrawal_requested events (Phase 8.5 — Block 4A-1).
    # The API publishes here when a client POSTs a withdrawal request; the
    # worker (apps/worker/app/main.py) consumes from the same stream name.
    # Default mirrors apps/worker/app/config.py to keep producer/consumer
    # in lockstep without requiring env-var overrides in dev.
    EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM: str = (
        "app.events.published_answer_withdrawal_requested"
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
