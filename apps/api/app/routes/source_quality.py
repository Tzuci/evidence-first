"""API routes for Phase 8.7F — source quality read endpoints (read-only).

Endpoints exposed by this module:

  GET /api/v1/evidence-spans/{evidence_span_id}/source-quality      (Phase 8.7F)
  GET /api/v1/tasks/{task_id}/source-quality                        (Phase 8.7F)

Strict invariants (Phase 8.7F — read-only observability):

  - These endpoints are COMPLETELY read-only. They MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call ``assess_source_quality`` or
        ``run_source_quality_assessment`` or any other worker service;
      * import worker code;
      * use Redis;
      * mutate ``source_quality_assessments``, ``audit_records``,
        ``claim_ledger_entries``, ``claim_lineage``,
        ``claim_evidence_links``, ``logical_claims``,
        ``published_answers``, ``source_loss_events``,
        ``source_loss_propagation_records`` or any other table.

  - The endpoints surface exactly the rows persisted in
    ``source_quality_assessments`` for the given target / task,
    serialized via the shared ``SourceQualityAssessmentRead`` schema.
    JSONB ``payload`` is returned verbatim. MVP-0 does not yet apply
    RBAC redaction; this is acknowledged in PHASE_8_7_PLAN.md §13 as
    a known debt (carried over from 8.6).

  - 404 RESOURCE_NOT_FOUND with the appropriate ``details.resource``
    and ``details.id`` is returned when the parent entity (the
    evidence_span for endpoint 1, the task_master for endpoint 2)
    does not exist. The error envelope mirrors the convention used
    by the 8.6 endpoints.

  - When the parent entity exists but no assessments are present,
    the endpoint returns 200 with an empty ``items`` list and
    ``latest_assessment: null``. This is by design: tasks created
    before Phase 8.7E (or tasks whose source-quality step failed)
    legitimately have no assessment for their spans, and the
    endpoint reflects DB state truthfully without fabricating
    history. This mirrors the contract of the 8.6 lifecycle and
    propagation endpoints.

  - The endpoints DO NOT evaluate source quality at read time. They
    only surface what the (mock) evaluator wrote. The Final Answer
    Gate is NOT modified by this block; it continues to use the
    "verified-backed" rule from 8.4. The Source Quality Evaluator
    remains deterministic mock; in particular every assessment in
    the wild today carries ``overall_quality='unknown'``.

  - The endpoint does NOT distinguish between "source quality" and
    "claim correctness". The semantic boundary documented in
    PHASE_8_7_PLAN.md §3 is the caller's responsibility to honor.

Semantic notes carried over from PHASE_8_7_PLAN.md:

  - source quality is NOT claim correctness;
  - source quality is NOT evidence support;
  - source quality is NOT verification outcome;
  - source quality is NOT source loss;
  - source quality is NOT final publication eligibility.

  These endpoints surface metadata about the structural quality of
  sources used to back claims; they do NOT judge whether the claims
  themselves are true.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import (
    SOURCE_QUALITY_OVERALL_QUALITY_VALUES,
    SourceQualityAssessmentRead,
)

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["source-quality"])


# ---------------------------------------------------------------------------
# Existence helpers
# ---------------------------------------------------------------------------
def _evidence_span_exists(
    conn: Connection, evidence_span_id: uuid.UUID
) -> bool:
    """Return True iff an evidence_spans row with the given id exists.

    Read-only: a plain ``SELECT 1 ... LIMIT 1`` is sufficient. The
    endpoint never mutates DB state, so row-level locking would be
    wasteful here. Mirrors the convention adopted by all 8.6
    endpoints.
    """
    row = conn.execute(
        text("SELECT 1 FROM evidence_spans WHERE id = :id LIMIT 1"),
        {"id": evidence_span_id},
    ).first()
    return row is not None


def _task_exists(conn: Connection, task_id: uuid.UUID) -> bool:
    """Return True iff a task_masters row with the given id exists."""
    row = conn.execute(
        text("SELECT 1 FROM task_masters WHERE id = :id LIMIT 1"),
        {"id": task_id},
    ).first()
    return row is not None


def _raise_evidence_span_not_found(evidence_span_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope expected by callers.

    Envelope shape mirrors the helper used in routes/lifecycle_events.py
    and routes/source_loss.py so that clients can rely on the same
    ``details.resource``/``details.id`` contract across all 8.6/8.7
    endpoints.
    """
    raise NormalizedError(
        code=ErrorCode.RESOURCE_NOT_FOUND,
        message="evidence_spans not found",
        details={
            "resource": "evidence_spans",
            "id": str(evidence_span_id),
        },
        http_status=404,
    )


