"""Source loss propagator service (Phase 8.5 — Block 2B-1).

This module implements the worker-side propagation of an existing
source_loss_events row toward:

  - the Claim Ledger (claim_ledger_entries, append-only): every logical_claim
    transitively supported by the lost evidence_span gets a NEW ledger entry
    in state 'unverifiable' / support_scope 'unsupported' /
    user_provided_dependency 'unsupported', with transition_reason
    'source_lost';
  - claim_lineage: a 'supersedes' edge is inserted from the previous head
    entry to the new unverifiable entry;
  - source_loss_propagation_records (append-only): one row per
    (source_loss_event, propagation_kind, optional target). Idempotency for
    'recorded' / 'skipped' outcomes is enforced by the four unique partial
    indexes declared in 0006_lifecycle.sql; 'failed' attempts remain as
    append-only history without consuming the final idempotency slot;
  - audit_records (chain_scope='task'): one event per *new* ledger entry
    actually created and one event per *newly-recorded* impacted
    published_answer. No audit is emitted on conflicts (already-recorded /
    already-skipped outcomes).

What this service does NOT do (Phase 8.5 invariants):

  - It does NOT withdraw published_answers, does NOT call apply_withdrawal,
    does NOT mutate published_answers.status in any way. The withdrawal
    pipeline is governed exclusively by published_answer_lifecycle.py.
  - It does NOT insert published_answer_lifecycle_events. Only the lifecycle
    service writes to that table.
  - It does NOT extend task_masters.status, does NOT modify
    lc_block_delete_if_published, does NOT run any DB trigger to perform
    propagation. Propagation is purely application-driven.
  - It does NOT install or modify any constraint. The schema declared in
    0006_lifecycle.sql is the contract this module honors.

Granularity:

  Propagation is keyed on source_loss_events.evidence_span_id. The
  document-level columns on source_loss_events (document_chunk_id,
  document_version_id, document_id) are reporting context only and are not
  used to expand the impact set: a chunk-level loss that did not generate
  evidence_span links yields no claim impact.

Append-only invariants:

  - claim_ledger_entries: only INSERT. Re-runs detect the 'source_lost'
    head and short-circuit with a 'skipped' propagation record.
  - claim_lineage: INSERT ... ON CONFLICT DO NOTHING on the
    (parent_entry_id, child_entry_id, relation_kind) UNIQUE.
  - source_loss_propagation_records: INSERT only. ON CONFLICT inference
    against the four declared partial unique indexes.
  - audit_records: INSERT only via audit_append, gated on RETURNING id from
    the propagation record insert so a second call does not append a
    duplicate audit event.

Concurrency contract:

  Before computing the next version_no for a claim, the service acquires a
  row-level lock on the underlying logical_claims row (SELECT ... FOR
  UPDATE). This serializes concurrent appends to the ledger for the same
  claim (whether by this propagator, the verifier, or any other writer that
  honors the same lock convention) and guarantees that
  cle_logical_version_uq cannot deadlock or be violated under contention.

  The caller MUST pass an active SQLAlchemy Connection inside an explicit
  transaction (e.g. via engine.begin()). Calling this function on a
  Connection in autocommit mode would degrade the locking semantics and is
  unsupported.

Transaction model:

  This module never calls get_engine(), never opens its own connection, and
  never calls commit() or rollback(). All writes happen inside the caller's
  transaction.
"""
from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import Uuid as SAUuid

from evidencefirst_shared.db.audit import audit_append


logger = structlog.get_logger(__name__)


SERVICE_NAME = "mvp0_source_loss_propagator_v1"
SERVICE_VERSION = "0.1.0"

# claim_ledger_entries values when transitioning to 'source_lost'.
UNVERIFIABLE_STATE = "unverifiable"
UNSUPPORTED_SUPPORT_SCOPE = "unsupported"
UNSUPPORTED_USER_PROVIDED_DEPENDENCY = "unsupported"
TRANSITION_REASON_SOURCE_LOST = "source_lost"

# claim_lineage relation_kind for v(N) -> v(N+1).
LINEAGE_RELATION_SUPERSEDES = "supersedes"

# source_loss_propagation_records.propagation_kind values.
PROP_KIND_CLAIM_UNVERIFIABLE = "claim_marked_unverifiable"
PROP_KIND_PUBLISHED_ANSWER_IMPACTED = "published_answer_impacted"
PROP_KIND_NO_CLAIMS_IMPACTED = "no_claims_impacted"
PROP_KIND_NO_ACTIVE_PA_IMPACTED = "no_active_published_answers_impacted"

# source_loss_propagation_records.status values.
PROP_STATUS_RECORDED = "recorded"
PROP_STATUS_SKIPPED = "skipped"
PROP_STATUS_FAILED = "failed"

