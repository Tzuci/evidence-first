"""Mock source quality evaluator service (Phase 8.7 — Block D).

This module is the FIRST writer for the source_quality_assessments table
introduced by migration 0007_source_quality.sql. It is a deterministic,
mock-driven evaluator: it does NOT consult any AI/LLM provider, does NOT
perform web search, and does NOT apply any real heuristic. It writes
explainable, codomain-valid rows that future blocks (8.7E worker
integration, 8.7F read API, 8.7G gate policy) can consume and reason
about.

Strict scope (Phase 8.7D invariants — see PHASE_8_7_PLAN.md §3 / §11):

  - This service ONLY writes to source_quality_assessments.
  - It does NOT mutate claim_ledger_entries, claim_lineage,
    claim_evidence_links, verification_records, logical_claims.
  - It does NOT mutate final_gate_reports, draft_final_answers,
    final_answer_spans, final_answer_span_claim_links,
    coverage_gap_statements, published_answers.
  - It does NOT mutate source_loss_events,
    source_loss_propagation_records, published_answer_lifecycle_events.
  - It does NOT emit audit_records. The audit emission for source
    quality is deferred to the worker integration block (8.7E).
  - It does NOT use Redis, does NOT import FastAPI/API modules, does
    NOT perform any network I/O.
  - It does NOT pretend to evaluate real source authority,
    independence, freshness, or relevance. Every row it writes carries
    ``payload.mock = true`` and a ``payload.semantic_warning`` that
    documents the limitation.

Semantic invariants (from PHASE_8_7_PLAN.md §3):

  - source quality is NOT claim correctness;
  - source quality is NOT evidence support;
  - source quality is NOT verification outcome;
  - source quality is NOT source loss;
  - source quality is NOT final publication eligibility.

Target XOR contract:

  Every assessment evaluates EXACTLY ONE target out of:
    - evidence_span_id
    - document_chunk_id
    - document_id

  This is enforced at three layers:
    1. Application validation in this module (returns
       status='invalid_target' when zero or more than one target is
       provided);
    2. The sqa_target_xor CHECK constraint on the DB;
    3. The shape of the three partial UNIQUE indexes for versioning
       and idempotency.

  The application validation is the user-facing layer: a misconfigured
  caller gets a structured ``status='invalid_target'`` response without
  any DB write rather than an IntegrityError.

Versioning and idempotency:

  - ``version_no`` is monotonically increasing per (target_kind,
    target_id). When this service is invoked twice on the same target
    with DIFFERENT ``idempotency_key`` values, the second invocation
    appends a NEW row with ``version_no = previous_max + 1``.
  - ``idempotency_key`` is unique per (target_kind, target_id, key).
    A redelivery with the SAME ``idempotency_key`` on the same target
    SHORT-CIRCUITS to the existing row and returns
    ``status='already_assessed'`` without inserting a duplicate. The
    same key MAY appear across different targets without collision.
  - To prevent races, we acquire a row-level lock on the target's
    parent row (evidence_spans / document_chunks / uploaded_documents)
    via ``SELECT ... FOR UPDATE`` before computing the next
    ``version_no``. This serializes concurrent appends to
    source_quality_assessments for the same target.

  Belt-and-suspenders: if a concurrent inserter wins the race for the
  same idempotency_key on the same target despite the application
  check, the partial UNIQUE index (sqa_evidence_idem_uq /
  sqa_chunk_idem_uq / sqa_document_idem_uq) raises an
  IntegrityError. This service catches that specific case, re-reads
  the existing row, and returns ``status='already_assessed'`` so the
  caller sees a stable contract.

Mock deterministic policy (Phase 8.7D, see prompt):

  Regardless of target, the assessment fixes:
    - source_type            = "user_document"
    - source_role            = "unclear"
    - authority_level        = "unknown"
    - independence_level     = "unknown"
    - freshness              = "undated"
    - contradiction_status   = "unchecked"
    - overall_quality        = "unknown"
    - confidence             = 0.5

  The two dimensions that vary by target reflect what is reasonable to
  say about that granularity in MVP-0 (a quote span has a tighter
  relationship to a claim than a whole document):
    evidence_span  -> relevance=direct_support,     extract_quality=exact_quote_match
    document_chunk -> relevance=contextual_support, extract_quality=partial_match
    document       -> relevance=contextual_support, extract_quality=partial_match

  All values are explicitly cross-checked against the SOURCE_QUALITY_*_VALUES
  codomains imported from evidencefirst_shared at module-load time
  (see _validate_codomain_membership below): if a future migration
  shrinks a codomain and this module is not updated, import fails
  loudly rather than at INSERT time.

Transaction model:

  The caller passes an active SQLAlchemy ``Connection`` inside an
  explicit transaction (e.g. ``with engine.begin() as conn:``). This
  module never opens its own connection, never commits, never rolls
  back.

What this service intentionally does NOT do (recap):

  - It does NOT claim to evaluate real sources. It writes mock rows
    that future blocks can replace with real evaluations.
  - It does NOT propagate to the Claim Ledger. Source quality lives
    in a separate table by design (see PHASE_8_7_PLAN.md §5).
  - It does NOT emit any audit_records. Audit emission is the
    responsibility of the worker integration block (8.7E), which will
    chain the call into ``task.created`` and stamp a task-scoped
    audit event.

  A source quality assessment is metadata about the source, not a
  claim about claim truth. The Final Answer Gate is NOT affected by
  this service in 8.7D.
"""
from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from evidencefirst_shared.schemas import (
    SOURCE_QUALITY_AUTHORITY_LEVEL_VALUES,
    SOURCE_QUALITY_CONTRADICTION_STATUS_VALUES,
    SOURCE_QUALITY_EXTRACT_QUALITY_VALUES,
    SOURCE_QUALITY_FRESHNESS_VALUES,
    SOURCE_QUALITY_INDEPENDENCE_LEVEL_VALUES,
    SOURCE_QUALITY_OVERALL_QUALITY_VALUES,
    SOURCE_QUALITY_RELEVANCE_VALUES,
    SOURCE_QUALITY_SOURCE_ROLE_VALUES,
    SOURCE_QUALITY_SOURCE_TYPE_VALUES,
)


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------
SERVICE_NAME = "mock_source_quality_evaluator"
SERVICE_VERSION = "0.1.0"

