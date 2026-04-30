"""Re-export of the audit_append helper from packages/shared."""
from evidencefirst_shared.db.audit import (  # noqa: F401
    GLOBAL_SCOPE_ID,
    audit_append,
    verify_audit_chain,
    verify_task_audit_chain,
)

__all__ = [
    "GLOBAL_SCOPE_ID",
    "audit_append",
    "verify_audit_chain",
    "verify_task_audit_chain",
]