# Audit event types (chain_scope='task').
AUDIT_EVENT_PROPAGATED_TO_CLAIM = "source_loss.propagated_to_claim"
AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER = (
    "source_loss.propagated_to_published_answer"
)


# ---------------------------------------------------------------------------
# JSON serialization (mirrors published_answer_lifecycle._payload_default)
# ---------------------------------------------------------------------------
def _payload_default(o: Any) -> Any:
    """JSON encoder fallback for non-primitive payload values.

    Mirrors the convention used by evidencefirst_shared.db.audit and
    published_answer_lifecycle so that JSONB content stays uniform across
    services:

      - uuid.UUID         -> canonical lowercase string;
      - bytes / bytearray -> hex string;
      - datetime.datetime -> ISO8601 in UTC with the 'Z' suffix; naive
                             timestamps are assumed to already be in UTC;
      - datetime.date     -> ISO8601 date string.

    Any unsupported type raises TypeError, surfacing malformed payloads
    instead of silently corrupting the JSONB column.
    """
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, datetime.datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=datetime.timezone.utc)
        return o.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(o, datetime.date):
        return o.isoformat()
    raise TypeError(
        f"Object of type {type(o).__name__} is not JSON serializable "
        f"in source_loss_propagator payloads"
    )


def _serialize_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=_payload_default,
    )


# ---------------------------------------------------------------------------
# read helpers
# ---------------------------------------------------------------------------
def _select_source_loss_event(
    conn: Connection, *, source_loss_event_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the source_loss_events row as a dict or None if not found."""
    row = conn.execute(
        text(
            """
            SELECT
              id,
              tenant_id,
              project_id,
              task_id,
              evidence_span_id,
              document_chunk_id,
              document_version_id,
              document_id,
              loss_kind,
              loss_reason,
              detected_by,
              event_payload,
              idempotency_key,
              created_at
            FROM source_loss_events
            WHERE id = :sle_id
            """
        ),
        {"sle_id": source_loss_event_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _select_impacted_logical_claim_ids(
    conn: Connection, *, evidence_span_id: uuid.UUID
) -> list[uuid.UUID]:
    """Return the list of logical_claims ids whose ledger entries are linked
    to the lost evidence_span via claim_evidence_links.

    The query groups by claim_logical_id so that a single claim with multiple
    links to the same span yields a single propagation target.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT cel.claim_logical_id AS claim_logical_id
            FROM claim_evidence_links cel
            WHERE cel.evidence_span_id = :esid
            ORDER BY cel.claim_logical_id
            """
        ),
        {"esid": evidence_span_id},
    ).fetchall()
    return [uuid.UUID(str(r._mapping["claim_logical_id"])) for r in rows]


def _lock_logical_claim(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> dict[str, Any] | None:
    """Acquire a row-level lock on logical_claims and return its task scope.

    Returns the dict with tenant_id / project_id / task_id, or None if the
    row no longer exists. The lock is held until the caller's transaction
    commits or rolls back, serializing concurrent ledger appends for this
    claim.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id,
              tenant_id,
              project_id,
              task_id
            FROM logical_claims
            WHERE id = :lcid
            FOR UPDATE
            """
        ),
        {"lcid": claim_logical_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _select_latest_ledger_entry(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the latest claim_ledger_entries row for a logical claim, or None.

    Must be called AFTER _lock_logical_claim to avoid races on version_no.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id,
              version_no,
              state,
              support_scope,
              user_provided_dependency,
              transition_reason
            FROM claim_ledger_entries
            WHERE claim_logical_id = :lcid
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"lcid": claim_logical_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _select_impacted_published_answers(
    conn: Connection, *, claim_logical_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Return the published_answers rows in status='published' that depend on
    any of the given logical_claims ids, joined with task_masters to expose
    tenant_id and project_id needed for the audit append.

    Dependency chain (declared in 0005_answers_gate.sql):
        published_answers
          -> draft_final_answers
            -> final_answer_spans
              -> final_answer_span_claim_links
                -> logical_claims
    """
    if not claim_logical_ids:
        return []
    stmt = text(
        """
        SELECT
          pa.id           AS published_answer_id,
          pa.task_id      AS task_id,
          tm.tenant_id    AS tenant_id,
          tm.project_id   AS project_id,
          ARRAY_AGG(DISTINCT fascl.claim_logical_id) AS impacted_claim_logical_ids
        FROM published_answers pa
        JOIN draft_final_answers           dfa   ON dfa.id = pa.draft_final_answer_id
        JOIN final_answer_spans            fas   ON fas.draft_final_answer_id = dfa.id
        JOIN final_answer_span_claim_links fascl ON fascl.final_answer_span_id = fas.id
        JOIN task_masters                  tm    ON tm.id = pa.task_id
        WHERE pa.status = 'published'
          AND fascl.claim_logical_id IN :lcids
        GROUP BY pa.id, pa.task_id, tm.tenant_id, tm.project_id
        ORDER BY pa.id
        """
    ).bindparams(bindparam("lcids", expanding=True, type_=SAUuid()))
    rows = conn.execute(stmt, {"lcids": claim_logical_ids}).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        m = r._mapping
        impacted_raw = m["impacted_claim_logical_ids"] or []
        impacted = [uuid.UUID(str(x)) for x in impacted_raw]
        out.append(
            {
                "published_answer_id": uuid.UUID(str(m["published_answer_id"])),
                "task_id": uuid.UUID(str(m["task_id"])),
                "tenant_id": uuid.UUID(str(m["tenant_id"])),
                "project_id": uuid.UUID(str(m["project_id"])),
                "impacted_claim_logical_ids": impacted,
            }
        )
    return out


# ---------------------------------------------------------------------------
# write helpers — claim ledger
# ---------------------------------------------------------------------------
def _insert_unverifiable_ledger_entry(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    new_version_no: int,
    payload: dict[str, Any],
) -> uuid.UUID:
    """Append a new claim_ledger_entries row in state='unverifiable' /
    support_scope='unsupported' / user_provided_dependency='unsupported',
    with transition_reason='source_lost'.

    The unique constraint cle_logical_version_uq (claim_logical_id,
    version_no) protects against concurrent inserts; the FOR UPDATE on
    logical_claims acquired by the caller is what prevents the conflict in
    the first place.
    """
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO claim_ledger_entries (
                id, claim_logical_id, version_no,
                state, support_scope, user_provided_dependency,
                human_review_required, transition_reason, payload
            ) VALUES (
                :id, :lcid, :vno,
                :state, :sscope, :upd,
                FALSE, :treason, CAST(:payload AS JSONB)
            )
            """
        ),
        {
            "id": new_id,
            "lcid": claim_logical_id,
            "vno": new_version_no,
            "state": UNVERIFIABLE_STATE,
            "sscope": UNSUPPORTED_SUPPORT_SCOPE,
            "upd": UNSUPPORTED_USER_PROVIDED_DEPENDENCY,
            "treason": TRANSITION_REASON_SOURCE_LOST,
            "payload": _serialize_payload(payload),
        },
    )
    return new_id


def _insert_supersedes_lineage(
    conn: Connection,
    *,
    parent_entry_id: uuid.UUID,
    child_entry_id: uuid.UUID,
) -> None:
    """Append a 'supersedes' edge from parent (vN) to child (vN+1).

    Idempotent via ON CONFLICT DO NOTHING on the
    (parent_entry_id, child_entry_id, relation_kind) UNIQUE.
    """
    conn.execute(
        text(
            """
            INSERT INTO claim_lineage (
                id, parent_entry_id, child_entry_id, relation_kind
            ) VALUES (
                :id, :parent, :child, :kind
            )
            ON CONFLICT (parent_entry_id, child_entry_id, relation_kind)
            DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "parent": parent_entry_id,
            "child": child_entry_id,
            "kind": LINEAGE_RELATION_SUPERSEDES,
        },
    )


