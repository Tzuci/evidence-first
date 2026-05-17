"""API routes for Phase 8.8A-READ-A — claim entailment read endpoint (read-only).

Endpoint exposed by this module:

  GET /api/v1/tasks/{task_id}/claim-entailment      (Phase 8.8A-READ-A)

Strict invariants (Phase 8.8A-READ-A — read-only observability):

  - This endpoint is COMPLETELY read-only. It MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call ``run_claim_entailment_checks`` or
        ``check_claim_entailment`` or any other worker service;
      * import worker code;
      * use Redis;
      * mutate ``claim_entailment_checks``, ``audit_records``,
        ``claim_ledger_entries``, ``claim_lineage``,
        ``claim_evidence_links``, ``logical_claims``,
        ``coverage_gap_statements``, ``final_gate_reports``,
        ``source_quality_assessments`` or any other table.

  - The endpoint surfaces exactly the rows persisted in
    ``claim_entailment_checks`` for the given task, serialized via the
    shared ``ClaimEntailmentCheckRead`` schema. JSONB ``payload`` is
    returned verbatim. MVP-0 does not yet apply RBAC redaction; this
    is acknowledged in PHASE_8_8A_PRE.md as a known debt (carried
    over from 8.6 / 8.7F).

  - 404 RESOURCE_NOT_FOUND with ``details.resource="task_masters"``
    and ``details.id=str(task_id)`` is returned when the task does
    not exist. The error envelope mirrors the convention used by the
    8.6 / 8.7F endpoints.

  - When the task exists but no claim_entailment_checks rows are
    present, the endpoint returns 200 with an empty ``items`` list.
    This is by design: tasks created before Phase 8.8A-WORKER (or
    tasks whose entailment step failed) legitimately have no
    entailment checks, and the endpoint reflects DB state truthfully
    without fabricating history. This mirrors the contract of the
    8.6 / 8.7F endpoints.

  - The endpoint DOES NOT evaluate entailment at read time. It only
    surfaces what the (mock) checker wrote. The Final Answer Gate is
    NOT modified by this block; entailment is consumed by the Gate
    separately (see 8.8A-GATE-CODE, already shipped).

  - The endpoint does NOT evaluate claim correctness. It only exposes
    claim entailment checks. The semantic boundary documented in
    PHASE_8_8A_PRE.md §3, §4 is the caller's responsibility to
    honor.

Semantic notes (preserved verbatim from PHASE_8_8A_PRE.md):

  - claim entailment evaluates the relation between a claim and an
    evidence span; it does NOT evaluate whether the claim is true in
    the world;
  - claim entailment is not evidence support (which is structural,
    via ``claim_evidence_links``);
  - claim entailment is not CVE-lite verification (which checks
    textual presence + quote_hash);
  - claim entailment is not source quality (which evaluates the
    source hosting the quote);
  - a ``contradicted`` verdict here is a LOCAL signal on a single
    (claim, evidence_span) pair, not a cross-source contradiction.

  ``payload`` is exposed verbatim, NOT redacted. A future block will
  introduce RBAC / redaction across all 8.6 / 8.7F / 8.8A read
  endpoints; this is a known debt explicitly out of scope here.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.engine import Connection

from evidencefirst_shared.errors import ErrorCode, NormalizedError
from evidencefirst_shared.schemas import ClaimEntailmentCheckRead

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["claim-entailment"])


# ---------------------------------------------------------------------------
# Existence helpers
# ---------------------------------------------------------------------------
def _task_exists(conn: Connection, task_id: uuid.UUID) -> bool:
    """Return True iff a ``task_masters`` row with the given id exists.

    Plain read-only ``SELECT 1 ... LIMIT 1``. Mirrors the convention
    adopted by every 8.6 / 8.7F endpoint.
    """
    row = conn.execute(
        text("SELECT 1 FROM task_masters WHERE id = :id LIMIT 1"),
        {"id": task_id},
    ).first()
    return row is not None


def _raise_task_not_found(task_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope used by 8.6 / 8.7F endpoints.

    Envelope shape (see ``packages/shared/evidencefirst_shared/errors.py``):

        {
          "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "task_masters not found",
            "details": {"resource": "task_masters", "id": "<uuid>"},
            ...
          }
        }
    """
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
    """Normalize a JSONB ``payload`` column to a Python dict.

    psycopg 3 returns JSONB as a native Python object, but on some
    driver/pool combinations the value may surface as a JSON string.
    The column is NOT NULL DEFAULT '{}'::jsonb at DB level
    (migrations/0009_claim_entailment_checks.sql), so the ``None``
    branch is defensive only.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _opt_uuid(value: Any) -> uuid.UUID | None:
    """Normalize an optional UUID column to a ``uuid.UUID`` or ``None``."""
    return uuid.UUID(str(value)) if value is not None else None


def _row_to_claim_entailment_check(row: Any) -> ClaimEntailmentCheckRead:
    """Map a SQLAlchemy row to the shared ``ClaimEntailmentCheckRead``.

    Field coercion notes:
      - UUID columns may surface as ``uuid.UUID`` or ``str`` depending
        on the driver/pool combination; we normalize defensively.
      - ``payload`` is JSONB; see ``_normalize_payload`` above.
      - ``confidence`` is DOUBLE PRECISION NULL in [0, 1]; coerced to
        float when present.
      - ``created_at`` is left as a ``datetime`` object so Pydantic's
        ``mode="json"`` serializer can format it consistently with the
        rest of the 8.5 / 8.6 / 8.7F surfaces.
      - ``rationale`` is TEXT NULL; surfaced verbatim (no truncation).
      - The schema's ``verdict`` field is typed as the
        ``ClaimEntailmentVerdict`` Literal alias (from
        ``packages/shared/evidencefirst_shared/schemas.py``); Pydantic
        will reject any value outside the five-element codomain
        defined in ``cec_verdict_chk`` (0009). This is a strictly
        defensive check: the DB CHECK already guarantees the invariant
        at write time.
    """
    m = row._mapping
    confidence_raw = m["confidence"]
    confidence = float(confidence_raw) if confidence_raw is not None else None
    rationale_raw = m["rationale"]
    rationale = str(rationale_raw) if rationale_raw is not None else None
    return ClaimEntailmentCheckRead(
        id=uuid.UUID(str(m["id"])),
        tenant_id=uuid.UUID(str(m["tenant_id"])),
        project_id=_opt_uuid(m["project_id"]),
        task_id=uuid.UUID(str(m["task_id"])),
        claim_logical_id=uuid.UUID(str(m["claim_logical_id"])),
        claim_ledger_entry_id=uuid.UUID(str(m["claim_ledger_entry_id"])),
        evidence_span_id=uuid.UUID(str(m["evidence_span_id"])),
        version_no=int(m["version_no"]),
        verdict=str(m["verdict"]),  # type: ignore[arg-type]
        confidence=confidence,
        checker_name=str(m["checker_name"]),
        checker_version=str(m["checker_version"]),
        policy_name=str(m["policy_name"]),
        policy_version=str(m["policy_version"]),
        idempotency_key=str(m["idempotency_key"]),
        rationale=rationale,
        payload=_normalize_payload(m["payload"]),
        created_at=m["created_at"],
    )


# ---------------------------------------------------------------------------
# Read query
# ---------------------------------------------------------------------------
def _select_checks_for_task(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    limit: int,
) -> list[Any]:
    """Fetch all claim_entailment_checks rows targeting the given task.

    Ordering: ``(created_at DESC, id DESC)``.

    Rationale:
      - Most-recent checks surface first, with ``id DESC`` as a
        deterministic tie-breaker when two rows share the same
        ``created_at``.
      - In MVP-0 ``version_no`` is fixed to 1 by the mock checker, so
        this read endpoint does not implement latest-per-pair
        grouping.
      - The query uses only bound params for runtime values.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              id,
              tenant_id,
              project_id,
              task_id,
              claim_logical_id,
              claim_ledger_entry_id,
              evidence_span_id,
              version_no,
              verdict,
              confidence,
              checker_name,
              checker_version,
              policy_name,
              policy_version,
              idempotency_key,
              rationale,
              payload,
              created_at
            FROM claim_entailment_checks
            WHERE task_id = :tid
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"tid": task_id, "limit": limit},
    ).fetchall()
    return list(rows)


