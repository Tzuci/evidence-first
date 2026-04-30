"""Re-export of the audit_append helper from packages/shared.

Kept as a thin compatibility shim so existing imports
(`from app.services.audit import audit_append`) continue to work.
The single source of truth is evidencefirst_shared.db.audit.
"""
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