# ---------------------------------------------------------------------------
# write helpers — propagation records
# ---------------------------------------------------------------------------
def _insert_claim_unverifiable_record(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    old_claim_ledger_entry_id: uuid.UUID,
    new_claim_ledger_entry_id: uuid.UUID | None,
    status: str,
    details: dict[str, Any],
) -> bool:
    """Idempotent insert of a 'claim_marked_unverifiable' propagation record.

    Inference target is the partial unique index
    slpr_claim_marked_unverifiable_uq, which is restricted to
    propagation_kind = 'claim_marked_unverifiable' AND status IN
    ('recorded', 'skipped') AND claim_logical_id IS NOT NULL.

    Status values 'recorded' and 'skipped' both consume the idempotency
    slot. The caller is responsible for choosing the correct status:
      - 'recorded' when a new ledger entry was actually appended;
      - 'skipped'  when the head was already in source_lost terminal state.

    Returns True if a new row was inserted, False if a prior recorded /
    skipped row already existed for the same
    (source_loss_event_id, claim_logical_id).
    """
    inserted = conn.execute(
        text(
            """
            INSERT INTO source_loss_propagation_records (
                id, source_loss_event_id, claim_logical_id,
                old_claim_ledger_entry_id, new_claim_ledger_entry_id,
                published_answer_id, propagation_kind, status, details
            ) VALUES (
                :id, :sle, :lcid,
                :old_id, :new_id,
                NULL, :kind, :status, CAST(:details AS JSONB)
            )
            ON CONFLICT (source_loss_event_id, propagation_kind, claim_logical_id)
            WHERE propagation_kind = 'claim_marked_unverifiable'
              AND status IN ('recorded', 'skipped')
              AND claim_logical_id IS NOT NULL
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "sle": source_loss_event_id,
            "lcid": claim_logical_id,
            "old_id": old_claim_ledger_entry_id,
            "new_id": new_claim_ledger_entry_id,
            "kind": PROP_KIND_CLAIM_UNVERIFIABLE,
            "status": status,
            "details": _serialize_payload(details),
        },
    ).first()
    return inserted is not None


def _insert_claim_unverifiable_failed_record(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    details: dict[str, Any],
) -> None:
    """Append-only insert of a failed 'claim_marked_unverifiable' record.

    'failed' rows are NOT covered by the partial unique index, so they may
    legitimately accumulate as append-only history without consuming the
    idempotency slot for a future successful retry. No ON CONFLICT clause
    is needed.
    """
    conn.execute(
        text(
            """
            INSERT INTO source_loss_propagation_records (
                id, source_loss_event_id, claim_logical_id,
                old_claim_ledger_entry_id, new_claim_ledger_entry_id,
                published_answer_id, propagation_kind, status, details
            ) VALUES (
                :id, :sle, :lcid,
                NULL, NULL,
                NULL, :kind, :status, CAST(:details AS JSONB)
            )
            """
        ),
        {
            "id": uuid.uuid4(),
            "sle": source_loss_event_id,
            "lcid": claim_logical_id,
            "kind": PROP_KIND_CLAIM_UNVERIFIABLE,
            "status": PROP_STATUS_FAILED,
            "details": _serialize_payload(details),
        },
    )


def _insert_published_answer_impacted_record(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    published_answer_id: uuid.UUID,
    details: dict[str, Any],
) -> bool:
    """Idempotent insert of a 'published_answer_impacted' propagation record.

    Inference target is the partial unique index
    slpr_published_answer_impacted_uq, which is restricted to
    propagation_kind = 'published_answer_impacted' AND status IN
    ('recorded', 'skipped') AND published_answer_id IS NOT NULL.

    Returns True if a new row was inserted, False if one already exists for
    the same (source_loss_event_id, published_answer_id).
    """
    inserted = conn.execute(
        text(
            """
            INSERT INTO source_loss_propagation_records (
                id, source_loss_event_id, claim_logical_id,
                old_claim_ledger_entry_id, new_claim_ledger_entry_id,
                published_answer_id, propagation_kind, status, details
            ) VALUES (
                :id, :sle, NULL,
                NULL, NULL,
                :pa_id, :kind, :status, CAST(:details AS JSONB)
            )
            ON CONFLICT (source_loss_event_id, propagation_kind, published_answer_id)
            WHERE propagation_kind = 'published_answer_impacted'
              AND status IN ('recorded', 'skipped')
              AND published_answer_id IS NOT NULL
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "sle": source_loss_event_id,
            "pa_id": published_answer_id,
            "kind": PROP_KIND_PUBLISHED_ANSWER_IMPACTED,
            "status": PROP_STATUS_RECORDED,
            "details": _serialize_payload(details),
        },
    ).first()
    return inserted is not None