DEFAULT_EVALUATOR_NAME = SERVICE_NAME
DEFAULT_EVALUATOR_VERSION = SERVICE_VERSION
DEFAULT_POLICY_NAME = "mvp0_mock_source_quality"
DEFAULT_POLICY_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Mock deterministic dimensions (shared by all targets)
# ---------------------------------------------------------------------------
_MOCK_SOURCE_TYPE = "user_document"
_MOCK_SOURCE_ROLE = "unclear"
_MOCK_AUTHORITY_LEVEL = "unknown"
_MOCK_INDEPENDENCE_LEVEL = "unknown"
_MOCK_FRESHNESS = "undated"
_MOCK_CONTRADICTION_STATUS = "unchecked"
_MOCK_OVERALL_QUALITY = "unknown"
_MOCK_CONFIDENCE = 0.5

# Target-specific dimensions.
_MOCK_RELEVANCE_BY_TARGET = {
    "evidence_span": "direct_support",
    "document_chunk": "contextual_support",
    "document": "contextual_support",
}
_MOCK_EXTRACT_QUALITY_BY_TARGET = {
    "evidence_span": "exact_quote_match",
    "document_chunk": "partial_match",
    "document": "partial_match",
}

# Payload semantic warning — explicit reminder that source quality is NOT
# claim truth. Stable string consumed by tests as a contract assertion.
_SEMANTIC_WARNING = "source_quality_does_not_mean_claim_truth"


