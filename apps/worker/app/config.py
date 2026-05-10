"""Worker settings.

Phase 8.5 — Block 3E-1: multi-stream wiring.

The worker consumes events from THREE distinct Redis streams, all sharing
the same consumer group (``CONSUMER_GROUP_NAME``) for operational simplicity.
The streams are disjoint at the application layer (different event_type
values, different downstream consumers), so a single shared group does not
introduce any cross-talk:

    - ``EVENTS_TASK_CREATED_STREAM``
        carries ``task.created`` events; routed to ``handle_task_created``.

    - ``EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM``
        carries ``published_answer.withdrawal_requested`` events; routed to
        ``handle_published_answer_withdrawal`` via the dispatcher.

    - ``EVENTS_SOURCE_LOSS_STREAM``
        carries ``source_loss.detected`` events; routed to
        ``handle_source_loss`` via the dispatcher.

Defaults are wired in code so that ``.env.example`` does NOT need to be
edited in this block. Operators who want to override the stream names in
production can simply export the corresponding env vars; Pydantic
``BaseSettings`` will pick them up case-insensitively.

Naming convention for stream defaults: ``app.events.<event_type>`` where
``<event_type>`` is the dotted form of the event_type attribute, with the
dot replaced by underscore for readability. The three stream names are
stable contracts between producer (API) and consumer (worker); changing
them requires a coordinated rollout.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    LOG_LEVEL: str = "info"
    WORKER_CONCURRENCY: int = 1
    WORKER_CONSUMER_NAME: str = "worker_1"

    # ------------------------------------------------------------------
    # Redis Streams configuration
    # ------------------------------------------------------------------
    # task.created events (8.3 / 8.4 pipeline). Pre-existing stream,
    # untouched by this block.
    EVENTS_TASK_CREATED_STREAM: str = "app.events.task_created"

    # published_answer.withdrawal_requested events (8.5 lifecycle).
    # New in Block 3E-1.
    EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM: str = (
        "app.events.published_answer_withdrawal_requested"
    )

    # source_loss.detected events (8.5 source-loss propagation).
    # New in Block 3E-1.
    EVENTS_SOURCE_LOSS_STREAM: str = "app.events.source_loss_detected"

    # Shared consumer group for ALL configured streams. Single group is
    # intentional: the streams are disjoint at the application layer
    # (different event_type values), so sharing the group keeps PEL and
    # redelivery semantics uniform without any risk of cross-talk.
    CONSUMER_GROUP_NAME: str = "worker_default"

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def event_streams(self) -> list[str]:
        """Return the ordered list of Redis streams the worker reads from.

        Order is stable but not semantically significant: ``xreadgroup``
        with multiple streams returns entries grouped by stream in the
        order requested, but each stream's own entries are returned in
        Redis-stream order. We list ``task.created`` first because it is
        the highest-volume stream in MVP-0; the lifecycle / source-loss
        streams are low-volume by design.
        """
        return [
            self.EVENTS_TASK_CREATED_STREAM,
            self.EVENTS_PUBLISHED_ANSWER_WITHDRAWAL_STREAM,
            self.EVENTS_SOURCE_LOSS_STREAM,
        ]


_settings: WorkerSettings | None = None


def get_settings() -> WorkerSettings:
    global _settings
    if _settings is None:
        _settings = WorkerSettings()
    return _settings
