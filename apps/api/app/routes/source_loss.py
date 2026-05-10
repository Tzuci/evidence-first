"""API routes for source-loss producer endpoint (Phase 8.5 — Block 4B-1).

Endpoint:
  POST /api/v1/source-loss-events

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
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError

from ..config import get_settings
from ..db import transaction
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


# ---------------------------------------------------------------------------
# Pydantic models
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
# DB helpers
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
# endpoint
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
