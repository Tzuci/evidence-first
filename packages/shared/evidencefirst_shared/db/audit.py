"""Audit chain append + verification (single source of truth).

Provides:
- audit_append(...) -> uuid.UUID
- verify_audit_chain(conn, chain_scope, scope_id) -> dict
- verify_task_audit_chain(conn, task_id) -> dict (convenience for chain_scope='task')

Contract:
- Caller passes an active SQLAlchemy Connection inside an explicit transaction.
- Caller chooses chain_scope ('task' | 'project' | 'tenant' | 'global').
- audit_scope_consistency CHECK on audit_records is enforced by:
    task    -> task_id IS NOT NULL AND scope_id = task_id
    project -> project_id IS NOT NULL AND scope_id = project_id
    tenant  -> scope_id = tenant_id
    global  -> scope_id = '00000000-0000-0000-0000-000000000000'

Payload normalization (introduced in 8.1d):
- redacted_payload may contain UUID, datetime, bytes, nested lists/dicts.
- Before computing event_hash AND before storing into JSONB, we normalize
  the payload by serializing it to a stable JSON string and re-parsing it.
  This guarantees that what we hash is byte-identical to what we store, so
  verify_audit_chain can recompute the hash deterministically from the row.
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..canonical_json import canonical_sha256


GLOBAL_SCOPE_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_scope_id(
    chain_scope: str,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
) -> uuid.UUID:
    if chain_scope == "task":
        if task_id is None:
            raise ValueError("audit_append: chain_scope='task' requires task_id")
        return task_id
    if chain_scope == "project":
        if project_id is None:
            raise ValueError("audit_append: chain_scope='project' requires project_id")
        return project_id
    if chain_scope == "tenant":
        return tenant_id
    if chain_scope == "global":
        return GLOBAL_SCOPE_ID
    raise ValueError(f"audit_append: invalid chain_scope {chain_scope!r}")


def _payload_default(o: Any) -> Any:
    """JSON encoder default for non-primitive values commonly used in payloads."""
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, _dt.datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=_dt.timezone.utc)
        return o.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, _dt.date):
        return o.isoformat()
    raise TypeError(f"Unserializable type for redacted_payload: {type(o).__name__}")


def _payload_jsonb_dumps(payload: dict[str, Any]) -> str:
    """Serialize a payload to a stable JSON string suitable for JSONB storage.

    Stable: sort_keys=True, ensure_ascii=False. Same encoder used by the
    normalization step, so the round trip dumps -> loads is the identity.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_payload_default)


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize an arbitrary payload to a JSON-only structure (str/int/float/bool/None/list/dict).

    This is the SINGLE source of truth used both by canonicalization-for-hashing
    and by JSONB storage. Without this, a payload containing UUID/datetime/bytes
    would be canonicalized one way (raw types) and stored another way
    (post-serialization), producing an unverifiable chain.
    """
    return json.loads(_payload_jsonb_dumps(payload))


def _build_canonical_input(
    *,
    chain_scope: str,
    scope_id: uuid.UUID,
    chain_seq: int,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    event_type: str,
    actor_type: str,
    actor_id: str,
    related_entity_type: str | None,
    related_entity_id: uuid.UUID | None,
    redacted_payload_normalized: dict[str, Any],
    sensitive_payload_object_id: uuid.UUID | None,
    previous_event_hash: bytes | None,
    created_at: _dt.datetime,
) -> dict[str, Any]:
    """Build the dictionary that gets canonicalized and hashed.

    redacted_payload_normalized MUST already be a JSON-only structure (output of
    _normalize_payload). The other typed fields (UUID, datetime, bytes) are left
    as-is here because canonical_dumps in canonical_json.py already handles them
    consistently (UUID -> lowercase string, datetime -> ISO Z millis, bytes -> hex).
    """
    return {
        "chain_scope": chain_scope,
        "scope_id": scope_id,
        "chain_seq": chain_seq,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_id": task_id,
        "session_id": session_id,
        "event_type": event_type,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "related_entity_type": related_entity_type,
        "related_entity_id": related_entity_id,
        "redacted_payload": redacted_payload_normalized,
        "sensitive_payload_object_id": sensitive_payload_object_id,
        "previous_event_hash": previous_event_hash,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# audit_append
# ---------------------------------------------------------------------------
def audit_append(
    conn: Connection,
    *,
    chain_scope: Literal["task", "project", "tenant", "global"],
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    event_type: str,
    actor_type: str,
    actor_id: str,
    redacted_payload: dict[str, Any] | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    sensitive_payload_object_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Append a new audit event to the chain identified by (chain_scope, scope_id)."""
    scope_id = _resolve_scope_id(
        chain_scope, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )

    # Normalize once: this is what we hash AND what we store.
    raw_payload = redacted_payload or {}
    normalized_payload = _normalize_payload(raw_payload)
    payload_jsonb_text = json.dumps(normalized_payload, sort_keys=True, ensure_ascii=False)

    # 1) Idempotent initialization of the head.
    conn.execute(
        text(
            """
            INSERT INTO audit_chain_heads (scope, scope_id, next_seq)
            VALUES (:scope, :scope_id, 1)
            ON CONFLICT (scope, scope_id) DO NOTHING
            """
        ),
        {"scope": chain_scope, "scope_id": scope_id},
    )

    # 2) Lock the head for serialized append.
    head = conn.execute(
        text(
            """
            SELECT next_seq, last_event_id, last_event_hash
            FROM audit_chain_heads
            WHERE scope = :scope AND scope_id = :scope_id
            FOR UPDATE
            """
        ),
        {"scope": chain_scope, "scope_id": scope_id},
    ).one()

    next_seq = int(head[0])
    prev_event_id = head[1]
    prev_event_hash_bytes: bytes | None = bytes(head[2]) if head[2] is not None else None

    created_at = _dt.datetime.now(tz=_dt.timezone.utc)

    canonical_input = _build_canonical_input(
        chain_scope=chain_scope,
        scope_id=scope_id,
        chain_seq=next_seq,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        session_id=session_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        redacted_payload_normalized=normalized_payload,
        sensitive_payload_object_id=sensitive_payload_object_id,
        previous_event_hash=prev_event_hash_bytes,
        created_at=created_at,
    )
    event_hash = bytes.fromhex(canonical_sha256(canonical_input))

    new_id = uuid.uuid4()

    conn.execute(
        text(
            """
            INSERT INTO audit_records (
                id, tenant_id, project_id, session_id, task_id,
                chain_scope, scope_id, chain_seq,
                previous_event_id, previous_event_hash, event_hash,
                event_type, actor_type, actor_id,
                related_entity_type, related_entity_id,
                redacted_payload, sensitive_payload_object_id,
                created_at
            ) VALUES (
                :id, :tenant_id, :project_id, :session_id, :task_id,
                :chain_scope, :scope_id, :chain_seq,
                :prev_id, :prev_hash, :event_hash,
                :event_type, :actor_type, :actor_id,
                :related_entity_type, :related_entity_id,
                CAST(:redacted_payload AS JSONB), :sensitive_payload_object_id,
                :created_at
            )
            """
        ),
        {
            "id": new_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "session_id": session_id,
            "task_id": task_id,
            "chain_scope": chain_scope,
            "scope_id": scope_id,
            "chain_seq": next_seq,
            "prev_id": prev_event_id,
            "prev_hash": prev_event_hash_bytes,
            "event_hash": event_hash,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "related_entity_type": related_entity_type,
            "related_entity_id": related_entity_id,
            "redacted_payload": payload_jsonb_text,
            "sensitive_payload_object_id": sensitive_payload_object_id,
            "created_at": created_at,
        },
    )

    conn.execute(
        text(
            """
            UPDATE audit_chain_heads
            SET next_seq = :next_seq + 1,
                last_event_id = :id,
                last_event_hash = :event_hash,
                updated_at = NOW()
            WHERE scope = :scope AND scope_id = :scope_id
            """
        ),
        {
            "next_seq": next_seq,
            "id": new_id,
            "event_hash": event_hash,
            "scope": chain_scope,
            "scope_id": scope_id,
        },
    )

    return new_id