# ---------------------------------------------------------------------------
# Result status discriminants
# ---------------------------------------------------------------------------
STATUS_ASSESSED = "assessed"
STATUS_ALREADY_ASSESSED = "already_assessed"
STATUS_INVALID_TARGET = "invalid_target"
STATUS_NOT_FOUND = "not_found"


# ---------------------------------------------------------------------------
# Codomain validation at module load time
# ---------------------------------------------------------------------------
def _validate_codomain_membership() -> None:
    """Assert that every mock value belongs to its DB-side codomain.

    If a future migration shrinks a codomain and this module is not
    updated, this assertion fails at import time rather than at the
    next INSERT — a much friendlier signal to the developer.

    The SOURCE_QUALITY_*_VALUES tuples are imported from
    evidencefirst_shared.schemas, which is the single source of truth
    for the codomains exposed at Python level. Migration
    0007_source_quality.sql is the single source of truth at DB level.
    The shared module's docstring requires the two to stay in sync;
    this function is the local enforcement of that contract.
    """
    assert _MOCK_SOURCE_TYPE in SOURCE_QUALITY_SOURCE_TYPE_VALUES, (
        f"mock source_type {_MOCK_SOURCE_TYPE!r} not in codomain"
    )
    assert _MOCK_SOURCE_ROLE in SOURCE_QUALITY_SOURCE_ROLE_VALUES, (
        f"mock source_role {_MOCK_SOURCE_ROLE!r} not in codomain"
    )
    assert _MOCK_AUTHORITY_LEVEL in SOURCE_QUALITY_AUTHORITY_LEVEL_VALUES, (
        f"mock authority_level {_MOCK_AUTHORITY_LEVEL!r} not in codomain"
    )
    assert _MOCK_INDEPENDENCE_LEVEL in SOURCE_QUALITY_INDEPENDENCE_LEVEL_VALUES, (
        f"mock independence_level {_MOCK_INDEPENDENCE_LEVEL!r} not in codomain"
    )
    assert _MOCK_FRESHNESS in SOURCE_QUALITY_FRESHNESS_VALUES, (
        f"mock freshness {_MOCK_FRESHNESS!r} not in codomain"
    )
    assert _MOCK_CONTRADICTION_STATUS in SOURCE_QUALITY_CONTRADICTION_STATUS_VALUES, (
        f"mock contradiction_status {_MOCK_CONTRADICTION_STATUS!r} not in codomain"
    )
    assert _MOCK_OVERALL_QUALITY in SOURCE_QUALITY_OVERALL_QUALITY_VALUES, (
        f"mock overall_quality {_MOCK_OVERALL_QUALITY!r} not in codomain"
    )
    for tk, val in _MOCK_RELEVANCE_BY_TARGET.items():
        assert val in SOURCE_QUALITY_RELEVANCE_VALUES, (
            f"mock relevance for {tk!r}: {val!r} not in codomain"
        )
    for tk, val in _MOCK_EXTRACT_QUALITY_BY_TARGET.items():
        assert val in SOURCE_QUALITY_EXTRACT_QUALITY_VALUES, (
            f"mock extract_quality for {tk!r}: {val!r} not in codomain"
        )
    assert 0.0 <= _MOCK_CONFIDENCE <= 1.0, (
        f"mock confidence {_MOCK_CONFIDENCE!r} out of [0, 1]"
    )


_validate_codomain_membership()