# ---------------------------------------------------------------------------
# Endpoint — GET /api/v1/tasks/{task_id}/claim-entailment
# ---------------------------------------------------------------------------
@router.get("/tasks/{task_id}/claim-entailment")
def list_task_claim_entailment_checks(
    task_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=2000),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """List claim entailment checks for a single task (read-only).

    Behavior:
      - 404 RESOURCE_NOT_FOUND with ``details.resource="task_masters"``
        and ``details.id=str(task_id)`` if the task does not exist.
        The check is performed BEFORE the entailment SELECT, so a
        client probing for a bogus id receives an immediate
        not-found rather than a misleading empty list.
      - 200 with ``items=[]`` if the task exists but no
        claim_entailment_checks rows have been written for it
        (legitimate case for tasks created before Phase 8.8A-WORKER
        or for tasks whose entailment step failed; no backfill is
        performed here).
      - 200 with the list of matching rows, ordered DESC by
        ``(created_at, id)``, truncated to ``limit`` rows.

    Wrapper shape::

        {
          "task_id": "<uuid>",
          "items": [<ClaimEntailmentCheckRead JSON>, ...]
        }

    Strict scope reminder (PHASE_8_8A_PRE.md §3, §4):
      - This endpoint surfaces metadata about claim ↔ quote semantic
        entailment. It does NOT evaluate claim truth, evidence
        support, CVE-lite verification, source quality, source loss,
        or final publication eligibility. The Final Answer Gate is
        NOT consulted here.
      - All JSONB ``payload`` content is returned verbatim; RBAC
        redaction is not applied in MVP-0.
      - ``confidence`` is an internal score in [0, 1] (or NULL); it
        is NEVER intended as a single-number truth score and MUST
        NOT be consumed as such by downstream agents.
    """
    if not _task_exists(conn, task_id):
        _raise_task_not_found(task_id)
        # _raise_task_not_found never returns; the explicit ``raise``
        # keeps static analyzers happy without ``# type: ignore``.
        raise AssertionError("unreachable")

    rows = _select_checks_for_task(conn, task_id=task_id, limit=limit)
    items = [_row_to_claim_entailment_check(r) for r in rows]

    return {
        "task_id": str(task_id),
        "items": [it.model_dump(mode="json") for it in items],
    }
