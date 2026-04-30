"""Database-side helpers shared across api and worker.

Contains the single source of truth for:
- audit chain append and verification (audit.py)
- event_processing_records idempotency helpers (idempotency.py)
"""

from .audit import (  # noqa: F401
    GLOBAL_SCOPE_ID,
    audit_append,
    verify_audit_chain,
    verify_task_audit_chain,
)
from .idempotency import (  # noqa: F401
    begin_processing,
    mark_succeeded,
    mark_failed,
    increment_attempt,
)

__all__ = [
    "GLOBAL_SCOPE_ID",
    "audit_append",
    "verify_audit_chain",
    "verify_task_audit_chain",
    "begin_processing",
    "mark_succeeded",
    "mark_failed",
    "increment_attempt",
]