# ---------------------------------------------------------------------------
# JSON serialization (mirrors the convention used by other worker services)
# ---------------------------------------------------------------------------
def _payload_default(o: Any) -> Any:
    """JSON encoder fallback for non-primitive payload values.

    Mirrors published_answer_lifecycle and source_loss_propagator so
    that JSONB content stays uniform across services:

      - uuid.UUID         -> canonical lowercase string;
      - bytes / bytearray -> hex string;
      - datetime.datetime -> ISO8601 in UTC with the 'Z' suffix; naive
                             timestamps are assumed to already be in
                             UTC;
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
        f"in source_quality_evaluator payloads"
    )


def _serialize_payload(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=_payload_default,
    )


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------
def _resolve_target_kind(
    evidence_span_id: uuid.UUID | None,
    document_chunk_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
) -> str | None:
    """Return the target kind string if exactly one target is set, else None.

    Returns one of "evidence_span" / "document_chunk" / "document", or
    None when zero or more than one target is supplied. The caller
    translates None into status='invalid_target'.
    """
    n_set = sum(
        1
        for v in (evidence_span_id, document_chunk_id, document_id)
        if v is not None
    )
    if n_set != 1:
        return None
    if evidence_span_id is not None:
        return "evidence_span"
    if document_chunk_id is not None:
        return "document_chunk"
    return "document"


# ---------------------------------------------------------------------------
# Target lookup helpers (also resolve canonical tenant/project for context)
# ---------------------------------------------------------------------------
# Each helper acquires a row-level lock (FOR UPDATE) on the target's parent
# row so that two concurrent invocations targeting the same row serialize
# their version_no computation. The lock is held until the caller's
# transaction commits or rolls back.


def _lock_and_load_evidence_span_scope(
    conn: Connection, *, evidence_span_id: uuid.UUID
) -> dict[str, Any] | None:
    """Lock the evidence_span row and return its document context.

    Joins: evidence_spans -> document_chunks -> document_versions ->
    uploaded_documents. The lock is on evidence_spans only because that
    is the parent row of the assessment target; the upstream rows are
    only read for scope context.
    """
    row = conn.execute(
        text(
            """
            SELECT
              es.id              AS evidence_span_id,
              ud.tenant_id       AS tenant_id,
              ud.project_id      AS project_id,
              ud.id              AS document_id,
              dv.id              AS document_version_id,
              dc.id              AS document_chunk_id
            FROM evidence_spans  es
            JOIN document_chunks    dc ON dc.id = es.document_chunk_id
            JOIN document_versions  dv ON dv.id = dc.document_version_id
            JOIN uploaded_documents ud ON ud.id = dv.document_id
            WHERE es.id = :es
            FOR UPDATE OF es
            """
        ),
        {"es": evidence_span_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _lock_and_load_document_chunk_scope(
    conn: Connection, *, document_chunk_id: uuid.UUID
) -> dict[str, Any] | None:
    """Lock the document_chunk row and return its document context."""
    row = conn.execute(
        text(
            """
            SELECT
              dc.id              AS document_chunk_id,
              ud.tenant_id       AS tenant_id,
              ud.project_id      AS project_id,
              ud.id              AS document_id,
              dv.id              AS document_version_id
            FROM document_chunks    dc
            JOIN document_versions  dv ON dv.id = dc.document_version_id
            JOIN uploaded_documents ud ON ud.id = dv.document_id
            WHERE dc.id = :dc
            FOR UPDATE OF dc
            """
        ),
        {"dc": document_chunk_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def _lock_and_load_document_scope(
    conn: Connection, *, document_id: uuid.UUID
) -> dict[str, Any] | None:
    """Lock the uploaded_documents row and return its tenant/project context."""
    row = conn.execute(
        text(
            """
            SELECT
              ud.id          AS document_id,
              ud.tenant_id   AS tenant_id,
              ud.project_id  AS project_id
            FROM uploaded_documents ud
            WHERE ud.id = :did
            FOR UPDATE OF ud
            """
        ),
        {"did": document_id},
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# Idempotency lookup
# ---------------------------------------------------------------------------
def _select_existing_by_idempotency(
    conn: Connection,
    *,
    target_kind: str,
    target_id: uuid.UUID,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Return the existing assessment for (target, idempotency_key), or None.

    The query uses the target-kind-specific column directly so it can
    exploit the partial UNIQUE index (sqa_evidence_idem_uq /
    sqa_chunk_idem_uq / sqa_document_idem_uq) for an O(log n) lookup.
    """
    if target_kind == "evidence_span":
        sql = (
            "SELECT id, version_no FROM source_quality_assessments "
            "WHERE evidence_span_id = :tid AND idempotency_key = :ik"
        )
    elif target_kind == "document_chunk":
        sql = (
            "SELECT id, version_no FROM source_quality_assessments "
            "WHERE document_chunk_id = :tid AND idempotency_key = :ik"
        )
    elif target_kind == "document":
        sql = (
            "SELECT id, version_no FROM source_quality_assessments "
            "WHERE document_id = :tid AND idempotency_key = :ik"
        )
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown target_kind {target_kind!r}")
    row = conn.execute(text(sql), {"tid": target_id, "ik": idempotency_key}).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "id": uuid.UUID(str(m["id"])),
        "version_no": int(m["version_no"]),
    }


