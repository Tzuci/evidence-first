"""API routes for source-loss producer endpoint (Phase 8.5 — Block 4B-1),
source-loss event read endpoint (Phase 8.6 — Block 8.6B), and source-loss
propagation read endpoint (Phase 8.6 — Block 8.6C).

Endpoints exposed by this module:

  POST /api/v1/source-loss-events                                       (Phase 8.5)
  GET  /api/v1/source-loss-events/{source_loss_event_id}                (Phase 8.6B)
  GET  /api/v1/source-loss-events/{source_loss_event_id}/propagation    (Phase 8.6C)

---------------------------------------------------------------------------
POST /api/v1/source-loss-events  (Phase 8.5 — Block 4B-1)
---------------------------------------------------------------------------

This endpoint creates an append-only ``source_loss_events`` row and
publishes a ``source_loss.detected`` event on the dedicated Redis stream
(``app.events.source_loss_detected``). The actual propagation work —
appending unverifiable ledger entries, recording impact on
published_answers, and emitting audits — is performed asynchronously by
the worker (``apps/worker/app/consumers/source_loss.py`` →
``apps/worker/app/services/source_loss_propagator.py``).

Behavior contract:

  - The endpoint validates the supplied ``evidence_span_id`` against the
    real corpus and derives the canonical scope columns
    (``tenant_id``, ``project_id``, ``document_chunk_id``,
    ``document_version_id``, ``document_id``) from
    ``evidence_spans → document_chunks → document_versions →
    uploaded_documents``. The body cannot override these values.
  - ``task_id`` is intentionally NOT derived. An evidence_span can be
    referenced by claims belonging to multiple tasks via
    ``claim_evidence_links → logical_claims.task_id``; there is no
    unique task scope for a span at the schema level. The schema
    allows ``source_loss_events.task_id`` to be NULL, and that is what
    we persist here. The propagator, downstream, resolves task scope
    per impacted claim from ``logical_claims`` (see
    ``_propagate_to_single_claim``).
  - If the ``evidence_span_id`` does not exist, the endpoint returns
    404 ``RESOURCE_NOT_FOUND`` with
    ``details.resource = "evidence_spans"``. No row is inserted, no
    Redis event is published.
  - The endpoint NEVER:
      * mutates ``claim_ledger_entries``, ``claim_lineage``,
        ``source_loss_propagation_records``;
      * mutates ``published_answers.status``,
        ``published_answer_lifecycle_events``;
      * invokes ``propagate_source_loss`` in-process.

Transaction & external-side-effect ordering:

  We insert the ``source_loss_events`` row and XADD on Redis inside the
  SAME transaction (option **B** in the block prompt). The XADD happens
  BEFORE the DB commit:

    BEGIN
      INSERT INTO source_loss_events ... RETURNING id, idempotency_key
      r.xadd(stream, fields, ...)
    COMMIT

  If the XADD raises, the transaction context manager rolls back the
  insert — no orphan ``source_loss_events`` row remains. This is the
  preferred behavior for a synchronous producer whose entire purpose is
  to enqueue work asynchronously: a row in ``source_loss_events`` that
  the worker will never see is a worse failure mode than no row at all.

  The narrow remaining risk is a successful XADD followed by a crash
  before COMMIT: in that case Redis carries a ``source_loss.detected``
  event whose ``source_loss_event_id`` points to a UUID that was never
  committed. The worker already handles this gracefully: the consumer's
  scope-resolution returns ``None``, the EPR is recorded as ``failed``
  with ``WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE`` (see
  ``apps/worker/app/consumers/source_loss.py``), and the event leaves a
  diagnostic trail without further harm. A future enhancement could add
  a confirmation-stamp on ``source_loss_events`` and a reaper job for
  unconfirmed Redis entries, but it is out of scope for Block 4B-1.

Idempotency:

  ``source_loss_events`` has a UNIQUE constraint on
  ``(evidence_span_id, loss_kind, idempotency_key)``. If the client
  passes an explicit ``idempotency_key`` that collides with an existing
  row for the same ``(evidence_span_id, loss_kind)``, the INSERT raises
  ``IntegrityError`` and we surface 409 ``RESOURCE_CONFLICT`` with the
  conflicting tuple in ``details``. When the client omits the key, we
  generate ``uuid4().hex`` — collision probability is effectively zero.

  Note: this idempotency is DB-level (per-source-loss-row). The
  worker's consumer-level idempotency (EPR's UNIQUE
  ``(consumer_name, idempotency_key)``) is a separate concern. We
  forward the same key to the Redis event for traceability; the worker
  uses it to open or replay the EPR row, while the propagator uses the
  ``source_loss_event_id`` itself as the schema-level idempotency token
  for ``source_loss_propagation_records``.

Error responses:

  - 404 RESOURCE_NOT_FOUND  — evidence_span_id not found.
  - 409 RESOURCE_CONFLICT   — idempotency_key collides with an existing
                              source_loss_events row for the same
                              (evidence_span_id, loss_kind).
  - 400 VALIDATION_ERROR    — event_payload is not JSON-serializable
                              (Pydantic catches most cases earlier).
  - 500 INTERNAL_ERROR      — Redis XADD failure (the DB transaction has
                              already been rolled back; no row was
                              persisted).

---------------------------------------------------------------------------
GET /api/v1/source-loss-events/{source_loss_event_id}  (Phase 8.6 — 8.6B)
---------------------------------------------------------------------------

Strict invariants (Phase 8.6B — read-only observability):

  - This endpoint is COMPLETELY read-only. It MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call ``propagate_source_loss`` or any other worker service;
      * read or join ``source_loss_propagation_records``;
      * resolve ``task_id`` via ``claim_evidence_links`` when the
        column is NULL in the database — by design, the producer
        leaves ``source_loss_events.task_id`` NULL because an
        evidence_span can be referenced by claims belonging to
        multiple tasks (see Phase 8.5 contract above);
      * mutate ``claim_ledger_entries``, ``published_answers``, or any
        other domain table;
      * use Redis;
      * trigger the worker in any way.

  - The endpoint surfaces exactly the columns persisted in
    ``source_loss_events`` for the given id, serialized via the shared
    ``SourceLossEventRead`` schema. Nullable columns (project_id,
    task_id, document_chunk_id, document_version_id, document_id) are
    serialized as JSON ``null`` when the DB row has NULL, matching the
    Pydantic field types declared in
    ``packages/shared/evidencefirst_shared/schemas.py``.

  - 404 RESOURCE_NOT_FOUND with details.resource="source_loss_events"
    and details.id=<source_loss_event_id> is returned when no row
    matches the supplied id. The error envelope mirrors the
    convention used by the lifecycle events endpoint (8.6A) and the
    answers endpoints (8.4).

  - All JSONB columns (``event_payload``) are returned verbatim. MVP-0
    does not yet apply RBAC redaction; this is acknowledged in
    PHASE_8_6_PLAN.md §9 as a known debt.

---------------------------------------------------------------------------
GET /api/v1/source-loss-events/{source_loss_event_id}/propagation
                                                       (Phase 8.6 — 8.6C)
---------------------------------------------------------------------------

Strict invariants (Phase 8.6C — read-only observability):

  - This endpoint is COMPLETELY read-only. It MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call ``propagate_source_loss`` or any other worker service;
      * import worker code;
      * use Redis;
      * mutate ``source_loss_propagation_records``,
        ``claim_ledger_entries``, ``claim_lineage``,
        ``audit_records``, ``published_answers``, or
        ``source_loss_events`` in any way.

  - The endpoint first checks that the given ``source_loss_event_id``
    exists in ``source_loss_events``. If not, it returns 404
    ``RESOURCE_NOT_FOUND`` with ``details.resource =
    "source_loss_events"`` and ``details.id = <source_loss_event_id>``.

  - If the source_loss_event exists but has no propagation rows yet
    (legitimate race window between
    ``POST /api/v1/source-loss-events`` and the worker's propagator
    processing the event), the endpoint returns 200 with
    ``items = []``. This is by design: PHASE_8_6_PLAN.md §9 calls out
    the race explicitly, and the contract is to surface DB state
    truthfully without fabricating propagation history.

  - Otherwise, the endpoint returns 200 with the list of matching rows
    ordered ASC by (created_at, id), filtered by the optional
    ``propagation_kind`` and ``status`` query parameters, and
    truncated to ``limit`` rows. The wrapper shape is::

        {"source_loss_event_id": "<uuid>", "items": [SourceLossPropagationRecordRead, ...]}

  - This endpoint does NOT collapse or hide ``failed`` rows: a client
    interested in propagation health needs to see them. The four
    declared ``propagation_kind`` values and the three declared
    ``status`` values are all admissible filter values.

  - This endpoint does NOT evaluate source quality, source authority,
    independence, primaryness or freshness. The presence of a
    propagation row only means that a source was lost and the system
    reacted; it does not mean the lost source was authoritative to
    begin with. The Source Quality Evaluator is a future fase (see
    PHASE_8_6_PLAN.md strategic note).

  - All JSONB columns (``details``) are returned verbatim. MVP-0 does
    not yet apply RBAC redaction; this is acknowledged in
    PHASE_8_6_PLAN.md §9 as a known debt.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import (
    SourceLossEventRead,
    SourceLossPropagationRecordRead,
)

from ..config import get_settings
from ..db import get_conn, transaction
from ..redis import get_redis


logger = structlog.get_logger(__name__)


router = APIRouter()


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
EVENT_TYPE_SOURCE_LOSS_DETECTED = "source_loss.detected"

# Default loss_kind for client-facing source-loss reports. This is the
# most common scenario (the user notices a document is gone). All five
# values declared in 0006_lifecycle.sql's CHECK are accepted via the
# Pydantic Literal below.
DEFAULT_LOSS_KIND = "source_deleted"

# Default loss_reason when the client omits one. The DB column is
# NOT NULL; we never want to leak a 500 just because the producer was
# terse, so we substitute a short marker.
DEFAULT_LOSS_REASON_API = "source_loss_reported_via_api"

# The schema requires detected_by NOT NULL. In MVP-0 there is no real
# auth, so we tag every API-originated row with this stable marker.
DEFAULT_DETECTED_BY_API = "api"


# Allowed loss_kind values, mirroring the CHECK in 0006_lifecycle.sql.
LossKind = Literal[
    "source_deleted",
    "source_access_lost",
    "quote_mismatch",
    "document_replaced",
    "policy_retraction",
]

# Allowed propagation_kind values for the 8.6C filter, mirroring the
# CHECK in 0006_lifecycle.sql on source_loss_propagation_records.
PropagationKind = Literal[
    "claim_marked_unverifiable",
    "published_answer_impacted",
    "no_claims_impacted",
    "no_active_published_answers_impacted",
]

# Allowed status values for the 8.6C filter, mirroring the CHECK in
# 0006_lifecycle.sql on source_loss_propagation_records.
PropagationStatus = Literal["recorded", "skipped", "failed"]


# ---------------------------------------------------------------------------
# Pydantic models (producer endpoint)
# ---------------------------------------------------------------------------
class SourceLossEventCreate(BaseModel):
    """Request body for POST /api/v1/source-loss-events.

    Fields:
      - ``evidence_span_id``: REQUIRED. Canonical granularity of the
        source loss. All scope columns (tenant_id, project_id,
        document_chunk_id, document_version_id, document_id) are derived
        from this id by the API; the body cannot supply them.
      - ``loss_kind``: optional. One of the five values declared in
        0006_lifecycle.sql. Defaults to ``"source_deleted"``.
      - ``loss_reason``: optional free-text reason. When omitted, falls
        back to ``DEFAULT_LOSS_REASON_API``.
      - ``idempotency_key``: optional. Used both as the DB-level
        idempotency token for ``source_loss_events`` UNIQUE
        ``(evidence_span_id, loss_kind, idempotency_key)`` AND as the
        consumer-level idempotency key forwarded to the worker. When
        omitted, the API generates ``uuid4().hex``.
      - ``event_payload``: optional opaque JSON-serializable dict.
        Persisted into ``source_loss_events.event_payload`` (JSONB) and
        propagated on the Redis stream as ``event_payload_json`` for
        traceability. The consumer/propagator do not read this dict
        directly; they read the persisted JSONB column from the
        ``source_loss_events`` row when they process the event.
    """

    evidence_span_id: uuid.UUID
    loss_kind: LossKind | None = None
    loss_reason: str | None = Field(default=None, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=200)
    event_payload: dict[str, Any] | None = None


class SourceLossEventQueued(BaseModel):
    """Response body for an accepted source-loss event.

    The endpoint always returns 202 Accepted. The lifecycle of the
    triggered propagation (claim ledger updates, published_answer
    impact records, audits) is asynchronous and observable through the
    worker's existing surfaces (``source_loss_propagation_records``,
    claim and audit endpoints).
    """

    status: str
    event_type: str
    event_id: uuid.UUID
    source_loss_event_id: uuid.UUID
    evidence_span_id: uuid.UUID
    stream: str
    idempotency_key: str


# ---------------------------------------------------------------------------
# DB helpers (producer endpoint)
# ---------------------------------------------------------------------------
def _resolve_evidence_span_scope(
    conn: Connection, *, evidence_span_id: uuid.UUID
) -> dict[str, Any] | None:
    """Resolve the canonical scope columns for an ``evidence_span``.

    Returns a dict with the following keys, or ``None`` when the
    evidence_span does not exist:

        document_chunk_id   : uuid.UUID
        document_version_id : uuid.UUID
        document_id         : uuid.UUID
        tenant_id           : uuid.UUID  (from uploaded_documents)
        project_id          : uuid.UUID  (from uploaded_documents)

    All of these flow from the immutable storage chain declared in
    0003_documents.sql: evidence_spans → document_chunks →
    document_versions → uploaded_documents. ``document_chunks`` has a
    CHECK that ``document_version_id IS NOT NULL`` (see
    ``dc_origin_xor``), so the join is total whenever the
    evidence_span row exists.

    ``uploaded_documents.tenant_id`` and ``uploaded_documents.project_id``
    are both ``NOT NULL`` per 0003. The returned tenant_id and
    project_id are therefore guaranteed non-null whenever the
    evidence_span exists.

    No FOR UPDATE: this is a read-only resolution. The INSERT happens
    later inside the same transaction; ``source_loss_events`` has its
    own UNIQUE that protects against duplicate inserts.
    """
    row = conn.execute(
        text(
            """
            SELECT
              dc.id  AS document_chunk_id,
              dv.id  AS document_version_id,
              ud.id  AS document_id,
              ud.tenant_id  AS tenant_id,
              ud.project_id AS project_id
            FROM evidence_spans     es
            JOIN document_chunks    dc ON dc.id = es.document_chunk_id
            JOIN document_versions  dv ON dv.id = dc.document_version_id
            JOIN uploaded_documents ud ON ud.id = dv.document_id
            WHERE es.id = :esid
            """
        ),
        {"esid": evidence_span_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "document_chunk_id": uuid.UUID(str(m["document_chunk_id"])),
        "document_version_id": uuid.UUID(str(m["document_version_id"])),
        "document_id": uuid.UUID(str(m["document_id"])),
        "tenant_id": uuid.UUID(str(m["tenant_id"])),
        "project_id": uuid.UUID(str(m["project_id"])),
    }


def _insert_source_loss_event(
    conn: Connection,
    *,
    evidence_span_id: uuid.UUID,
    scope: dict[str, Any],
    loss_kind: str,
    loss_reason: str,
    detected_by: str,
    event_payload: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    """Insert one append-only ``source_loss_events`` row.

    ``task_id`` is intentionally NULL: see module docstring.

    The schema's UNIQUE
    ``(evidence_span_id, loss_kind, idempotency_key)`` will raise
    ``IntegrityError`` on collision; the caller is responsible for
    translating it into a normalized 409 RESOURCE_CONFLICT.

    ``tenant_id`` is NOT NULL on the schema; we always set it from the
    resolved scope.
    """
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO source_loss_events (
                id, tenant_id, project_id, task_id,
                evidence_span_id, document_chunk_id,
                document_version_id, document_id,
                loss_kind, loss_reason, detected_by,
                event_payload, idempotency_key
            ) VALUES (
                :id, :tenant_id, :project_id, NULL,
                :evidence_span_id, :document_chunk_id,
                :document_version_id, :document_id,
                :loss_kind, :loss_reason, :detected_by,
                CAST(:event_payload AS JSONB), :idempotency_key
            )
            """
        ),
        {
            "id": new_id,
            "tenant_id": scope["tenant_id"],
            "project_id": scope["project_id"],
            "evidence_span_id": evidence_span_id,
            "document_chunk_id": scope["document_chunk_id"],
            "document_version_id": scope["document_version_id"],
            "document_id": scope["document_id"],
            "loss_kind": loss_kind,
            "loss_reason": loss_reason,
            "detected_by": detected_by,
            "event_payload": json.dumps(
                event_payload, separators=(",", ":"), sort_keys=True
            ),
            "idempotency_key": idempotency_key,
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB helpers (read endpoint — Phase 8.6B)
# ---------------------------------------------------------------------------
def _select_source_loss_event_by_id(
    conn: Connection, source_loss_event_id: uuid.UUID
) -> dict[str, Any] | None:
    """Fetch a single ``source_loss_events`` row by primary key.

    Returns a dict whose keys match the column names in
    ``source_loss_events``, or ``None`` when no row matches.

    Read-only: plain SELECT, no FOR UPDATE. The endpoint never mutates
    DB state, so row-level locking would be wasteful.

    JSONB ``event_payload`` is normalized to a dict if the driver
    surfaces it as a string (psycopg 3 returns dicts natively, but the
    fallback is defensive and matches the convention in
    ``lifecycle_events.py``).
    """
    row = conn.execute(
        text(
            """
            SELECT
              id, tenant_id, project_id, task_id,
              evidence_span_id, document_chunk_id,
              document_version_id, document_id,
              loss_kind, loss_reason, detected_by,
              event_payload, idempotency_key, created_at
            FROM source_loss_events
            WHERE id = :id
            """
        ),
        {"id": source_loss_event_id},
    ).first()
    if row is None:
        return None

    m = row._mapping

    # event_payload normalization: accept either native dict (psycopg 3
    # JSONB), JSON string (driver/pool edge case), or NULL (defensive —
    # the column is NOT NULL DEFAULT '{}'::jsonb so this should not
    # occur in practice).
    raw_payload = m["event_payload"]
    if raw_payload is None:
        event_payload: dict[str, Any] = {}
    elif isinstance(raw_payload, str):
        event_payload = json.loads(raw_payload)
    else:
        event_payload = dict(raw_payload)

    def _opt_uuid(value: Any) -> uuid.UUID | None:
        return uuid.UUID(str(value)) if value is not None else None

    return {
        "id": uuid.UUID(str(m["id"])),
        "tenant_id": uuid.UUID(str(m["tenant_id"])),
        "project_id": _opt_uuid(m["project_id"]),
        "task_id": _opt_uuid(m["task_id"]),
        "evidence_span_id": uuid.UUID(str(m["evidence_span_id"])),
        "document_chunk_id": _opt_uuid(m["document_chunk_id"]),
        "document_version_id": _opt_uuid(m["document_version_id"]),
        "document_id": _opt_uuid(m["document_id"]),
        "loss_kind": str(m["loss_kind"]),
        "loss_reason": str(m["loss_reason"]),
        "detected_by": str(m["detected_by"]),
        "event_payload": event_payload,
        "idempotency_key": str(m["idempotency_key"]),
        "created_at": m["created_at"],
    }


def _raise_source_loss_event_not_found(source_loss_event_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope expected by callers.

    Envelope shape mirrors the helper used in routes/lifecycle_events.py
    and routes/answers.py so clients can rely on the same
    ``details.resource``/``details.id`` contract across all
    8.4/8.5/8.6 endpoints.

    Reused by both the 8.6B and 8.6C endpoints: both surfaces resolve
    the same underlying ``source_loss_events`` resource and present an
    identical not-found contract to the client.
    """
    raise NormalizedError(
        code=ErrorCode.RESOURCE_NOT_FOUND,
        message="source_loss_events not found",
        details={
            "resource": "source_loss_events",
            "id": str(source_loss_event_id),
        },
        http_status=404,
    )


# ---------------------------------------------------------------------------
# DB helpers (propagation read endpoint — Phase 8.6C)
# ---------------------------------------------------------------------------
def _source_loss_event_exists(
    conn: Connection, source_loss_event_id: uuid.UUID
) -> bool:
    """Return True iff a ``source_loss_events`` row with the given id exists.

    Read-only: plain ``SELECT 1 ... LIMIT 1``. We do NOT reuse
    ``_select_source_loss_event_by_id`` here because we only need a
    boolean and want to avoid materializing the full row, which would
    include JSONB deserialization for a check that does not need it.

    The 8.6C endpoint uses this helper to gate the 404 path before
    issuing the propagation SELECT. When the row exists but no
    propagation rows do, the endpoint returns 200 with ``items = []``
    rather than 404 — that distinction is what makes this dedicated
    helper worth its weight.
    """
    row = conn.execute(
        text("SELECT 1 FROM source_loss_events WHERE id = :id LIMIT 1"),
        {"id": source_loss_event_id},
    ).first()
    return row is not None


def _row_to_source_loss_propagation_record(
    row: Any,
) -> SourceLossPropagationRecordRead:
    """Map a SQLAlchemy row to the shared SourceLossPropagationRecordRead.

    Field coercion notes:
      - UUID columns may surface as ``uuid.UUID`` or as ``str``
        depending on the driver/pool combination; we normalize via
        ``uuid.UUID(str(...))`` defensively, mirroring the convention
        adopted in ``lifecycle_events.py``.
      - ``details`` is JSONB: psycopg 3 returns it as a native dict,
        but we coerce a stray ``None`` (defensive — the column is
        NOT NULL DEFAULT '{}'::jsonb so this should not occur in
        practice) to an empty dict, and a stray string (some
        driver/pool combinations) via ``json.loads``. This keeps the
        Pydantic model happy under every realistic driver variant.
      - All optional FK columns (``claim_logical_id``,
        ``old_claim_ledger_entry_id``, ``new_claim_ledger_entry_id``,
        ``published_answer_id``) may be NULL per the schema; we
        surface them as ``None`` so the response carries JSON ``null``.
    """
    m = row._mapping

    raw_details = m["details"]
    if raw_details is None:
        details: dict[str, Any] = {}
    elif isinstance(raw_details, str):
        details = json.loads(raw_details)
    else:
        details = dict(raw_details)

    def _opt_uuid(value: Any) -> uuid.UUID | None:
        return uuid.UUID(str(value)) if value is not None else None

    return SourceLossPropagationRecordRead(
        id=uuid.UUID(str(m["id"])),
        source_loss_event_id=uuid.UUID(str(m["source_loss_event_id"])),
        claim_logical_id=_opt_uuid(m["claim_logical_id"]),
        old_claim_ledger_entry_id=_opt_uuid(m["old_claim_ledger_entry_id"]),
        new_claim_ledger_entry_id=_opt_uuid(m["new_claim_ledger_entry_id"]),
        published_answer_id=_opt_uuid(m["published_answer_id"]),
        propagation_kind=str(m["propagation_kind"]),
        status=str(m["status"]),
        details=details,
        created_at=m["created_at"],
    )


def _select_source_loss_propagation_records(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    limit: int,
    propagation_kind: str | None,
    status: str | None,
) -> list[Any]:
    """Fetch propagation rows for a given source_loss_event, filtered.

    Ordering is ASC by ``(created_at, id)`` for replay-friendliness
    and to keep the limit truncation deterministic.

    Query construction notes:
      - We use a single fixed SQL string with NULL-aware filter
        predicates ``(:kind IS NULL OR propagation_kind = :kind)``
        and the equivalent for ``:status``. This avoids any string
        interpolation or concatenation of SQL fragments and keeps
        every value strictly as a bound parameter. PostgreSQL's
        planner short-circuits the ``IS NULL`` branch when the
        parameter is NULL, so the absence of a filter has the same
        plan as an unfiltered query.
      - No f-string SQL anywhere.

    The caller is expected to have already verified that the source
    loss event exists (via ``_source_loss_event_exists``); this helper
    returns an empty list both when no event exists AND when the event
    exists but has no propagation rows. The 404/200-empty distinction
    is the caller's job.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              id,
              source_loss_event_id,
              claim_logical_id,
              old_claim_ledger_entry_id,
              new_claim_ledger_entry_id,
              published_answer_id,
              propagation_kind,
              status,
              details,
              created_at
            FROM source_loss_propagation_records
            WHERE source_loss_event_id = :sle_id
              AND (CAST(:kind   AS TEXT) IS NULL OR propagation_kind = CAST(:kind   AS TEXT))
              AND (CAST(:status AS TEXT) IS NULL OR status           = CAST(:status AS TEXT))
            ORDER BY created_at ASC, id ASC
            LIMIT :limit
            """
        ),
        {
            "sle_id": source_loss_event_id,
            "kind": propagation_kind,
            "status": status,
            "limit": limit,
        },
    ).fetchall()
    return list(rows)


# ---------------------------------------------------------------------------
# producer endpoint (Phase 8.5 — Block 4B-1)
# ---------------------------------------------------------------------------
@router.post(
    "/api/v1/source-loss-events",
    response_model=SourceLossEventQueued,
    status_code=202,
    tags=["source_loss"],
)
def create_source_loss_event(
    body: SourceLossEventCreate,
) -> SourceLossEventQueued:
    """Create a ``source_loss_events`` row and enqueue the propagation event.

    See module docstring for the full contract. End-to-end flow:

      1. Resolve ``evidence_span_id`` → (tenant, project, chunk,
         version, document). 404 if missing.
      2. Open a transaction.
      3. INSERT into ``source_loss_events`` (RETURNING id).
      4. Build the Redis event payload (stream fields are strings).
      5. XADD on the configured ``EVENTS_SOURCE_LOSS_STREAM``.
      6. Commit. If any step in [3..5] fails, the transaction rolls
         back and no orphan row remains.
      7. Return 202 with the event metadata so the client can correlate
         with the asynchronous propagation outcome.
    """
    body = body  # explicit no-op for readability
    loss_kind = body.loss_kind if body.loss_kind is not None else DEFAULT_LOSS_KIND

    if body.loss_reason is not None and body.loss_reason != "":
        loss_reason = body.loss_reason
    else:
        loss_reason = DEFAULT_LOSS_REASON_API

    if body.idempotency_key is not None and body.idempotency_key != "":
        idempotency_key = body.idempotency_key
    else:
        idempotency_key = uuid.uuid4().hex

    event_payload = body.event_payload if body.event_payload is not None else {}

    # Validate JSON-serializability of event_payload early. Pydantic
    # already enforces a dict-of-Any shape; this catches edge cases
    # (custom objects, bytes, etc.) before we even open a transaction.
    try:
        event_payload_json = json.dumps(
            event_payload, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError) as exc:
        raise NormalizedError(
            ErrorCode.VALIDATION_ERROR,
            "event_payload is not JSON-serializable",
            details={"exception_type": exc.__class__.__name__},
        )

    settings = get_settings()
    stream = settings.EVENTS_SOURCE_LOSS_STREAM

    event_id = uuid.uuid4()
    source_loss_event_id: uuid.UUID | None = None

    try:
        with transaction() as conn:
            scope = _resolve_evidence_span_scope(
                conn, evidence_span_id=body.evidence_span_id
            )
            if scope is None:
                # 404: the evidence_span does not exist. We raise inside
                # the transaction; the context manager rolls back the
                # (empty) transaction on exception, and the normalized
                # error handler emits the envelope.
                raise NormalizedError(
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                    message="evidence_spans not found",
                    details={
                        "resource": "evidence_spans",
                        "id": str(body.evidence_span_id),
                    },
                    http_status=404,
                )

            # INSERT first; if the UNIQUE blows up, the IntegrityError
            # short-circuits the XADD entirely.
            source_loss_event_id = _insert_source_loss_event(
                conn,
                evidence_span_id=body.evidence_span_id,
                scope=scope,
                loss_kind=loss_kind,
                loss_reason=loss_reason,
                detected_by=DEFAULT_DETECTED_BY_API,
                event_payload=event_payload,
                idempotency_key=idempotency_key,
            )

            # Build the Redis fields. All values must be strings;
            # optional fields are omitted from the dict when absent
            # rather than serialized as "" or "null", to keep stream
            # entries minimal and avoid ambiguity in the consumer's
            # _coerce_optional_uuid (which treats "" as None).
            fields: dict[str, str] = {
                "event_id": str(event_id),
                "event_type": EVENT_TYPE_SOURCE_LOSS_DETECTED,
                "source_loss_event_id": str(source_loss_event_id),
                "evidence_span_id": str(body.evidence_span_id),
                "idempotency_key": idempotency_key,
                "tenant_id": str(scope["tenant_id"]),
                "project_id": str(scope["project_id"]),
                "document_chunk_id": str(scope["document_chunk_id"]),
                "document_version_id": str(scope["document_version_id"]),
                "document_id": str(scope["document_id"]),
                "loss_kind": loss_kind,
                "loss_reason": loss_reason,
                "detected_by": DEFAULT_DETECTED_BY_API,
            }
            # event_payload_json is only attached when the client
            # supplied a non-empty dict. The consumer does not read it
            # (it loads event_payload from the source_loss_events row
            # directly), but downstream operators may inspect Redis
            # entries for forensic/replay purposes.
            if event_payload:
                fields["event_payload_json"] = event_payload_json

            # XADD inside the transaction. If it raises, the
            # transaction context manager rolls back the INSERT.
            try:
                r = get_redis()
                r.xadd(
                    stream,
                    fields,
                    maxlen=10000,
                    approximate=True,
                )
            except Exception as exc:  # noqa: BLE001 — log and re-raise normalized
                logger.exception(
                    "source_loss_event_publish_failed",
                    evidence_span_id=str(body.evidence_span_id),
                    stream=stream,
                    event_id=str(event_id),
                )
                raise NormalizedError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to publish source_loss event",
                    details={
                        "stream": stream,
                        "exception_type": exc.__class__.__name__,
                    },
                )

    except IntegrityError as exc:
        # UNIQUE (evidence_span_id, loss_kind, idempotency_key) collision.
        # The transaction context manager has already rolled back. We
        # surface a normalized 409 so a client retrying with the same
        # idempotency_key sees a deterministic answer rather than a
        # 500 INTERNAL_ERROR.
        logger.info(
            "source_loss_event_idempotency_conflict",
            evidence_span_id=str(body.evidence_span_id),
            loss_kind=loss_kind,
            idempotency_key=idempotency_key,
            exception_type=exc.__class__.__name__,
        )
        raise NormalizedError(
            code=ErrorCode.RESOURCE_CONFLICT,
            message=(
                "A source_loss_events row already exists for the same "
                "(evidence_span_id, loss_kind, idempotency_key)."
            ),
            details={
                "resource": "source_loss_events",
                "evidence_span_id": str(body.evidence_span_id),
                "loss_kind": loss_kind,
                "idempotency_key": idempotency_key,
            },
        )

    # Defensive: source_loss_event_id is always set when we exit the
    # transaction without exception. The assertion is here to make the
    # type-narrowing explicit for the return statement.
    assert source_loss_event_id is not None

    return SourceLossEventQueued(
        status="queued",
        event_type=EVENT_TYPE_SOURCE_LOSS_DETECTED,
        event_id=event_id,
        source_loss_event_id=source_loss_event_id,
        evidence_span_id=body.evidence_span_id,
        stream=stream,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# read endpoint (Phase 8.6 — Block 8.6B)
# ---------------------------------------------------------------------------
@router.get(
    "/api/v1/source-loss-events/{source_loss_event_id}",
    response_model=SourceLossEventRead,
    tags=["source_loss"],
)
def get_source_loss_event(
    source_loss_event_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> SourceLossEventRead:
    """Single-row read of a ``source_loss_events`` entity by id.

    Read-only observability endpoint introduced in Phase 8.6B. See the
    module-level docstring for the strict invariants this endpoint
    honors. In particular:

      - No DB mutation, no Redis, no worker import, no propagator call.
      - ``task_id`` may be NULL in the database: the propagator
        resolves task scope per impacted claim downstream; this
        endpoint simply surfaces what the DB stores. A NULL ``task_id``
        is serialized as JSON ``null`` in the response, matching the
        ``task_id: uuid.UUID | None`` field on
        ``SourceLossEventRead``.
      - The endpoint does NOT join
        ``source_loss_propagation_records``; that is the job of the
        8.6C endpoint. A consumer that wants the propagation
        outcome for a given source_loss event must call 8.6C
        explicitly.

    Errors:
      - 404 RESOURCE_NOT_FOUND with
        ``details.resource = "source_loss_events"`` and
        ``details.id = <source_loss_event_id>`` when no row matches.
    """
    row = _select_source_loss_event_by_id(conn, source_loss_event_id)
    if row is None:
        _raise_source_loss_event_not_found(source_loss_event_id)
        # _raise_source_loss_event_not_found never returns; the
        # explicit ``raise`` keeps static analyzers happy without
        # ``# type: ignore``.
        raise AssertionError("unreachable")

    return SourceLossEventRead(**row)


# ---------------------------------------------------------------------------
# propagation read endpoint (Phase 8.6 — Block 8.6C)
# ---------------------------------------------------------------------------
@router.get(
    "/api/v1/source-loss-events/{source_loss_event_id}/propagation",
    tags=["source_loss"],
)
def list_source_loss_propagation_records(
    source_loss_event_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=500, ge=1, le=5000),
    propagation_kind: PropagationKind | None = Query(default=None),
    status: PropagationStatus | None = Query(default=None),
) -> dict[str, Any]:
    """List propagation rows for a source_loss_event (read-only).

    Behavior:
      - 404 RESOURCE_NOT_FOUND with details.resource="source_loss_events"
        if the source_loss_event does not exist. The check is performed
        BEFORE the propagation SELECT, so a client probing for a bogus
        id receives an immediate not-found rather than a misleading
        empty list.
      - 200 with ``items=[]`` if the source_loss_event exists but has
        no propagation rows (including the legitimate race window
        between ``POST /api/v1/source-loss-events`` and the worker's
        propagator processing the event; PHASE_8_6_PLAN.md §9 calls
        this out explicitly).
      - 200 with the list of matching rows, ordered ASC by
        (created_at, id), filtered by ``propagation_kind`` and
        ``status`` if provided, and truncated to ``limit`` rows.

    The wrapper shape is inline ``{"source_loss_event_id": <uuid>,
    "items": [SourceLossPropagationRecordRead, ...]}``. We do not bind
    a Pydantic ``response_model`` here because the wrapper is purely a
    response shape; the items themselves are serialized via the
    shared ``SourceLossPropagationRecordRead`` model (mirrors the
    pattern in ``lifecycle_events.py`` for 8.6A).

    Strict scope reminder:
      - This endpoint does NOT evaluate source quality, authority,
        primaryness, freshness, or independence. The presence of a
        propagation row only means the system tracked a reaction to a
        source loss. The Source Quality Evaluator is a future fase
        (see PHASE_8_6_PLAN.md strategic note).
      - All JSONB ``details`` payloads are returned verbatim; RBAC
        redaction is not applied in MVP-0 (PHASE_8_6_PLAN.md §9).
    """
    if not _source_loss_event_exists(conn, source_loss_event_id):
        _raise_source_loss_event_not_found(source_loss_event_id)
        # _raise_source_loss_event_not_found never returns; the
        # explicit ``raise`` keeps static analyzers happy without
        # ``# type: ignore``.
        raise AssertionError("unreachable")

    rows = _select_source_loss_propagation_records(
        conn,
        source_loss_event_id=source_loss_event_id,
        limit=limit,
        propagation_kind=propagation_kind,
        status=status,
    )
    items = [
        _row_to_source_loss_propagation_record(r).model_dump(mode="json")
        for r in rows
    ]

    return {
        "source_loss_event_id": str(source_loss_event_id),
        "items": items,
    }