def _insert_no_claims_impacted_record(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    details: dict[str, Any],
) -> bool:
    """Idempotent insert of a 'no_claims_impacted' propagation record.

    Inference target is the partial unique index slpr_no_claims_impacted_uq.
    Returns True on first insert, False on conflict.
    """
    inserted = conn.execute(
        text(
            """
            INSERT INTO source_loss_propagation_records (
                id, source_loss_event_id, claim_logical_id,
                old_claim_ledger_entry_id, new_claim_ledger_entry_id,
                published_answer_id, propagation_kind, status, details
            ) VALUES (
                :id, :sle, NULL,
                NULL, NULL,
                NULL, :kind, :status, CAST(:details AS JSONB)
            )
            ON CONFLICT (source_loss_event_id, propagation_kind)
            WHERE propagation_kind = 'no_claims_impacted'
              AND status IN ('recorded', 'skipped')
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "sle": source_loss_event_id,
            "kind": PROP_KIND_NO_CLAIMS_IMPACTED,
            "status": PROP_STATUS_RECORDED,
            "details": _serialize_payload(details),
        },
    ).first()
    return inserted is not None


def _insert_no_active_pa_impacted_record(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    details: dict[str, Any],
) -> bool:
    """Idempotent insert of a 'no_active_published_answers_impacted' record.

    Inference target is the partial unique index
    slpr_no_active_published_answers_impacted_uq.
    """
    inserted = conn.execute(
        text(
            """
            INSERT INTO source_loss_propagation_records (
                id, source_loss_event_id, claim_logical_id,
                old_claim_ledger_entry_id, new_claim_ledger_entry_id,
                published_answer_id, propagation_kind, status, details
            ) VALUES (
                :id, :sle, NULL,
                NULL, NULL,
                NULL, :kind, :status, CAST(:details AS JSONB)
            )
            ON CONFLICT (source_loss_event_id, propagation_kind)
            WHERE propagation_kind = 'no_active_published_answers_impacted'
              AND status IN ('recorded', 'skipped')
            DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": uuid.uuid4(),
            "sle": source_loss_event_id,
            "kind": PROP_KIND_NO_ACTIVE_PA_IMPACTED,
            "status": PROP_STATUS_RECORDED,
            "details": _serialize_payload(details),
        },
    ).first()
    return inserted is not None