# ---------------------------------------------------------------------------
# verify_audit_chain
# ---------------------------------------------------------------------------
def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": uuid.UUID(str(row.id)),
        "tenant_id": uuid.UUID(str(row.tenant_id)),
        "project_id": uuid.UUID(str(row.project_id)) if row.project_id else None,
        "session_id": uuid.UUID(str(row.session_id)) if row.session_id else None,
        "task_id": uuid.UUID(str(row.task_id)) if row.task_id else None,
        "chain_scope": row.chain_scope,
        "scope_id": uuid.UUID(str(row.scope_id)),
        "chain_seq": int(row.chain_seq),
        "previous_event_id": uuid.UUID(str(row.previous_event_id)) if row.previous_event_id else None,
        "previous_event_hash": bytes(row.previous_event_hash) if row.previous_event_hash else None,
        "event_hash": bytes(row.event_hash),
        "event_type": row.event_type,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "related_entity_type": row.related_entity_type,
        "related_entity_id": uuid.UUID(str(row.related_entity_id)) if row.related_entity_id else None,
        "redacted_payload": (
            row.redacted_payload
            if isinstance(row.redacted_payload, dict)
            else (json.loads(row.redacted_payload) if row.redacted_payload else {})
        ),
        "sensitive_payload_object_id": uuid.UUID(str(row.sensitive_payload_object_id)) if row.sensitive_payload_object_id else None,
        "created_at": row.created_at,
    }