def _select_max_version_no(
    conn: Connection,
    *,
    target_kind: str,
    target_id: uuid.UUID,
) -> int:
    """Return MAX(version_no) for the target, or 0 if no assessment exists.

    Caller MUST have already acquired the appropriate row-level lock on
    the target parent row before invoking this; otherwise the read is
    racy with respect to a concurrent inserter.
    """
    if target_kind == "evidence_span":
        sql = (
            "SELECT COALESCE(MAX(version_no), 0) "
            "FROM source_quality_assessments WHERE evidence_span_id = :tid"
        )
    elif target_kind == "document_chunk":
        sql = (
            "SELECT COALESCE(MAX(version_no), 0) "
            "FROM source_quality_assessments WHERE document_chunk_id = :tid"
        )
    elif target_kind == "document":
        sql = (
            "SELECT COALESCE(MAX(version_no), 0) "
            "FROM source_quality_assessments WHERE document_id = :tid"
        )
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown target_kind {target_kind!r}")
    return int(conn.execute(text(sql), {"tid": target_id}).scalar_one())


# ---------------------------------------------------------------------------
# INSERT helper
# ---------------------------------------------------------------------------
def _insert_assessment_row(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    target_kind: str,
    evidence_span_id: uuid.UUID | None,
    document_chunk_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    version_no: int,
    payload: dict[str, Any],
    idempotency_key: str,
    evaluator_name: str,
    evaluator_version: str,
    policy_name: str,
    policy_version: str,
) -> uuid.UUID:
    """INSERT a new source_quality_assessments row and return its id.

    The INSERT lets the table-level DEFAULT app_new_uuid() generate the
    id, mirroring how 0007_source_quality.sql is exercised by
    tests/test_migration_0007_source_quality.py.

    The CHECK constraints in the migration enforce the codomains and
    target XOR. The append-only trigger enforces immutability after
    INSERT. The partial UNIQUE indexes enforce
    (target_id, version_no) and (target_id, idempotency_key)
    uniqueness; the caller is responsible for staging values that
    respect those.
    """
    new_id_row = conn.execute(
        text(
            """
            INSERT INTO source_quality_assessments (
                tenant_id, project_id,
                evidence_span_id, document_chunk_id, document_id,
                version_no,
                source_type, source_role, authority_level, independence_level,
                freshness, relevance, extract_quality, contradiction_status,
                overall_quality, confidence,
                evaluator_name, evaluator_version,
                policy_name, policy_version,
                idempotency_key, payload
            ) VALUES (
                :tenant_id, :project_id,
                :evidence_span_id, :document_chunk_id, :document_id,
                :version_no,
                :source_type, :source_role, :authority_level, :independence_level,
                :freshness, :relevance, :extract_quality, :contradiction_status,
                :overall_quality, :confidence,
                :evaluator_name, :evaluator_version,
                :policy_name, :policy_version,
                :idempotency_key, CAST(:payload AS JSONB)
            )
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "evidence_span_id": evidence_span_id,
            "document_chunk_id": document_chunk_id,
            "document_id": document_id,
            "version_no": version_no,
            "source_type": _MOCK_SOURCE_TYPE,
            "source_role": _MOCK_SOURCE_ROLE,
            "authority_level": _MOCK_AUTHORITY_LEVEL,
            "independence_level": _MOCK_INDEPENDENCE_LEVEL,
            "freshness": _MOCK_FRESHNESS,
            "relevance": _MOCK_RELEVANCE_BY_TARGET[target_kind],
            "extract_quality": _MOCK_EXTRACT_QUALITY_BY_TARGET[target_kind],
            "contradiction_status": _MOCK_CONTRADICTION_STATUS,
            "overall_quality": _MOCK_OVERALL_QUALITY,
            "confidence": _MOCK_CONFIDENCE,
            "evaluator_name": evaluator_name,
            "evaluator_version": evaluator_version,
            "policy_name": policy_name,
            "policy_version": policy_version,
            "idempotency_key": idempotency_key,
            "payload": _serialize_payload(payload),
        },
    ).first()
    return uuid.UUID(str(new_id_row[0]))


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def assess_source_quality(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID | None = None,
    document_chunk_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    idempotency_key: str,
    evaluator_name: str = DEFAULT_EVALUATOR_NAME,
    evaluator_version: str = DEFAULT_EVALUATOR_VERSION,
    policy_name: str = DEFAULT_POLICY_NAME,
    policy_version: str = DEFAULT_POLICY_VERSION,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a mock source_quality_assessments row for the given target.

    Exactly ONE of (evidence_span_id, document_chunk_id, document_id)
    MUST be supplied. The function:

      1. Validates the target XOR. Zero or more than one target ->
         returns ``status='invalid_target'`` without any DB write.
      2. Locks the target's parent row (FOR UPDATE) and reads its
         canonical scope (tenant_id, project_id, document context).
         If the target does not exist -> returns ``status='not_found'``.
      3. Checks for an existing row with the SAME idempotency_key on
         the SAME target. If present -> returns
         ``status='already_assessed'`` with the existing id and
         version_no; NO new row is inserted.
      4. Otherwise computes ``next_version_no = MAX(version_no) + 1``
         for the target (lock held since step 2) and INSERTs a new row
         with the deterministic mock values described in the module
         docstring.

    Canonical scope contract:

        The ``tenant_id`` and ``project_id`` arguments supplied by the
        caller are accepted for back-compatibility with the original
        block-prompt signature, but they are NOT used as the values
        written to source_quality_assessments. The INSERT always uses
        the canonical ``tenant_id`` and ``project_id`` read from the
        target's parent row (evidence_span -> chunk -> version ->
        document, or chunk -> version -> document, or document
        directly). This protects the DB from caller mistakes: passing
        FK-valid but semantically wrong tenant/project values cannot
        produce an inconsistent row. The same canonical scope is
        returned to the caller so it can detect such mismatches if
        needed.

    Returns:
        {
          "status":       "assessed" | "already_assessed"
                          | "invalid_target" | "not_found",
          "assessment_id": str | None,
          "version_no":    int | None,
          "target_type":   "evidence_span" | "document_chunk" | "document" | None,
          "target_id":     str | None,
          "tenant_id":     str | None,   # canonical (from target row)
          "project_id":    str | None,   # canonical (from target row)
        }

    Concurrency contract:

        The caller MUST pass a Connection inside an explicit
        transaction. The function acquires a row-level lock on the
        target's parent row at step 2, which serializes concurrent
        appends to the assessment versioning for that target. The
        application-level idempotency check at step 3 is also
        protected by that lock.

        As a belt-and-suspenders measure, if two callers race past
        step 3 with the same idempotency_key on the same target and
        the INSERT at step 4 hits the partial UNIQUE index, the
        IntegrityError is caught and ``status='already_assessed'`` is
        returned after re-reading the existing row.

        The INSERT is wrapped in a SAVEPOINT (``conn.begin_nested()``)
        precisely so the caller's outer transaction survives the race.
        On PostgreSQL, an IntegrityError aborts the current
        transaction, and any subsequent statement on the same
        Connection fails with "current transaction is aborted" until a
        rollback. By confining the INSERT to a nested transaction, the
        savepoint rollback restores the outer transaction to a usable
        state, the recovery SELECT runs successfully, and the caller
        can keep using the same Connection.

        This function never commits and never rolls back the outer
        transaction.

    Side effects:

        Writes EXACTLY one row to source_quality_assessments on
        success. Reads (with FOR UPDATE) the appropriate target parent
        row. Does NOT touch any other table.
    """
    target_kind = _resolve_target_kind(
        evidence_span_id, document_chunk_id, document_id
    )
    if target_kind is None:
        # Either zero or more than one target was supplied. We do NOT
        # raise here; the caller gets a structured status string so
        # the function remains usable from both code paths and tests
        # without try/except gymnastics. No DB write happens.
        logger.info(
            "source_quality_evaluator.invalid_target",
            evidence_span_id_set=evidence_span_id is not None,
            document_chunk_id_set=document_chunk_id is not None,
            document_id_set=document_id is not None,
        )
        return {
            "status": STATUS_INVALID_TARGET,
            "assessment_id": None,
            "version_no": None,
            "target_type": None,
            "target_id": None,
            "tenant_id": None,
            "project_id": None,
        }

    # Resolve target_id once for downstream helpers.
    if target_kind == "evidence_span":
        target_id = evidence_span_id  # type: ignore[assignment]
        scope = _lock_and_load_evidence_span_scope(
            conn, evidence_span_id=target_id  # type: ignore[arg-type]
        )
    elif target_kind == "document_chunk":
        target_id = document_chunk_id  # type: ignore[assignment]
        scope = _lock_and_load_document_chunk_scope(
            conn, document_chunk_id=target_id  # type: ignore[arg-type]
        )
    else:  # document
        target_id = document_id  # type: ignore[assignment]
        scope = _lock_and_load_document_scope(
            conn, document_id=target_id  # type: ignore[arg-type]
        )

    if scope is None:
        logger.info(
            "source_quality_evaluator.not_found",
            target_type=target_kind,
            target_id=str(target_id),
        )
        return {
            "status": STATUS_NOT_FOUND,
            "assessment_id": None,
            "version_no": None,
            "target_type": target_kind,
            "target_id": str(target_id),
            "tenant_id": None,
            "project_id": None,
        }

    canonical_tenant_id = uuid.UUID(str(scope["tenant_id"]))
    canonical_project_id = (
        uuid.UUID(str(scope["project_id"]))
        if scope.get("project_id") is not None
        else None
    )

    # Idempotency short-circuit: if an assessment for this exact
    # (target, idempotency_key) already exists, return it without
    # writing.
    existing = _select_existing_by_idempotency(
        conn,
        target_kind=target_kind,
        target_id=target_id,  # type: ignore[arg-type]
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        logger.info(
            "source_quality_evaluator.already_assessed",
            target_type=target_kind,
            target_id=str(target_id),
            assessment_id=str(existing["id"]),
            version_no=existing["version_no"],
        )
        return {
            "status": STATUS_ALREADY_ASSESSED,
            "assessment_id": str(existing["id"]),
            "version_no": existing["version_no"],
            "target_type": target_kind,
            "target_id": str(target_id),
            "tenant_id": str(canonical_tenant_id),
            "project_id": (
                str(canonical_project_id)
                if canonical_project_id is not None
                else None
            ),
        }

    # Compute the next version_no under the lock acquired at scope load.
    next_version_no = _select_max_version_no(
        conn,
        target_kind=target_kind,
        target_id=target_id,  # type: ignore[arg-type]
    ) + 1

    # Assemble payload. The input_payload field preserves whatever the
    # caller provided so future readers can correlate the mock
    # assessment with the call context. The semantic_warning is a
    # stable constant explicitly flagging that this row does NOT
    # judge claim truth.
    full_payload: dict[str, Any] = {
        "target_type": target_kind,
        "mock": True,
        "rationale": (
            f"mock deterministic assessment for {target_kind} target; "
            "no AI / web / heuristic involved (Phase 8.7D)"
        ),
        "semantic_warning": _SEMANTIC_WARNING,
        "service_name": SERVICE_NAME,
        "service_version": SERVICE_VERSION,
    }
    if payload is not None:
        # Preserve the caller's payload verbatim under a dedicated key
        # so we never lose information passed in. We do NOT merge it
        # into the top-level dict to avoid accidental key collisions
        # with our own structural keys.
        full_payload["input_payload"] = payload

    # INSERT inside a SAVEPOINT so that a UNIQUE-violation race against
    # a concurrent inserter does NOT abort the caller's outer
    # transaction. On PostgreSQL, an IntegrityError raised outside a
    # savepoint leaves the current transaction in the aborted state,
    # and the recovery SELECT below would fail with "current
    # transaction is aborted, commands ignored until end of
    # transaction block". With conn.begin_nested() the savepoint is
    # rolled back, the outer transaction remains usable, and the
    # recovery SELECT runs normally.
    #
    # Scope written to DB is the CANONICAL scope read from the target
    # row, not what the caller passed in: this prevents an FK-valid
    # but semantically wrong tenant/project from producing an
    # inconsistent row.
    try:
        with conn.begin_nested():
            new_id = _insert_assessment_row(
                conn,
                tenant_id=canonical_tenant_id,
                project_id=canonical_project_id,
                target_kind=target_kind,
                evidence_span_id=evidence_span_id,
                document_chunk_id=document_chunk_id,
                document_id=document_id,
                version_no=next_version_no,
                payload=full_payload,
                idempotency_key=idempotency_key,
                evaluator_name=evaluator_name,
                evaluator_version=evaluator_version,
                policy_name=policy_name,
                policy_version=policy_version,
            )
    except IntegrityError as exc:
        # Belt-and-suspenders for a concurrent inserter that won the
        # race for the same idempotency_key on the same target. The
        # partial UNIQUE index (sqa_evidence_idem_uq /
        # sqa_chunk_idem_uq / sqa_document_idem_uq) raises here.
        #
        # We do NOT swallow other IntegrityErrors (e.g. a version_no
        # collision, which would only fire if the FOR UPDATE lock
        # failed to serialize correctly — that would indicate a
        # programming error elsewhere). If the recovery SELECT cannot
        # find a row matching this idempotency_key, the original
        # IntegrityError is re-raised so the test suite surfaces it.
        existing_after_race = _select_existing_by_idempotency(
            conn,
            target_kind=target_kind,
            target_id=target_id,  # type: ignore[arg-type]
            idempotency_key=idempotency_key,
        )
        if existing_after_race is None:
            raise
        logger.info(
            "source_quality_evaluator.race_resolved_to_already_assessed",
            target_type=target_kind,
            target_id=str(target_id),
            assessment_id=str(existing_after_race["id"]),
            version_no=existing_after_race["version_no"],
            integrity_error=str(exc),
        )
        return {
            "status": STATUS_ALREADY_ASSESSED,
            "assessment_id": str(existing_after_race["id"]),
            "version_no": existing_after_race["version_no"],
            "target_type": target_kind,
            "target_id": str(target_id),
            "tenant_id": str(canonical_tenant_id),
            "project_id": (
                str(canonical_project_id)
                if canonical_project_id is not None
                else None
            ),
        }

    logger.info(
        "source_quality_evaluator.assessed",
        target_type=target_kind,
        target_id=str(target_id),
        assessment_id=str(new_id),
        version_no=next_version_no,
    )

    return {
        "status": STATUS_ASSESSED,
        "assessment_id": str(new_id),
        "version_no": next_version_no,
        "target_type": target_kind,
        "target_id": str(target_id),
        "tenant_id": str(canonical_tenant_id),
        "project_id": (
            str(canonical_project_id)
            if canonical_project_id is not None
            else None
        ),
    }