# ---------------------------------------------------------------------------
# per-claim handler
# ---------------------------------------------------------------------------
def _propagate_to_single_claim(
    conn: Connection,
    *,
    sle: dict[str, Any],
    claim_logical_id: uuid.UUID,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Propagate a source loss to a single impacted logical claim.

    Returns a dict describing the outcome:
        {
          "claim_logical_id": uuid,
          "outcome": "created" | "skipped" | "failed",
          "new_claim_ledger_entry_id": uuid | None,
          "old_claim_ledger_entry_id": uuid | None,
        }

    Outcomes:
      - "created":  a new unverifiable ledger entry was appended, the
                    supersedes lineage was inserted (or already present),
                    a 'recorded' propagation record was inserted (or
                    already present), and an audit event was emitted (only
                    when the propagation record was actually inserted).
      - "skipped":  the latest ledger entry is already 'unverifiable' with
                    transition_reason='source_lost'; no new ledger entry is
                    appended; a 'skipped' propagation record is inserted
                    idempotently; no audit event is emitted.
      - "failed":   no claim_ledger_entries row exists for this logical
                    claim — a malformed corpus state. A 'failed' propagation
                    record is appended; no audit event is emitted.
    """
    source_loss_event_id = uuid.UUID(str(sle["id"]))
    evidence_span_id = uuid.UUID(str(sle["evidence_span_id"]))
    loss_kind = str(sle["loss_kind"])
    loss_reason = str(sle["loss_reason"])

    # 1) Lock logical_claims to serialize ledger appends for this claim.
    locked = _lock_logical_claim(conn, claim_logical_id=claim_logical_id)
    if locked is None:
        # logical_claims row vanished between the impact query and the lock.
        # claim_evidence_links has ON DELETE RESTRICT to logical_claims, so
        # this is essentially impossible without external interference, but
        # we guard defensively.
        details = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "claim_logical_id": str(claim_logical_id),
            "reason": "missing_logical_claim",
        }
        if idempotency_key is not None:
            details["call_idempotency_key"] = idempotency_key
        _insert_claim_unverifiable_failed_record(
            conn,
            source_loss_event_id=source_loss_event_id,
            claim_logical_id=claim_logical_id,
            details=details,
        )
        return {
            "claim_logical_id": claim_logical_id,
            "outcome": "failed",
            "new_claim_ledger_entry_id": None,
            "old_claim_ledger_entry_id": None,
        }

    claim_tenant_id = uuid.UUID(str(locked["tenant_id"]))
    claim_project_id = uuid.UUID(str(locked["project_id"]))
    claim_task_id = uuid.UUID(str(locked["task_id"]))

    # 2) Fetch the latest entry for the claim.
    latest = _select_latest_ledger_entry(conn, claim_logical_id=claim_logical_id)
    if latest is None:
        # Defensive: a logical_claims row without any claim_ledger_entries is
        # an anomaly. Record a failed propagation row and move on; no audit.
        details = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "claim_logical_id": str(claim_logical_id),
            "reason": "missing_claim_ledger_entry",
        }
        if idempotency_key is not None:
            details["call_idempotency_key"] = idempotency_key
        _insert_claim_unverifiable_failed_record(
            conn,
            source_loss_event_id=source_loss_event_id,
            claim_logical_id=claim_logical_id,
            details=details,
        )
        logger.warning(
            "source_loss_propagator.missing_claim_ledger_entry",
            source_loss_event_id=str(source_loss_event_id),
            claim_logical_id=str(claim_logical_id),
        )
        return {
            "claim_logical_id": claim_logical_id,
            "outcome": "failed",
            "new_claim_ledger_entry_id": None,
            "old_claim_ledger_entry_id": None,
        }

    latest_id = uuid.UUID(str(latest["id"]))
    latest_version_no = int(latest["version_no"])
    latest_state = str(latest["state"])
    latest_transition_reason = (
        str(latest["transition_reason"]) if latest["transition_reason"] is not None else None
    )

    # 3) Already in source_lost terminal head: skip (no new ledger entry).
    if (
        latest_state == UNVERIFIABLE_STATE
        and latest_transition_reason == TRANSITION_REASON_SOURCE_LOST
    ):
        skip_details = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "claim_logical_id": str(claim_logical_id),
            "reason": "already_unverifiable_source_lost",
            "previous_entry_id": str(latest_id),
            "previous_state": latest_state,
            "previous_version_no": latest_version_no,
            "loss_kind": loss_kind,
            "loss_reason": loss_reason,
        }
        if idempotency_key is not None:
            skip_details["call_idempotency_key"] = idempotency_key
        _insert_claim_unverifiable_record(
            conn,
            source_loss_event_id=source_loss_event_id,
            claim_logical_id=claim_logical_id,
            old_claim_ledger_entry_id=latest_id,
            new_claim_ledger_entry_id=None,
            status=PROP_STATUS_SKIPPED,
            details=skip_details,
        )
        return {
            "claim_logical_id": claim_logical_id,
            "outcome": "skipped",
            "new_claim_ledger_entry_id": None,
            "old_claim_ledger_entry_id": latest_id,
        }

    # 4) Append a new unverifiable ledger entry vN+1.
    new_version_no = latest_version_no + 1
    ledger_payload = {
        "service_name": SERVICE_NAME,
        "service_version": SERVICE_VERSION,
        "source_loss_event_id": str(source_loss_event_id),
        "evidence_span_id": str(evidence_span_id),
        "previous_entry_id": str(latest_id),
        "previous_state": latest_state,
        "loss_kind": loss_kind,
        "loss_reason": loss_reason,
    }
    new_entry_id = _insert_unverifiable_ledger_entry(
        conn,
        claim_logical_id=claim_logical_id,
        new_version_no=new_version_no,
        payload=ledger_payload,
    )

    # 5) Lineage edge vN -> vN+1 ('supersedes'), idempotent.
    _insert_supersedes_lineage(
        conn,
        parent_entry_id=latest_id,
        child_entry_id=new_entry_id,
    )

    # 6) Idempotent recorded propagation row. We only emit audit when the
    #    INSERT actually creates a new propagation row (RETURNING id).
    rec_details = {
        "service_name": SERVICE_NAME,
        "service_version": SERVICE_VERSION,
        "source_loss_event_id": str(source_loss_event_id),
        "evidence_span_id": str(evidence_span_id),
        "claim_logical_id": str(claim_logical_id),
        "previous_entry_id": str(latest_id),
        "previous_state": latest_state,
        "previous_version_no": latest_version_no,
        "new_entry_id": str(new_entry_id),
        "new_version_no": new_version_no,
        "loss_kind": loss_kind,
        "loss_reason": loss_reason,
    }
    if idempotency_key is not None:
        rec_details["call_idempotency_key"] = idempotency_key

    record_inserted = _insert_claim_unverifiable_record(
        conn,
        source_loss_event_id=source_loss_event_id,
        claim_logical_id=claim_logical_id,
        old_claim_ledger_entry_id=latest_id,
        new_claim_ledger_entry_id=new_entry_id,
        status=PROP_STATUS_RECORDED,
        details=rec_details,
    )

    # 7) Audit only if a propagation record was actually inserted on this
    #    call. This pairs the audit emission with the unique partial index
    #    on the propagation record so a redelivery cannot double-audit.
    if record_inserted:
        audit_payload: dict[str, Any] = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "claim_logical_id": str(claim_logical_id),
            "previous_entry_id": str(latest_id),
            "previous_state": latest_state,
            "previous_version_no": latest_version_no,
            "new_entry_id": str(new_entry_id),
            "new_version_no": new_version_no,
            "new_state": UNVERIFIABLE_STATE,
            "transition_reason": TRANSITION_REASON_SOURCE_LOST,
            "loss_kind": loss_kind,
            "loss_reason": loss_reason,
        }
        if idempotency_key is not None:
            audit_payload["call_idempotency_key"] = idempotency_key
        audit_append(
            conn,
            chain_scope="task",
            tenant_id=claim_tenant_id,
            project_id=claim_project_id,
            task_id=claim_task_id,
            session_id=None,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
            actor_type="system",
            actor_id="system",
            redacted_payload=audit_payload,
            related_entity_type="logical_claims",
            related_entity_id=claim_logical_id,
        )

    return {
        "claim_logical_id": claim_logical_id,
        "outcome": "created",
        "new_claim_ledger_entry_id": new_entry_id,
        "old_claim_ledger_entry_id": latest_id,
    }


# ---------------------------------------------------------------------------
# per-published-answer handler
# ---------------------------------------------------------------------------
def _propagate_to_single_published_answer(
    conn: Connection,
    *,
    sle: dict[str, Any],
    pa: dict[str, Any],
    idempotency_key: str | None,
) -> bool:
    """Record impact on a single published_answer and emit audit on first record.

    Does NOT modify the published_answers row in any way (no status change,
    no withdrawn_at, no superseded_at). The withdrawal pipeline lives in
    published_answer_lifecycle.py and is intentionally not chained from
    here.

    Returns True if the propagation record was newly inserted on this call
    (and therefore an audit was emitted), False on conflict (already
    recorded by a prior invocation).
    """
    source_loss_event_id = uuid.UUID(str(sle["id"]))
    evidence_span_id = uuid.UUID(str(sle["evidence_span_id"]))
    loss_kind = str(sle["loss_kind"])
    loss_reason = str(sle["loss_reason"])

    pa_id: uuid.UUID = pa["published_answer_id"]
    pa_task_id: uuid.UUID = pa["task_id"]
    pa_tenant_id: uuid.UUID = pa["tenant_id"]
    pa_project_id: uuid.UUID = pa["project_id"]
    impacted_claim_logical_ids: list[uuid.UUID] = pa["impacted_claim_logical_ids"]

    details = {
        "service_name": SERVICE_NAME,
        "service_version": SERVICE_VERSION,
        "source_loss_event_id": str(source_loss_event_id),
        "evidence_span_id": str(evidence_span_id),
        "published_answer_id": str(pa_id),
        "task_id": str(pa_task_id),
        "impacted_claim_logical_ids": [str(x) for x in impacted_claim_logical_ids],
        "loss_kind": loss_kind,
        "loss_reason": loss_reason,
    }
    if idempotency_key is not None:
        details["call_idempotency_key"] = idempotency_key

    record_inserted = _insert_published_answer_impacted_record(
        conn,
        source_loss_event_id=source_loss_event_id,
        published_answer_id=pa_id,
        details=details,
    )

    if record_inserted:
        audit_payload: dict[str, Any] = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "published_answer_id": str(pa_id),
            "task_id": str(pa_task_id),
            "impacted_claim_logical_ids": [str(x) for x in impacted_claim_logical_ids],
            "loss_kind": loss_kind,
            "loss_reason": loss_reason,
        }
        if idempotency_key is not None:
            audit_payload["call_idempotency_key"] = idempotency_key
        audit_append(
            conn,
            chain_scope="task",
            tenant_id=pa_tenant_id,
            project_id=pa_project_id,
            task_id=pa_task_id,
            session_id=None,
            event_type=AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER,
            actor_type="system",
            actor_id="system",
            redacted_payload=audit_payload,
            related_entity_type="published_answers",
            related_entity_id=pa_id,
        )

    return record_inserted


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------
def propagate_source_loss(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Propagate an existing source_loss_events row through the system.

    Steps:
      1. Load the source_loss_events row. If it does not exist, return
         status='not_found'.
      2. Compute the impact set on logical_claims via claim_evidence_links
         on the lost evidence_span_id.
      3. If empty, idempotently record a 'no_claims_impacted' propagation
         record and return status='no_claims_impacted'.
      4. Otherwise, for each impacted claim:
           - lock logical_claims FOR UPDATE;
           - read the latest claim_ledger_entries head;
           - if missing -> failed propagation record (no audit);
           - if already 'unverifiable' / 'source_lost' -> skipped
             propagation record (no audit, no new ledger entry);
           - else append a new vN+1 unverifiable ledger entry, append the
             'supersedes' lineage edge, append a recorded propagation
             record idempotently, and emit a task-scoped audit event ONLY
             when the propagation record was actually inserted on this
             call.
      5. Compute the set of published_answers in status='published' that
         depend on any impacted claim. For each one, record a
         'published_answer_impacted' propagation row idempotently and emit
         an audit event ONLY when the row was actually inserted on this
         call. The published_answer itself is NEVER modified.
      6. If claims were impacted but no active published_answer was, record
         a 'no_active_published_answers_impacted' propagation row
         idempotently.
      7. Return a summary dict with status='propagated' and counters.

    Returned dict shape:
        {
          "status": "not_found" | "no_claims_impacted" | "propagated",
          "source_loss_event_id": str,
          "evidence_span_id": str | None,            # absent on not_found
          "impacted_claim_count": int,
          "created_unverifiable_count": int,
          "skipped_claim_count": int,
          "failed_claim_count": int,
          "impacted_published_answer_count": int,
          "newly_recorded_published_answer_count": int,
        }

    Idempotency contract:
      Calling this function twice with the same source_loss_event_id MUST
      be safe. The schema-level partial unique indexes guarantee that
      'recorded' / 'skipped' propagation rows are not duplicated; the
      claim_ledger_entries skip branch and the per-call audit gating
      guarantee that no duplicate ledger entry, lineage edge, or audit
      event is produced. The optional `idempotency_key` parameter is
      threaded into propagation/audit payloads for traceability only — it
      is not used as the schema-level idempotency token (the source_loss
      event id plays that role).

    Transaction model:
      The caller passes a Connection inside an explicit transaction. This
      function never opens its own connection, never commits, and never
      rolls back.
    """
    # 1) Load the source_loss_events row.
    sle = _select_source_loss_event(
        conn, source_loss_event_id=source_loss_event_id
    )
    if sle is None:
        logger.info(
            "source_loss_propagator.not_found",
            source_loss_event_id=str(source_loss_event_id),
        )
        return {
            "status": "not_found",
            "source_loss_event_id": str(source_loss_event_id),
        }

    evidence_span_id = uuid.UUID(str(sle["evidence_span_id"]))

    # 2) Compute claim impact set.
    impacted_claim_logical_ids = _select_impacted_logical_claim_ids(
        conn, evidence_span_id=evidence_span_id
    )

    # 3) No claims impacted: record the dedicated propagation kind and exit.
    if not impacted_claim_logical_ids:
        no_claims_details = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "reason": "no_claim_evidence_links",
            "loss_kind": str(sle["loss_kind"]),
            "loss_reason": str(sle["loss_reason"]),
        }
        if idempotency_key is not None:
            no_claims_details["call_idempotency_key"] = idempotency_key
        _insert_no_claims_impacted_record(
            conn,
            source_loss_event_id=source_loss_event_id,
            details=no_claims_details,
        )
        logger.info(
            "source_loss_propagator.no_claims_impacted",
            source_loss_event_id=str(source_loss_event_id),
            evidence_span_id=str(evidence_span_id),
        )
        return {
            "status": "no_claims_impacted",
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "impacted_claim_count": 0,
            "created_unverifiable_count": 0,
            "skipped_claim_count": 0,
            "failed_claim_count": 0,
            "impacted_published_answer_count": 0,
            "newly_recorded_published_answer_count": 0,
        }

    # 4) Per-claim propagation.
    created_unverifiable_count = 0
    skipped_claim_count = 0
    failed_claim_count = 0

    for claim_logical_id in impacted_claim_logical_ids:
        result = _propagate_to_single_claim(
            conn,
            sle=sle,
            claim_logical_id=claim_logical_id,
            idempotency_key=idempotency_key,
        )
        outcome = result["outcome"]
        if outcome == "created":
            created_unverifiable_count += 1
        elif outcome == "skipped":
            skipped_claim_count += 1
        else:  # "failed"
            failed_claim_count += 1

    # 5) Per-published-answer propagation. Computed against the FULL impacted
    #    claim set (including claims that ended up 'skipped' or 'failed'):
    #    the published_answer link does not depend on whether a new ledger
    #    entry was appended on this exact call, only on whether any of its
    #    backing logical claims is in the impact set.
    impacted_pas = _select_impacted_published_answers(
        conn, claim_logical_ids=impacted_claim_logical_ids
    )

    newly_recorded_pa_count = 0
    for pa in impacted_pas:
        if _propagate_to_single_published_answer(
            conn,
            sle=sle,
            pa=pa,
            idempotency_key=idempotency_key,
        ):
            newly_recorded_pa_count += 1

    # 6) If we had impacted claims but zero active published_answers, record
    #    the dedicated 'no_active_published_answers_impacted' kind.
    if not impacted_pas:
        no_active_details = {
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "source_loss_event_id": str(source_loss_event_id),
            "evidence_span_id": str(evidence_span_id),
            "impacted_claim_logical_ids": [str(x) for x in impacted_claim_logical_ids],
            "reason": "no_active_published_answers",
            "loss_kind": str(sle["loss_kind"]),
            "loss_reason": str(sle["loss_reason"]),
        }
        if idempotency_key is not None:
            no_active_details["call_idempotency_key"] = idempotency_key
        _insert_no_active_pa_impacted_record(
            conn,
            source_loss_event_id=source_loss_event_id,
            details=no_active_details,
        )

    logger.info(
        "source_loss_propagator.propagated",
        source_loss_event_id=str(source_loss_event_id),
        evidence_span_id=str(evidence_span_id),
        impacted_claim_count=len(impacted_claim_logical_ids),
        created_unverifiable_count=created_unverifiable_count,
        skipped_claim_count=skipped_claim_count,
        failed_claim_count=failed_claim_count,
        impacted_published_answer_count=len(impacted_pas),
        newly_recorded_published_answer_count=newly_recorded_pa_count,
    )

    return {
        "status": "propagated",
        "source_loss_event_id": str(source_loss_event_id),
        "evidence_span_id": str(evidence_span_id),
        "impacted_claim_count": len(impacted_claim_logical_ids),
        "created_unverifiable_count": created_unverifiable_count,
        "skipped_claim_count": skipped_claim_count,
        "failed_claim_count": failed_claim_count,
        "impacted_published_answer_count": len(impacted_pas),
        "newly_recorded_published_answer_count": newly_recorded_pa_count,
    }