def verify_audit_chain(
    conn: Connection,
    *,
    chain_scope: Literal["task", "project", "tenant", "global"],
    scope_id: uuid.UUID,
) -> dict[str, Any]:
    """Verify integrity of the chain (chain_scope, scope_id).

    Returns:
      {"ok": bool, "checked_count": int, "discrepancies": [...]}
    """
    rows = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id, session_id, task_id,
                   chain_scope, scope_id, chain_seq,
                   previous_event_id, previous_event_hash, event_hash,
                   event_type, actor_type, actor_id,
                   related_entity_type, related_entity_id,
                   redacted_payload, sensitive_payload_object_id,
                   created_at
            FROM audit_records
            WHERE chain_scope = :scope AND scope_id = :scope_id
            ORDER BY chain_seq ASC
            """
        ),
        {"scope": chain_scope, "scope_id": scope_id},
    ).fetchall()

    discrepancies: list[dict[str, Any]] = []

    if not rows:
        return {"ok": True, "checked_count": 0, "discrepancies": []}

    expected_seq = 1
    prev_hash: bytes | None = None
    for idx, raw in enumerate(rows):
        rec = _row_to_dict(raw)

        if rec["chain_seq"] != expected_seq:
            discrepancies.append(
                {
                    "row_index": idx,
                    "chain_seq": rec["chain_seq"],
                    "kind": "chain_seq_gap",
                    "detail": f"expected {expected_seq}, got {rec['chain_seq']}",
                }
            )

        if rec["scope_id"] != scope_id:
            discrepancies.append(
                {
                    "row_index": idx,
                    "chain_seq": rec["chain_seq"],
                    "kind": "scope_id_mismatch",
                    "detail": f"expected {scope_id}, got {rec['scope_id']}",
                }
            )

        if rec["previous_event_hash"] != prev_hash:
            discrepancies.append(
                {
                    "row_index": idx,
                    "chain_seq": rec["chain_seq"],
                    "kind": "previous_event_hash_mismatch",
                    "detail": "previous_event_hash does not match prior event_hash",
                }
            )

        # The stored payload is already in JSON-only form (post normalization at
        # write time), so we feed it directly to the canonical input builder.
        canonical_input = _build_canonical_input(
            chain_scope=rec["chain_scope"],
            scope_id=rec["scope_id"],
            chain_seq=rec["chain_seq"],
            tenant_id=rec["tenant_id"],
            project_id=rec["project_id"],
            task_id=rec["task_id"],
            session_id=rec["session_id"],
            event_type=rec["event_type"],
            actor_type=rec["actor_type"],
            actor_id=rec["actor_id"],
            related_entity_type=rec["related_entity_type"],
            related_entity_id=rec["related_entity_id"],
            redacted_payload_normalized=rec["redacted_payload"],
            sensitive_payload_object_id=rec["sensitive_payload_object_id"],
            previous_event_hash=rec["previous_event_hash"],
            created_at=rec["created_at"],
        )

        recomputed = bytes.fromhex(canonical_sha256(canonical_input))
        if recomputed != rec["event_hash"]:
            discrepancies.append(
                {
                    "row_index": idx,
                    "chain_seq": rec["chain_seq"],
                    "kind": "event_hash_recompute_mismatch",
                    "detail": "recomputed sha-256 does not match stored event_hash",
                }
            )

        expected_seq = rec["chain_seq"] + 1
        prev_hash = rec["event_hash"]

    return {
        "ok": len(discrepancies) == 0,
        "checked_count": len(rows),
        "discrepancies": discrepancies,
    }


def verify_task_audit_chain(conn: Connection, *, task_id: uuid.UUID) -> dict[str, Any]:
    """Convenience wrapper: verify audit chain for chain_scope='task'."""
    return verify_audit_chain(conn, chain_scope="task", scope_id=task_id)
