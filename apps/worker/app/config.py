"""Worker settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    LOG_LEVEL: str = "info"
    WORKER_CONCURRENCY: int = 1
    WORKER_CONSUMER_NAME: str = "worker_1"

    EVENTS_TASK_CREATED_STREAM: str = "app.events.task_created"
    CONSUMER_GROUP_NAME: str = "worker_default"


_settings: WorkerSettings | None = None


def get_settings() -> WorkerSettings:
    global _settings
    if _settings is None:
        _settings = WorkerSettings()
    return _settings