def _raise_task_not_found(task_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope expected by callers."""
    raise NormalizedError(
        code=ErrorCode.RESOURCE_NOT_FOUND,
        message="task_masters not found",
        details={
            "resource": "task_masters",
            "id": str(task_id),
        },
        http_status=404,
    )


# ---------------------------------------------------------------------------
# Row coercion
# ---------------------------------------------------------------------------
def _normalize_payload(value: Any) -> dict[str, Any]:
    """Normalize a JSONB payload column to a Python dict.

    psycopg 3 returns JSONB as a native Python object, but on some
    driver / pool combinations the value may surface as a JSON string.
    The column is NOT NULL DEFAULT '{}'::jsonb at DB level, so the
    None branch is defensive only.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _opt_uuid(value: Any) -> uuid.UUID | None:
    """Normalize an optional UUID column to a uuid.UUID or None."""
    return uuid.UUID(str(value)) if value is not None else None


def _row_to_source_quality_assessment(row: Any) -> SourceQualityAssessmentRead:
    """Map a SQLAlchemy row to the shared SourceQualityAssessmentRead.

    Field coercion notes:
      - UUID columns may surface as ``uuid.UUID`` or ``str`` depending
        on the driver/pool combination; we normalize via
        ``uuid.UUID(str(...))`` defensively.
      - ``payload`` is JSONB; see ``_normalize_payload`` above.
      - ``confidence`` is DOUBLE PRECISION NULL in [0, 1]; coerced to
        float when present.
      - ``created_at`` is left as a ``datetime`` object so Pydantic's
        ``mode="json"`` serializer can format it consistently with the
        rest of the 8.5/8.6 surfaces.
      - The three target columns are surfaced verbatim, including
        NULLs: the DB CHECK ``sqa_target_xor`` guarantees that
        exactly one of them is non-null per row, so consumers can
        rely on that invariant.
    """
    m = row._mapping
    confidence_raw = m["confidence"]
    confidence = float(confidence_raw) if confidence_raw is not None else None
    return SourceQualityAssessmentRead(
        id=uuid.UUID(str(m["id"])),
        tenant_id=uuid.UUID(str(m["tenant_id"])),
        project_id=_opt_uuid(m["project_id"]),
        evidence_span_id=_opt_uuid(m["evidence_span_id"]),
        document_chunk_id=_opt_uuid(m["document_chunk_id"]),
        document_id=_opt_uuid(m["document_id"]),
        version_no=int(m["version_no"]),
        source_type=str(m["source_type"]),
        source_role=str(m["source_role"]),
        authority_level=str(m["authority_level"]),
        independence_level=str(m["independence_level"]),
        freshness=str(m["freshness"]),
        relevance=str(m["relevance"]),
        extract_quality=str(m["extract_quality"]),
        contradiction_status=str(m["contradiction_status"]),
        overall_quality=str(m["overall_quality"]),
        confidence=confidence,
        evaluator_name=str(m["evaluator_name"]),
        evaluator_version=str(m["evaluator_version"]),
        policy_name=str(m["policy_name"]),
        policy_version=str(m["policy_version"]),
        idempotency_key=str(m["idempotency_key"]),
        payload=_normalize_payload(m["payload"]),
        created_at=m["created_at"],
    )


# ---------------------------------------------------------------------------
# Read queries
# ---------------------------------------------------------------------------
# The three SELECTs below all use sqlalchemy.text() with strictly bound
# parameters. No string interpolation, no f-string SQL.

_ASSESSMENT_COLUMNS_SQL = """
    id,
    tenant_id,
    project_id,
    evidence_span_id,
    document_chunk_id,
    document_id,
    version_no,
    source_type,
    source_role,
    authority_level,
    independence_level,
    freshness,
    relevance,
    extract_quality,
    contradiction_status,
    overall_quality,
    confidence,
    evaluator_name,
    evaluator_version,
    policy_name,
    policy_version,
    idempotency_key,
    payload,
    created_at
"""


def _select_assessments_for_evidence_span(
    conn: Connection,
    *,
    evidence_span_id: uuid.UUID,
    limit: int,
) -> list[Any]:
    """Fetch all assessments targeting the given evidence_span.

    Ordering: (version_no ASC, created_at ASC, id ASC). This matches
    the block prompt exactly. version_no is monotonically increasing
    per target (enforced by the partial UNIQUE
    ``sqa_evidence_version_uq``), so version_no ASC alone would
    already be deterministic; the secondary keys are a defensive
    tie-breaker.

    The caller is expected to have already verified that the
    evidence_span exists (via ``_evidence_span_exists``); this helper
    returns an empty list both when no span exists AND when the span
    exists but has no assessments. The 404/200-empty distinction is
    the caller's job.
    """
    sql = text(
        f"""
        SELECT
{_ASSESSMENT_COLUMNS_SQL}
        FROM source_quality_assessments
        WHERE evidence_span_id = :esid
        ORDER BY version_no ASC, created_at ASC, id ASC
        LIMIT :limit
        """
    )
    rows = conn.execute(
        sql,
        {"esid": evidence_span_id, "limit": limit},
    ).fetchall()
    return list(rows)


def _select_task_evidence_span_ids(
    conn: Connection,
    *,
    task_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return DISTINCT evidence_span_id rows linked to the task.

    Same logic as the worker's
    ``source_quality_orchestrator._select_distinct_evidence_span_ids``:
    we join ``claim_evidence_links`` to ``logical_claims`` and filter
    on ``lc.task_id`` plus a defensive
    ``cel.evidence_span_id IS NOT NULL`` predicate (honors the
    ``cel_origin_xor`` CHECK from 0004_claim_ledger.sql, which
    permits ``retrieved_source_span_id`` rows in principle even if
    the closed-corpus pipeline does not produce them today).

    Ordering by ``evidence_span_id`` ASC makes the iteration
    deterministic so the response keeps a stable shape across
    invocations even when the same span is reachable via multiple
    claims.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT cel.evidence_span_id
            FROM claim_evidence_links cel
            JOIN logical_claims lc ON lc.id = cel.claim_logical_id
            WHERE lc.task_id = :task_id
              AND cel.evidence_span_id IS NOT NULL
            ORDER BY cel.evidence_span_id ASC
            """
        ),
        {"task_id": task_id},
    ).fetchall()
    return [uuid.UUID(str(r._mapping["evidence_span_id"])) for r in rows]


# ---------------------------------------------------------------------------
# Per-span item assembly (shared by both endpoints)
# ---------------------------------------------------------------------------
def _build_span_item(
    conn: Connection,
    *,
    evidence_span_id: uuid.UUID,
    limit: int,
) -> dict[str, Any]:
    """Build the per-span response payload.

    Shape::

        {
          "evidence_span_id": "<uuid>",
          "latest_assessment": <SourceQualityAssessmentRead JSON> | null,
          "items": [<SourceQualityAssessmentRead JSON>, ...]
        }

    ``latest_assessment`` semantics:
      - ``null`` if ``items`` is empty;
      - otherwise the LAST element of ``items``. With the ASC
        ordering by version_no this is also the highest version_no
        in the returned set. When the caller truncates the result
        via ``limit``, ``latest_assessment`` reflects the latest
        among the RETURNED items, not the latest globally for the
        span. This is the simplest, most coherent semantics for a
        limit-only pagination and matches the block prompt's
        recommendation ("latest_assessment = ultimo tra gli items
        restituiti").

    Implementation note:
      We do not run an extra query to find the "true" latest. The
      partial UNIQUE index ``sqa_evidence_version_uq`` plus the ASC
      ordering already give us a deterministic answer with a single
      SELECT.
    """
    rows = _select_assessments_for_evidence_span(
        conn,
        evidence_span_id=evidence_span_id,
        limit=limit,
    )
    items_models = [_row_to_source_quality_assessment(r) for r in rows]
    items_json = [it.model_dump(mode="json") for it in items_models]

    latest_json: dict[str, Any] | None
    if items_json:
        # Latest within the returned slice. ASC ordering ⇒ last
        # element. Re-serialize the last model rather than indexing
        # the items list so we have a single place that decides the
        # latest semantic (defensive against future ordering changes
        # that might decouple "last in items" from "latest_assessment").
        latest_json = items_models[-1].model_dump(mode="json")
    else:
        latest_json = None

    return {
        "evidence_span_id": str(evidence_span_id),
        "latest_assessment": latest_json,
        "items": items_json,
    }


# ---------------------------------------------------------------------------
# Summary helpers (task endpoint)
# ---------------------------------------------------------------------------
def _init_overall_quality_counts() -> dict[str, int]:
    """Return a zero-initialized counter dict for the overall_quality codomain.

    Every value in ``SOURCE_QUALITY_OVERALL_QUALITY_VALUES`` is
    present as a key, so consumers can always read every bucket
    unconditionally. The codomain is sourced from the shared
    constants (single source of truth for the DB CHECK).

    Note on the block prompt: the prompt example used the keys
    ``{high, medium, low, unknown, not_applicable}``, but those are
    values of ``authority_level``, NOT of ``overall_quality``. The
    actual codomain of ``overall_quality`` is
    ``{strong, adequate, weak, unsuitable, unknown}`` (see
    migrations/0007_source_quality.sql, ``sqa_overall_quality_chk``).
    We honor the actual codomain to keep the summary semantically
    correct: ``latest_overall_quality_counts`` must count
    ``overall_quality`` values.
    """
    return {value: 0 for value in SOURCE_QUALITY_OVERALL_QUALITY_VALUES}


def _build_task_summary(span_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate counters across span items for the task endpoint.

    Shape::

        {
          "evidence_spans_total":      <int>,
          "spans_with_assessment":     <int>,
          "spans_without_assessment":  <int>,
          "latest_overall_quality_counts": { <overall_quality>: <int>, ... }
        }

    Counting rules (from the block prompt):
      - ``evidence_spans_total`` = number of distinct evidence_spans
        linked to the task at call time.
      - ``spans_with_assessment`` = number of those for which
        ``latest_assessment`` is non-null.
      - ``spans_without_assessment`` = total − with.
      - ``latest_overall_quality_counts`` counts ONLY the latest
        assessment per span, NOT every assessment. This makes the
        summary "what is the current per-span verdict" rather than
        "how many assessments of each kind exist".
      - The counter dict is always fully initialized (every codomain
        key present with value 0 by default), even when zero spans
        have a latest_assessment.

    Defensive note: if a future evaluator writes an
    ``overall_quality`` value not yet in the codomain (which would
    only be possible after a 0007 amendment that updates the shared
    constants), we'd silently drop the count. We log no warning here
    because the endpoint is read-only and surfacing such anomalies
    is the job of the eval/audit layer, not the read API.
    """
    total = len(span_items)
    with_assessment = 0
    counts = _init_overall_quality_counts()
    for item in span_items:
        latest = item["latest_assessment"]
        if latest is None:
            continue
        with_assessment += 1
        oq = latest.get("overall_quality")
        if oq in counts:
            counts[oq] += 1
    return {
        "evidence_spans_total": total,
        "spans_with_assessment": with_assessment,
        "spans_without_assessment": total - with_assessment,
        "latest_overall_quality_counts": counts,
    }


# ---------------------------------------------------------------------------
# Endpoint 1 — GET /api/v1/evidence-spans/{evidence_span_id}/source-quality
# ---------------------------------------------------------------------------
@router.get("/evidence-spans/{evidence_span_id}/source-quality")
def get_evidence_span_source_quality(
    evidence_span_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit: int = Query(default=100, ge=1, le=5000),
) -> dict[str, Any]:
    """List source quality assessments for a single evidence_span (read-only).

    Behavior:
      - 404 RESOURCE_NOT_FOUND with details.resource="evidence_spans"
        if the evidence_span does not exist. The check is performed
        BEFORE the assessment SELECT, so a client probing for a
        bogus id receives an immediate not-found rather than a
        misleading empty list.
      - 200 with ``items=[]`` and ``latest_assessment=null`` if the
        evidence_span exists but no assessment has been written for
        it (legitimate case for spans created before Phase 8.7E or
        for spans whose source-quality step failed; no backfill is
        performed here).
      - 200 with the list of matching rows, ordered ASC by
        (version_no, created_at, id), truncated to ``limit`` rows.
        ``latest_assessment`` is the last element of the returned
        items (i.e. the highest version_no in the returned slice).

    Wrapper shape::

        {
          "evidence_span_id": "<uuid>",
          "latest_assessment": <SourceQualityAssessmentRead JSON> | null,
          "items": [<SourceQualityAssessmentRead JSON>, ...]
        }

    Strict scope reminder (PHASE_8_7_PLAN.md §3):
      - This endpoint surfaces metadata about source quality. It does
        NOT evaluate claim truth, evidence support, verification
        outcome, source loss, or final publication eligibility. The
        Final Answer Gate is NOT modified by this block.
      - All JSONB ``payload`` content is returned verbatim; RBAC
        redaction is not applied in MVP-0.
    """
    if not _evidence_span_exists(conn, evidence_span_id):
        _raise_evidence_span_not_found(evidence_span_id)
        # _raise_evidence_span_not_found never returns; the explicit
        # ``raise`` keeps static analyzers happy without
        # ``# type: ignore``.
        raise AssertionError("unreachable")

    return _build_span_item(
        conn,
        evidence_span_id=evidence_span_id,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Endpoint 2 — GET /api/v1/tasks/{task_id}/source-quality
# ---------------------------------------------------------------------------
@router.get("/tasks/{task_id}/source-quality")
def get_task_source_quality(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
    limit_per_span: int = Query(default=100, ge=1, le=5000),
) -> dict[str, Any]:
    """List source quality assessments per evidence_span linked to a task
    (read-only).

    Behavior:
      - 404 RESOURCE_NOT_FOUND with details.resource="task_masters"
        if the task does not exist. The check is performed BEFORE
        any join, so a client probing for a bogus id receives an
        immediate not-found rather than a misleading empty list.
      - 200 with ``items=[]`` and the summary's totals at zero if
        the task exists but has no ``claim_evidence_links``
        connecting it to any evidence_span.
      - 200 with one element per distinct evidence_span linked to
        the task. Spans without any assessment in
        ``source_quality_assessments`` (e.g. tasks pre-8.7E, or
        tasks whose source-quality step failed) are still surfaced
        with ``latest_assessment=null`` and ``items=[]``: hiding
        them would obscure the difference between "no claims" and
        "claims without assessment".

    Wrapper shape::

        {
          "task_id": "<uuid>",
          "items": [
            {
              "evidence_span_id": "<uuid>",
              "latest_assessment": <SourceQualityAssessmentRead JSON> | null,
              "items": [<SourceQualityAssessmentRead JSON>, ...]
            },
            ...
          ],
          "summary": {
            "evidence_spans_total":      <int>,
            "spans_with_assessment":     <int>,
            "spans_without_assessment":  <int>,
            "latest_overall_quality_counts": {
              "strong":     <int>,
              "adequate":   <int>,
              "weak":       <int>,
              "unsuitable": <int>,
              "unknown":    <int>
            }
          }
        }

    Implementation note — N+1 query:
      We loop over the distinct ``evidence_span_id`` values returned
      by ``_select_task_evidence_span_ids`` and call
      ``_select_assessments_for_evidence_span`` for each. This is
      O(N+1) where N is the number of spans linked to the task. The
      block prompt acknowledges this explicitly as acceptable for
      MVP-0 (number of spans per task is bounded and small in
      practice). A future optimization could batch via
      ``ANY(:span_ids)`` or a CTE, but the simpler form keeps test
      seams clean and avoids SQLAlchemy/psycopg array-binding
      pitfalls. This is documented in the block's residual risks.

    Ordering:
      - Spans are ordered by ``evidence_span_id`` ASC (delivered by
        ``_select_task_evidence_span_ids``).
      - Assessments within each span are ordered by
        ``(version_no ASC, created_at ASC, id ASC)``.

    Strict scope reminder (PHASE_8_7_PLAN.md §3):
      - The summary's ``latest_overall_quality_counts`` is computed
        on the LATEST assessment per span only, not on every
        assessment. This matches the semantics of "what is the
        current verdict for each span".
      - The counter is always fully initialized: every key in
        ``SOURCE_QUALITY_OVERALL_QUALITY_VALUES`` is present, with
        value 0 by default. Today's mock evaluator always writes
        ``overall_quality='unknown'``, so in practice only the
        ``unknown`` counter will be populated; future evaluators
        will fill the other buckets.
      - The Final Answer Gate is NOT modified. This endpoint is
        purely observational.
    """
    if not _task_exists(conn, task_id):
        _raise_task_not_found(task_id)
        # _raise_task_not_found never returns.
        raise AssertionError("unreachable")

    span_ids = _select_task_evidence_span_ids(conn, task_id=task_id)

    items: list[dict[str, Any]] = []
    for span_id in span_ids:
        items.append(
            _build_span_item(
                conn,
                evidence_span_id=span_id,
                limit=limit_per_span,
            )
        )

    summary = _build_task_summary(items)

    return {
        "task_id": str(task_id),
        "items": items,
        "summary": summary,
    }
