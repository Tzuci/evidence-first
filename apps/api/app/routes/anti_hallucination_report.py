"""API routes for Phase 8.8B-REPORT — Anti-Hallucination Report (read-only).

Endpoint exposed by this module:

  GET /api/v1/tasks/{task_id}/anti-hallucination-report      (Phase 8.8B-REPORT-CODE-A + CODE-B)

Strict invariants (Phase 8.8B-REPORT — read-only aggregated view):

  - This endpoint is COMPLETELY read-only. It MUST NOT:
      * INSERT / UPDATE / DELETE any row in any table;
      * call any worker service or import worker code;
      * use Redis;
      * recompute the Final Answer Gate decision;
      * re-evaluate claims, sources, entailment, source quality, CVE-lite;
      * substitute the append-only tables or the specialist read APIs
        (8.4 / 8.6 / 8.7F / 8.8A-READ-A).

  - The endpoint surfaces a derived task-level VIEW that aggregates a
    handful of facts already persisted by the existing append-only
    tables. JSONB ``payload`` / ``details`` are returned VERBATIM. RBAC
    redaction is NOT applied in MVP-0 (acknowledged debt).

  - 404 RESOURCE_NOT_FOUND with ``details.resource="task_masters"`` and
    ``details.id=str(task_id)`` is returned when the task does not
    exist. Mirrors the convention used by every other 8.6 / 8.7F /
    8.8A-READ-A endpoint.

  - When the task exists but no draft / gate / published rows are
    present, the endpoint returns 200 with sensible empty / null
    fields. Specifically:
        * ``publication.status`` derived deterministically per §8.5 of
          PHASE_8_8B_REPORT_PRE.md (e.g. "not_ready" when neither a
          gate report nor a published answer exist);
        * ``gate.decision`` and ``gate.reason_code`` set to None;
        * ``claims`` and ``evidence`` populated in CODE-B (one entry
          per logical_claim of the task; one entry per evidence_span
          attached via task_documents);
        * ``axis_summary.final_gate`` derived from
          ``coverage_gap_statements`` collected on the latest draft;
        * ``axis_summary.cve_lite``, ``axis_summary.source_quality``,
          ``axis_summary.claim_entailment`` populated in CODE-B from
          claim-level aggregation;
        * ``mock_indicators`` reflect provenance from real rows where
          present, plus the MVP-0 fallback values for axes with no
          data yet;
        * ``limitations`` is always populated with the disclaimers
          documented in PHASE_8_8B_REPORT_PRE.md §9.

Semantic notes (preserved verbatim from PHASE_8_8B_REPORT_PRE.md):

  - The Anti-Hallucination Report is a derived read-only view for UI
    and human audit. It does NOT introduce new decisions.
  - One cited source does NOT imply a true claim.
  - A textually present quote does NOT imply the quote supports the
    claim.
  - An ``entailed`` verdict does NOT imply the claim is true in the
    world.
  - The JSONB payload is exposed verbatim; RBAC / redaction is a known
    debt.

CODE-B (this block) implements:
  - claim-level entries in ``claims``;
  - evidence rows in ``evidence``;
  - CVE-lite verification record aggregation (per latest entry);
  - Source Quality latest-per-target aggregation (over spans linked
    to claims);
  - Claim Entailment latest-per-pair aggregation (over (entry, span)
    pairs derived from claim_evidence_links);
  - axis_summary completed for cve_lite / source_quality /
    claim_entailment, with missing_count semantics restricted to
    claim-linked spans / pairs (PHASE_8_8B_REPORT_PRE.md §5.2).

CODE-B explicitly DOES NOT:
  - alter the Final Answer Gate or any worker code;
  - introduce new tables or migrations;
  - compute or expose lifecycle / source-loss event details
    (deferred);
  - apply RBAC / redaction to JSONB payload content (deferred).
"""
from __future__ import annotations

import datetime as _dt_mod
import json
import uuid
from typing import Any, Iterable

from fastapi import APIRouter, Depends
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from sqlalchemy.types import Uuid as _SAUuid

from evidencefirst_shared.errors import ErrorCode, NormalizedError

from ..db import get_conn


router = APIRouter(prefix="/api/v1", tags=["anti-hallucination-report"])


# ---------------------------------------------------------------------------
# Constants — mock indicator detection
# ---------------------------------------------------------------------------
# Identity of the mock compiler currently shipped (see
# apps/worker/app/services/compiler.py). When a real compiler is wired
# in the future, its compiler_name will differ and
# ``uses_mock_compiler`` will flip to False without further changes.
_MOCK_COMPILER_NAME = "mvp0_compiler_v1"

# Identity of the mock CVE-lite check (see
# apps/worker/app/services/cve_lite.py). verification_records with
# check_kind='cve_lite' AND check_name == this value are mock-driven.
_MOCK_CVE_LITE_CHECK_NAME = "quote_hash_and_substring_v1"

# Identity of the mock claim entailment checker (see
# apps/worker/app/services/claim_entailment_checker.py).
_MOCK_ENTAILMENT_CHECKER_NAME = "mvp0_mock_entailment_checker"

# Identity of the mock source quality evaluator (see
# apps/worker/app/services/source_quality_evaluator.py).
_MOCK_SOURCE_QUALITY_EVALUATOR_NAME = "mock_source_quality_evaluator"


# Mapping from coverage_gap_statements.kind to the report's derived
# ``axis`` decoration. PHASE_8_8B_REPORT_PRE.md §8 spells this out
# explicitly. ``axis`` is a presentation-layer concern; the DB-level
# ``kind`` remains authoritative.
_COVERAGE_GAP_AXIS_BY_KIND: dict[str, str] = {
    "missing_evidence": "coverage",
    "unverified_claim": "cve_lite",
    "out_of_scope": "coverage",
    "source_loss": "source_loss",
    "source_quality_block": "source_quality",
    "source_quality_warning": "source_quality",
    "entailment_block": "claim_entailment",
    "entailment_warning": "claim_entailment",
}
_COVERAGE_GAP_AXIS_FALLBACK = "other"


# Ordering rank for coverage gaps. The block prompt requires
# severity-first: block (highest priority, rendered first) > warn > info.
# Other unknown severities sort last; this is defensive and never
# expected today given the CHECK constraint declared in 0005.
_COVERAGE_GAP_SEVERITY_RANK: dict[str, int] = {
    "block": 0,
    "warn": 1,
    "info": 2,
}
_COVERAGE_GAP_SEVERITY_FALLBACK_RANK = 99


# Source Quality codomain values (mirrors 0007 CHECK + shared
# constants). Hardcoded here so the route module does not import any
# additional shared symbol just for the counters; kept in sync with the
# DB CHECK by the same review discipline that maintains
# evidencefirst_shared.schemas.
_SOURCE_QUALITY_OVERALL_QUALITY_VALUES = (
    "strong",
    "adequate",
    "weak",
    "unsuitable",
    "unknown",
)

# Claim Entailment verdict codomain (mirrors 0009 CHECK + shared
# constants).
_CLAIM_ENTAILMENT_VERDICT_VALUES = (
    "entailed",
    "partially_supported",
    "not_supported",
    "contradicted",
    "uncertain",
)


# ---------------------------------------------------------------------------
# Generic coercion helpers
# ---------------------------------------------------------------------------
def _normalize_jsonb(value: Any) -> Any:
    """Normalize a JSONB column value to a JSON-serializable Python object.

    psycopg 3 returns JSONB as a native Python object (dict/list/scalar),
    but on some driver/pool combinations the value may surface as a JSON
    string. The function accepts either form. ``None`` is preserved so
    callers can distinguish "no payload" from "empty object".

    The returned value is NOT deep-copied: callers must not mutate it.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            # Defensive: a JSONB column declared NOT NULL DEFAULT
            # '{}'::jsonb should not surface as a non-JSON string, but
            # if it does we return the raw string rather than crash.
            return value
    return value


def _dt(value: Any) -> str | None:
    """Coerce a datetime-like value to its ISO 8601 string form.

    Returns ``None`` when the input is ``None``. Accepts both
    ``datetime`` objects (the typical case from SQLAlchemy) and
    already-stringified values (defensive).
    """
    if value is None:
        return None
    if isinstance(value, _dt_mod.datetime):
        return value.isoformat()
    return str(value)


def _uuid_str(value: Any) -> str | None:
    """Coerce a UUID-like value to its canonical string form.

    Returns ``None`` when the input is ``None``. Accepts both
    ``uuid.UUID`` and strings.
    """
    if value is None:
        return None
    return str(value)


def _row_dict(row: Any) -> dict[str, Any]:
    """Map a SQLAlchemy Row to a plain dict via the row's ``_mapping``.

    Centralizing this conversion keeps the SELECTs below readable and
    makes it trivial to switch to a different driver in the future.
    """
    return dict(row._mapping)


def _is_payload_mock(payload: Any) -> bool:
    """Return True iff the JSONB ``payload`` dict has ``mock: true``.

    Defensive against the payload arriving as a JSON-encoded string
    (some driver/pool combinations) or as ``None``. Any non-bool value
    at the ``mock`` key is treated as False — we only flag a row as
    mock if the writer made a deliberate, explicit assertion.
    """
    obj = _normalize_jsonb(payload)
    if not isinstance(obj, dict):
        return False
    return obj.get("mock") is True


# ---------------------------------------------------------------------------
# Existence / 404
# ---------------------------------------------------------------------------
def _raise_task_not_found(task_id: uuid.UUID) -> None:
    """Raise the normalized 404 envelope used by 8.6 / 8.7F / 8.8A-READ-A.

    Envelope shape (see packages/shared/evidencefirst_shared/errors.py):

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


def _task_or_404(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any]:
    """Fetch the ``task_masters`` row for ``task_id`` or raise 404.

    Returns a dict with: id, tenant_id, project_id, status, objective,
    mode, created_at, updated_at. We deliberately select only the
    columns the report needs: published_at / archived_at and other
    metadata stay outside CODE-A's surface area.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id,
              tenant_id,
              project_id,
              status,
              objective,
              mode,
              created_at,
              updated_at
            FROM task_masters
            WHERE id = :task_id
            """
        ),
        {"task_id": task_id},
    ).first()
    if row is None:
        _raise_task_not_found(task_id)
        raise AssertionError("unreachable")
    return _row_dict(row)


# ---------------------------------------------------------------------------
# SELECTs — latest draft / latest gate report / latest published answer
# ---------------------------------------------------------------------------
def _select_latest_draft(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the latest ``draft_final_answers`` for the task, or None.

    Ordering: ``version_no DESC, created_at DESC, id DESC``. The
    UNIQUE constraint ``(task_id, version_no)`` guarantees the highest
    ``version_no`` is unique per task; the secondary keys are a
    defensive tie-breaker.

    Today the compiler emits only v1 per task (see PROJECT_STATE.md);
    the defensive ordering future-proofs the helper without changing
    behavior in MVP-0.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id,
              task_id,
              version_no,
              compiler_name,
              compiler_version,
              summary_text,
              payload,
              created_at
            FROM draft_final_answers
            WHERE task_id = :task_id
            ORDER BY version_no DESC, created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"task_id": task_id},
    ).first()
    return _row_dict(row) if row is not None else None


def _select_final_gate_report(
    conn: Connection, draft_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the latest ``final_gate_reports`` for the given draft.

    ``final_gate_reports`` carries UNIQUE (draft_final_answer_id) at
    DB level, so at most one row matches. The ``ORDER BY created_at
    DESC, id DESC`` is purely defensive in case a future migration
    relaxes that UNIQUE.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id,
              task_id,
              draft_final_answer_id,
              decision,
              reason_code,
              payload,
              created_at
            FROM final_gate_reports
            WHERE draft_final_answer_id = :draft_id
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"draft_id": draft_id},
    ).first()
    return _row_dict(row) if row is not None else None


def _select_published_answer(
    conn: Connection, task_id: uuid.UUID
) -> dict[str, Any] | None:
    """Return the latest ``published_answers`` for the task, or None.

    Ordering: ``version_no DESC, published_at DESC, id DESC``.

    Note: a row in ``published_answers`` with ``status='withdrawn'``
    or ``status='superseded'`` is still surfaced here. The
    ``publication.status`` field of the report is derived
    deterministically from that ``status`` value (see
    ``_derive_publication_status``) and is NEVER flattened to
    ``published`` for historically-published-but-now-inactive rows.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id,
              task_id,
              draft_final_answer_id,
              final_gate_report_id,
              version_no,
              content_hash,
              payload,
              status,
              published_at,
              withdrawn_at,
              superseded_at,
              superseded_by_id
            FROM published_answers
            WHERE task_id = :task_id
            ORDER BY version_no DESC, published_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"task_id": task_id},
    ).first()
    return _row_dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Coverage gaps — load + axis decoration + severity ordering
# ---------------------------------------------------------------------------
def _coverage_gap_axis(kind: Any) -> str:
    """Map a coverage_gap_statements.kind value to the report's axis.

    Defensive against unknown kinds (e.g. if a future migration adds a
    new kind and the report is deployed before the mapping is
    updated): returns ``_COVERAGE_GAP_AXIS_FALLBACK`` ("other") in
    that case.
    """
    if not isinstance(kind, str):
        return _COVERAGE_GAP_AXIS_FALLBACK
    return _COVERAGE_GAP_AXIS_BY_KIND.get(kind, _COVERAGE_GAP_AXIS_FALLBACK)


def _coverage_gap_severity_rank(severity: Any) -> int:
    """Return the sort rank for a coverage_gap_statements.severity value.

    Lower rank sorts first. ``block`` < ``warn`` < ``info`` matches
    the UI requirement of surfacing blockers above warnings above
    info-level diagnostics.
    """
    if not isinstance(severity, str):
        return _COVERAGE_GAP_SEVERITY_FALLBACK_RANK
    return _COVERAGE_GAP_SEVERITY_RANK.get(
        severity, _COVERAGE_GAP_SEVERITY_FALLBACK_RANK
    )


def _select_coverage_gaps(
    conn: Connection, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return all ``coverage_gap_statements`` rows for the given draft.

    SQL ordering is ``created_at ASC, id ASC``. The severity-first
    ordering required by the block prompt is applied IN PYTHON by the
    caller via ``_apply_severity_first_ordering`` — this keeps the
    SQL portable and avoids a brittle CASE expression on the severity
    codomain.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              id,
              draft_final_answer_id,
              kind,
              severity,
              gap_key,
              details,
              created_at
            FROM coverage_gap_statements
            WHERE draft_final_answer_id = :draft_id
            ORDER BY created_at ASC, id ASC
            """
        ),
        {"draft_id": draft_id},
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _build_coverage_gap_view(gap: dict[str, Any]) -> dict[str, Any]:
    """Serialize a single coverage_gap row to the report's view shape.

    Adds the derived ``axis`` field. JSONB ``details`` is normalized
    verbatim. ``id`` and ``draft_final_answer_id`` become string UUIDs;
    ``created_at`` becomes an ISO 8601 string.
    """
    return {
        "id": _uuid_str(gap["id"]),
        "draft_final_answer_id": _uuid_str(gap["draft_final_answer_id"]),
        "kind": gap["kind"],
        "severity": gap["severity"],
        "gap_key": gap["gap_key"],
        "details": _normalize_jsonb(gap["details"]),
        "created_at": _dt(gap["created_at"]),
        "axis": _coverage_gap_axis(gap["kind"]),
    }


def _apply_severity_first_ordering(
    gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return ``gaps`` sorted severity-first, then created_at ASC, then id ASC.

    The SQL fetch is already in ``(created_at ASC, id ASC)`` order, so
    a stable sort by ``severity_rank`` alone preserves the secondary
    keys without any extra bookkeeping. We extract a tuple key
    explicitly to keep the sort deterministic even when the input list
    comes from a different SQL ordering in the future.
    """
    def _key(g: dict[str, Any]) -> tuple[int, str, str]:
        return (
            _coverage_gap_severity_rank(g["severity"]),
            # ``created_at`` is already an ISO 8601 string by this
            # point; lexicographic ordering on ISO strings matches
            # chronological ordering.
            str(g.get("created_at") or ""),
            # ``id`` is a string UUID here; lex order is deterministic.
            str(g.get("id") or ""),
        )

    return sorted(gaps, key=_key)


# ---------------------------------------------------------------------------
# Publication status derivation (PHASE_8_8B_REPORT_PRE.md §8.5)
# ---------------------------------------------------------------------------
def _derive_publication_status(
    *,
    task: dict[str, Any],
    gate: dict[str, Any] | None,
    published: dict[str, Any] | None,
) -> str:
    """Derive the report's ``publication.status`` field.

    The mapping mirrors the decision table in PHASE_8_8B_REPORT_PRE.md
    §8.5 / §7 exactly. ``withdrawn`` and ``superseded`` are exposed
    AS-IS (NEVER flattened to ``published``) so consumers can tell
    apart a currently-active publication from a historical row that
    is no longer the active one.

    ``publication_held`` is a DERIVED report state, NOT a value of
    ``task_masters.status``. It corresponds to "no published answer
    AND the gate rejected the draft". This is documented in
    ``limitations`` so consumers do not confuse it with a DB column.
    """
    if published is not None:
        status = published.get("status")
        if status == "published":
            return "published"
        if status == "withdrawn":
            return "withdrawn"
        if status == "superseded":
            return "superseded"
        # Unknown status value (defensive against a future codomain
        # extension that the report hasn't been updated for). Fall
        # through to the rest of the derivation rather than masking it.

    if published is None and gate is not None and gate.get("decision") == "rejected":
        return "publication_held"

    task_status = task.get("status")
    if task_status == "failed":
        return "failed"

    if gate is None:
        return "not_ready"

    return "unknown"


# ---------------------------------------------------------------------------
# CODE-B: SELECTs for claim-level aggregation
# ---------------------------------------------------------------------------
def _select_logical_claims(
    conn: Connection, task_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return the ``logical_claims`` rows for the task in deterministic order.

    Ordering: ``created_at ASC, id ASC`` — matches the convention used
    by the compiler when iterating verified claims
    (``compiler._select_verified_latest_for_task``) and by the claim
    read API (``GET /api/v1/tasks/{id}/claims``). This keeps the report
    consistent with the rest of the surface area.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              id,
              tenant_id,
              project_id,
              task_id,
              canonical_claim_text,
              canonical_claim_hash,
              created_at
            FROM logical_claims
            WHERE task_id = :task_id
            ORDER BY created_at ASC, id ASC
            """
        ),
        {"task_id": task_id},
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _select_ledger_entries_for_logical_ids(
    conn: Connection, logical_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Return ledger entries for the given logical_ids, ordered for
    latest-per-logical_id picking.

    Ordering: ``claim_logical_id ASC, version_no DESC, created_at DESC,
    id DESC``. The downstream helper ``_latest_ledger_by_logical_id``
    walks this list and picks the FIRST row per ``claim_logical_id`` —
    which by the ordering is the latest.

    Empty input → empty output, no SQL roundtrip. SQLAlchemy's
    ``bindparam(expanding=True, type_=Uuid())`` is used for the
    IN-list to handle large fanouts safely; the typed bindparam also
    cooperates with the psycopg 3 driver under both UUID and string
    inputs.
    """
    if not logical_ids:
        return []
    stmt = text(
        """
        SELECT
          id,
          claim_logical_id,
          version_no,
          state,
          support_scope,
          user_provided_dependency,
          human_review_required,
          human_review_status,
          transition_reason,
          payload,
          created_at
        FROM claim_ledger_entries
        WHERE claim_logical_id IN :logical_ids
        ORDER BY claim_logical_id ASC,
                 version_no DESC,
                 created_at DESC,
                 id DESC
        """
    ).bindparams(
        bindparam("logical_ids", expanding=True, type_=_SAUuid())
    )
    rows = conn.execute(stmt, {"logical_ids": list(logical_ids)}).fetchall()
    return [_row_dict(r) for r in rows]


def _latest_ledger_by_logical_id(
    rows: list[dict[str, Any]],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Pick the first row per ``claim_logical_id``.

    Caller MUST have fetched ``rows`` with the ordering produced by
    ``_select_ledger_entries_for_logical_ids`` (``claim_logical_id
    ASC, version_no DESC, created_at DESC, id DESC``): under that
    ordering the first row encountered for each ``claim_logical_id``
    is the absolute latest. Doing the latest-pick in Python keeps the
    SQL simple and readable in MVP-0 (numbers of claims per task are
    bounded).
    """
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        lid = uuid.UUID(str(r["claim_logical_id"]))
        if lid in out:
            continue
        out[lid] = r
    return out


def _select_claim_evidence_links(
    conn: Connection, entry_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Return ``claim_evidence_links`` rows targeting the given ledger
    entries.

    Only links with a non-null ``evidence_span_id`` are returned (the
    CHECK ``cel_origin_xor`` makes the second branch
    ``retrieved_source_span_id`` mutually exclusive, and the
    closed-corpus pipeline does not exercise it in MVP-0).

    Ordering: ``claim_ledger_entry_id ASC, evidence_span_id ASC, id
    ASC`` — deterministic so the response's ``evidence_links`` array
    stays stable across invocations.
    """
    if not entry_ids:
        return []
    stmt = text(
        """
        SELECT
          id,
          claim_logical_id,
          claim_ledger_entry_id,
          evidence_span_id,
          link_role,
          created_at
        FROM claim_evidence_links
        WHERE claim_ledger_entry_id IN :entry_ids
          AND evidence_span_id IS NOT NULL
        ORDER BY claim_ledger_entry_id ASC,
                 evidence_span_id ASC,
                 id ASC
        """
    ).bindparams(
        bindparam("entry_ids", expanding=True, type_=_SAUuid())
    )
    rows = conn.execute(stmt, {"entry_ids": list(entry_ids)}).fetchall()
    return [_row_dict(r) for r in rows]


def _select_evidence_for_task(
    conn: Connection, task_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return every ``evidence_spans`` row attached to the task via
    ``task_documents``.

    The chain is:
        task_documents
          -> uploaded_documents
          -> document_versions (any kind; MVP-0 uses 'parsed')
          -> document_chunks
          -> evidence_spans

    Ordering follows PHASE_8_8B_REPORT_PRE.md §5.1 verbatim:
    ``document_id ASC, chunk_index ASC, char_start ASC, evidence_spans.id
    ASC``. The chosen ordering reconstructs a document-reading order
    that is intuitive for UI consumers without forcing them to perform
    a second sort.
    """
    rows = conn.execute(
        text(
            """
            SELECT
              es.id              AS evidence_span_id,
              es.document_chunk_id AS document_chunk_id,
              es.quote           AS quote,
              es.quote_hash      AS quote_hash,
              ud.id              AS document_id,
              ud.filename        AS document_filename,
              dc.chunk_index     AS chunk_index,
              es.char_start      AS char_start
            FROM task_documents td
            JOIN uploaded_documents ud  ON ud.id = td.document_id
            JOIN document_versions  dv  ON dv.document_id = ud.id
            JOIN document_chunks    dc  ON dc.document_version_id = dv.id
            JOIN evidence_spans     es  ON es.document_chunk_id = dc.id
            WHERE td.task_id = :task_id
            ORDER BY ud.id ASC,
                     dc.chunk_index ASC,
                     es.char_start ASC,
                     es.id ASC
            """
        ),
        {"task_id": task_id},
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _select_cve_lite_records(
    conn: Connection, entry_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Return CVE-lite ``verification_records`` relevant to latest entries.

    Important lineage detail:
    CVE-lite writes ``verification_records`` on the v1 candidate entry,
    then appends a v2 ``claim_ledger_entries`` row such as
    ``verified_fact`` / ``unverifiable`` and links v1 -> v2 via
    ``claim_lineage(relation_kind='supersedes')``.

    The report is built around latest ledger entries, so we must read
    CVE-lite records both:
      - directly attached to a latest entry, for manually seeded / future
        rows; and
      - attached to the parent entry that was superseded by the latest
        entry, which is the normal worker pipeline path.

    ``report_claim_ledger_entry_id`` is the latest entry under which the
    record should be displayed. ``claim_ledger_entry_id`` remains the
    actual ledger entry referenced by the verification_records row.
    """
    if not entry_ids:
        return []
    stmt = text(
        """
        WITH target_entries AS (
            SELECT
              cle.id AS report_claim_ledger_entry_id,
              cle.id AS record_claim_ledger_entry_id
            FROM claim_ledger_entries cle
            WHERE cle.id IN :entry_ids

            UNION ALL

            SELECT
              cl.child_entry_id  AS report_claim_ledger_entry_id,
              cl.parent_entry_id AS record_claim_ledger_entry_id
            FROM claim_lineage cl
            WHERE cl.child_entry_id IN :entry_ids
              AND cl.relation_kind = 'supersedes'
        )
        SELECT DISTINCT ON (te.report_claim_ledger_entry_id, vr.id)
          te.report_claim_ledger_entry_id,
          vr.id,
          vr.claim_logical_id,
          vr.claim_ledger_entry_id,
          vr.check_kind,
          vr.check_name,
          vr.outcome,
          vr.payload,
          vr.created_at
        FROM target_entries te
        JOIN verification_records vr
          ON vr.claim_ledger_entry_id = te.record_claim_ledger_entry_id
        WHERE vr.check_kind = 'cve_lite'
        ORDER BY te.report_claim_ledger_entry_id ASC,
                 vr.id ASC
        """
    ).bindparams(
        bindparam("entry_ids", expanding=True, type_=_SAUuid())
    )
    rows = conn.execute(stmt, {"entry_ids": list(entry_ids)}).fetchall()
    return [_row_dict(r) for r in rows]


def _select_source_quality_rows(
    conn: Connection, evidence_span_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Return ALL ``source_quality_assessments`` for the given
    evidence spans, ordered for latest-per-target picking.

    Ordering: ``evidence_span_id ASC, version_no DESC, created_at
    DESC, id DESC``. The caller (``_latest_source_quality_by_span``)
    walks this list and picks the FIRST row per ``evidence_span_id``
    — under the ordering, that is the absolute latest at DB level
    (matches the Final Answer Gate's read semantics; see
    PHASE_8_8B_REPORT_PRE.md §5.3).

    The query filters by ``evidence_span_id`` only; rows targeting
    document_chunk_id or document_id are excluded because the report
    aggregates Source Quality on the span axis.
    """
    if not evidence_span_ids:
        return []
    stmt = text(
        """
        SELECT
          id,
          tenant_id,
          project_id,
          evidence_span_id,
          version_no,
          overall_quality,
          contradiction_status,
          evaluator_name,
          evaluator_version,
          policy_name,
          policy_version,
          payload,
          created_at
        FROM source_quality_assessments
        WHERE evidence_span_id IN :span_ids
        ORDER BY evidence_span_id ASC,
                 version_no DESC,
                 created_at DESC,
                 id DESC
        """
    ).bindparams(
        bindparam("span_ids", expanding=True, type_=_SAUuid())
    )
    rows = conn.execute(stmt, {"span_ids": list(evidence_span_ids)}).fetchall()
    return [_row_dict(r) for r in rows]


def _latest_source_quality_by_span(
    rows: list[dict[str, Any]],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Pick the first row per ``evidence_span_id``.

    Same first-wins-by-prior-ordering pattern as
    ``_latest_ledger_by_logical_id``.
    """
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        sid = uuid.UUID(str(r["evidence_span_id"]))
        if sid in out:
            continue
        out[sid] = r
    return out


def _select_entailment_rows(
    conn: Connection, entry_ids: list[uuid.UUID]
) -> list[dict[str, Any]]:
    """Return ALL ``claim_entailment_checks`` for the given ledger
    entries, ordered for latest-per-pair picking.

    Ordering: ``claim_ledger_entry_id ASC, evidence_span_id ASC,
    version_no DESC, created_at DESC, id DESC``. The caller
    (``_latest_entailment_by_pair``) walks this list and picks the
    FIRST row per ``(claim_ledger_entry_id, evidence_span_id)`` pair.

    We over-fetch by entry_id only (no explicit pair filter at SQL
    level) and let the caller filter by the set of relevant pairs in
    Python: this avoids constructing a tuple-IN filter at the
    SQLAlchemy boundary while keeping the query simple. In MVP-0 the
    number of pairs per task is bounded.
    """
    if not entry_ids:
        return []
    stmt = text(
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
          payload,
          created_at
        FROM claim_entailment_checks
        WHERE claim_ledger_entry_id IN :entry_ids
        ORDER BY claim_ledger_entry_id ASC,
                 evidence_span_id ASC,
                 version_no DESC,
                 created_at DESC,
                 id DESC
        """
    ).bindparams(
        bindparam("entry_ids", expanding=True, type_=_SAUuid())
    )
    rows = conn.execute(stmt, {"entry_ids": list(entry_ids)}).fetchall()
    return [_row_dict(r) for r in rows]


def _latest_entailment_by_pair(
    rows: list[dict[str, Any]],
) -> dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]]:
    """Pick the first row per ``(claim_ledger_entry_id,
    evidence_span_id)`` pair.

    Same first-wins-by-prior-ordering pattern as
    ``_latest_ledger_by_logical_id``.
    """
    out: dict[tuple[uuid.UUID, uuid.UUID], dict[str, Any]] = {}
    for r in rows:
        eid = uuid.UUID(str(r["claim_ledger_entry_id"]))
        sid = uuid.UUID(str(r["evidence_span_id"]))
        key = (eid, sid)
        if key in out:
            continue
        out[key] = r
    return out


# ---------------------------------------------------------------------------
# CODE-B: per-claim builders
# ---------------------------------------------------------------------------
def _build_evidence_link_view(link: dict[str, Any]) -> dict[str, Any]:
    """Serialize one ``claim_evidence_links`` row for the report.

    Only the fields needed by UI consumers are surfaced. The
    ``claim_logical_id`` is implicit from the enclosing claim entry,
    so we do not duplicate it here.
    """
    return {
        "claim_evidence_link_id": _uuid_str(link["id"]),
        "evidence_span_id": _uuid_str(link["evidence_span_id"]),
        "link_role": link["link_role"],
    }


def _build_cve_lite_view(record: dict[str, Any]) -> dict[str, Any]:
    """Serialize one CVE-lite ``verification_records`` row for the report."""
    return {
        "verification_record_id": _uuid_str(record["id"]),
        "claim_ledger_entry_id": _uuid_str(record["claim_ledger_entry_id"]),
        "outcome": record["outcome"],
        "check_name": record["check_name"],
    }


def _build_source_quality_view(
    *,
    evidence_span_id: uuid.UUID,
    latest_assessment: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serialize one source-quality slot in a claim's ``source_quality``
    list.

    When ``latest_assessment`` is None the slot reports a "missing"
    state with nulls in every detail field; the caller increments the
    axis_summary ``missing_count`` accordingly.

    ``mock`` is derived from ``payload.mock`` when an assessment is
    present, falling back to None when missing (we cannot assert mock
    on a non-existent row).
    """
    if latest_assessment is None:
        return {
            "evidence_span_id": _uuid_str(evidence_span_id),
            "latest_assessment_id": None,
            "overall_quality": None,
            "contradiction_status": None,
            "evaluator_name": None,
            "policy_name": None,
            "policy_version": None,
            "mock": None,
        }
    return {
        "evidence_span_id": _uuid_str(evidence_span_id),
        "latest_assessment_id": _uuid_str(latest_assessment["id"]),
        "overall_quality": latest_assessment["overall_quality"],
        "contradiction_status": latest_assessment["contradiction_status"],
        "evaluator_name": latest_assessment["evaluator_name"],
        "policy_name": latest_assessment["policy_name"],
        "policy_version": latest_assessment["policy_version"],
        "mock": _is_payload_mock(latest_assessment.get("payload")),
    }


def _build_entailment_view(
    *,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    latest_check: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serialize one entailment slot in a claim's ``entailment`` list.

    When ``latest_check`` is None the slot reports a "missing" state
    with nulls in every detail field; the caller increments the
    axis_summary ``missing_count`` accordingly.

    ``confidence`` is coerced to float when present (the DB column is
    DOUBLE PRECISION). ``mock`` follows the same rule as source
    quality: True / False only when a row is present and the
    ``payload.mock`` flag is explicit; None when the check is missing.
    """
    if latest_check is None:
        return {
            "claim_ledger_entry_id": _uuid_str(claim_ledger_entry_id),
            "evidence_span_id": _uuid_str(evidence_span_id),
            "latest_check_id": None,
            "verdict": None,
            "confidence": None,
            "checker_name": None,
            "policy_name": None,
            "policy_version": None,
            "mock": None,
        }
    confidence_raw = latest_check.get("confidence")
    confidence = (
        float(confidence_raw) if confidence_raw is not None else None
    )
    return {
        "claim_ledger_entry_id": _uuid_str(claim_ledger_entry_id),
        "evidence_span_id": _uuid_str(evidence_span_id),
        "latest_check_id": _uuid_str(latest_check["id"]),
        "verdict": latest_check["verdict"],
        "confidence": confidence,
        "checker_name": latest_check["checker_name"],
        "policy_name": latest_check["policy_name"],
        "policy_version": latest_check["policy_version"],
        "mock": _is_payload_mock(latest_check.get("payload")),
    }


def _build_claim_items(
    *,
    logical_claims: list[dict[str, Any]],
    latest_entry_by_logical: dict[uuid.UUID, dict[str, Any]],
    links_by_entry: dict[uuid.UUID, list[dict[str, Any]]],
    cve_records_by_entry: dict[uuid.UUID, list[dict[str, Any]]],
    latest_sq_by_span: dict[uuid.UUID, dict[str, Any]],
    latest_ce_by_pair: dict[
        tuple[uuid.UUID, uuid.UUID], dict[str, Any]
    ],
) -> list[dict[str, Any]]:
    """Assemble the ``claims`` array of the report.

    One entry per ``logical_claim`` of the task. For each:
      - ``latest_entry_id`` / ``latest_state`` from the latest
        ``claim_ledger_entries`` row by ``(version_no DESC, created_at
        DESC, id DESC)``; null when no ledger entry exists yet (pre-CVE
        tasks).
      - ``support_scope`` mirrors the latest ledger entry; null when
        absent.
      - ``claim_type`` is left at None in CODE-B because logical_claims
        does NOT persist a claim_type column at DB level — the value
        lives on ``classified_claims`` which is keyed by raw_claim and
        is out of scope for this aggregation. A future block may join
        classified_claims for a richer surface.
      - ``evidence_links``: structural links scoped to the latest
        ledger entry. Links pointing at older entries are NOT
        surfaced; the report follows the same latest-entry semantics
        used by the Final Answer Gate.
      - ``cve_lite``: every CVE-lite verification_record attached to
        the latest entry.
      - ``source_quality``: one slot per linked evidence_span_id with
        the latest assessment (or a missing slot).
      - ``entailment``: one slot per (latest_entry, evidence_span)
        pair with the latest check (or a missing slot).
    """
    items: list[dict[str, Any]] = []
    for lc in logical_claims:
        lid = uuid.UUID(str(lc["id"]))
        latest_entry = latest_entry_by_logical.get(lid)
        if latest_entry is None:
            latest_entry_id: uuid.UUID | None = None
            latest_entry_id_str: str | None = None
            latest_state: str | None = None
            support_scope: str | None = None
        else:
            latest_entry_id = uuid.UUID(str(latest_entry["id"]))
            latest_entry_id_str = str(latest_entry_id)
            latest_state = latest_entry.get("state")
            support_scope = latest_entry.get("support_scope")

        links: list[dict[str, Any]] = (
            links_by_entry.get(latest_entry_id, [])
            if latest_entry_id is not None
            else []
        )
        cve_records: list[dict[str, Any]] = (
            cve_records_by_entry.get(latest_entry_id, [])
            if latest_entry_id is not None
            else []
        )

        # source_quality: one slot per linked evidence_span_id; latest
        # assessment if present, else a missing slot.
        sq_views: list[dict[str, Any]] = []
        seen_span_ids: set[uuid.UUID] = set()
        for link in links:
            ev_id_raw = link["evidence_span_id"]
            if ev_id_raw is None:
                continue
            ev_id = uuid.UUID(str(ev_id_raw))
            if ev_id in seen_span_ids:
                # Two links pointing to the same evidence_span for the
                # same latest_entry would produce duplicate slots
                # otherwise. Dedup defensively.
                continue
            seen_span_ids.add(ev_id)
            sq_views.append(
                _build_source_quality_view(
                    evidence_span_id=ev_id,
                    latest_assessment=latest_sq_by_span.get(ev_id),
                )
            )

        # entailment: one slot per (latest_entry, evidence_span) pair.
        ce_views: list[dict[str, Any]] = []
        if latest_entry_id is not None:
            seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
            for link in links:
                ev_id_raw = link["evidence_span_id"]
                if ev_id_raw is None:
                    continue
                ev_id = uuid.UUID(str(ev_id_raw))
                pair_key = (latest_entry_id, ev_id)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                ce_views.append(
                    _build_entailment_view(
                        claim_ledger_entry_id=latest_entry_id,
                        evidence_span_id=ev_id,
                        latest_check=latest_ce_by_pair.get(pair_key),
                    )
                )

        items.append(
            {
                "logical_claim_id": str(lid),
                "latest_entry_id": latest_entry_id_str,
                "latest_state": latest_state,
                "canonical_claim_text": lc.get("canonical_claim_text"),
                # claim_type is not persisted on logical_claims /
                # claim_ledger_entries in MVP-0; surfaced as null to
                # preserve the documented shape without inventing
                # data. See PHASE_8_8B_REPORT_PRE.md §15 (raw/classified
                # excluded from v1).
                "claim_type": None,
                "support_scope": support_scope,
                "evidence_links": [
                    _build_evidence_link_view(ln) for ln in links
                ],
                "cve_lite": [
                    _build_cve_lite_view(r) for r in cve_records
                ],
                "source_quality": sq_views,
                "entailment": ce_views,
            }
        )
    return items


def _build_evidence_items(
    evidence_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble the ``evidence`` array of the report.

    Each row mirrors the shape documented in PHASE_8_8B_REPORT_PRE.md
    §6: identifiers + the verbatim quote and quote_hash + document
    metadata for context. Ordering is preserved from the SQL fetch
    (document_id ASC, chunk_index ASC, char_start ASC, evidence id
    ASC).
    """
    out: list[dict[str, Any]] = []
    for r in evidence_rows:
        out.append(
            {
                "evidence_span_id": _uuid_str(r["evidence_span_id"]),
                "document_chunk_id": _uuid_str(r["document_chunk_id"]),
                "quote": r.get("quote"),
                "quote_hash": r.get("quote_hash"),
                "document_id": _uuid_str(r["document_id"]),
                "document_filename": r.get("document_filename"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# CODE-B: axis_summary aggregations
# ---------------------------------------------------------------------------
def _build_axis_summary_cve(
    cve_records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate CVE-lite outcomes into report counters.

    Mapping (PHASE_8_8B_REPORT_PRE.md §7.4):
      - outcome='pass'         → verified_claims_count
      - outcome='fail'         → unverified_claims_count
      - outcome='inconclusive' → inconclusive_count

    Unknown outcomes (defensive; the CHECK constraint in 0004 already
    pins the codomain) are bucketed into ``inconclusive_count`` so a
    future codomain extension surfaces as inconclusive rather than as
    a silent drop.
    """
    counts = {
        "verified_claims_count": 0,
        "unverified_claims_count": 0,
        "inconclusive_count": 0,
    }
    for r in cve_records:
        outcome = r.get("outcome")
        if outcome == "pass":
            counts["verified_claims_count"] += 1
        elif outcome == "fail":
            counts["unverified_claims_count"] += 1
        else:
            counts["inconclusive_count"] += 1
    return counts


def _build_axis_summary_source_quality(
    *,
    relevant_span_ids: list[uuid.UUID],
    latest_sq_by_span: dict[uuid.UUID, dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate Source Quality latest-per-target into report counters.

    The relevant span set is the union of evidence_span_ids reached
    by claim_evidence_links targeting any latest ledger entry of the
    task — see PHASE_8_8B_REPORT_PRE.md §5.2. Spans attached to the
    task via task_documents but never linked to a claim are NOT
    counted (they would inflate ``missing_count`` artificially).

    All five overall_quality codomain keys are initialized to 0 so
    consumers can rely on the shape regardless of what data is
    present. Unknown values surface in the ``unknown`` bucket
    defensively.
    """
    counts: dict[str, int] = {
        f"{v}_count": 0 for v in _SOURCE_QUALITY_OVERALL_QUALITY_VALUES
    }
    counts["missing_count"] = 0
    for sid in relevant_span_ids:
        latest = latest_sq_by_span.get(sid)
        if latest is None:
            counts["missing_count"] += 1
            continue
        oq = latest.get("overall_quality")
        if isinstance(oq, str) and oq in _SOURCE_QUALITY_OVERALL_QUALITY_VALUES:
            counts[f"{oq}_count"] += 1
        else:
            counts["unknown_count"] += 1
    return counts


def _build_axis_summary_entailment(
    *,
    relevant_pairs: list[tuple[uuid.UUID, uuid.UUID]],
    latest_ce_by_pair: dict[
        tuple[uuid.UUID, uuid.UUID], dict[str, Any]
    ],
) -> dict[str, Any]:
    """Aggregate Claim Entailment latest-per-pair into report counters.

    The relevant pair set is ``(latest_entry_id, evidence_span_id)``
    derived from claim_evidence_links restricted to the latest ledger
    entry per logical claim. Pairs not realized by an existing
    entailment check fall into ``missing_count`` — same rule applied
    by the Final Answer Gate for "missing entailment check"
    warnings (see PHASE_8_8A_GATE_PRE.md and the Gate's
    ``_classify_entailment_per_span``).

    All five verdict codomain keys are initialized to 0. Unknown
    verdicts surface in ``uncertain_count`` defensively (same fallback
    used by the Gate when it cannot map a verdict).
    """
    counts: dict[str, int] = {
        f"{v}_count": 0 for v in _CLAIM_ENTAILMENT_VERDICT_VALUES
    }
    counts["missing_count"] = 0
    for pair in relevant_pairs:
        latest = latest_ce_by_pair.get(pair)
        if latest is None:
            counts["missing_count"] += 1
            continue
        verdict = latest.get("verdict")
        if isinstance(verdict, str) and verdict in _CLAIM_ENTAILMENT_VERDICT_VALUES:
            counts[f"{verdict}_count"] += 1
        else:
            counts["uncertain_count"] += 1
    return counts


def _build_axis_summary_final_gate(
    coverage_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the ``axis_summary.final_gate`` block from coverage gaps.

    Counters:
      - ``blocking_gap_count``: number of gaps with severity == "block".
      - ``warning_gap_count``: number of gaps with severity == "warn".
      - ``has_blocking_gaps``: ``blocking_gap_count > 0``.
      - ``has_warnings``: ``warning_gap_count > 0``.

    Info-severity gaps are not counted toward either bucket; they
    surface in ``gate.coverage_gaps`` but do not influence these
    summary booleans (PHASE_8_8B_REPORT_PRE.md §8.7).
    """
    blocking_gap_count = 0
    warning_gap_count = 0
    for g in coverage_gaps:
        sev = g.get("severity")
        if sev == "block":
            blocking_gap_count += 1
        elif sev == "warn":
            warning_gap_count += 1
    return {
        "has_blocking_gaps": blocking_gap_count > 0,
        "has_warnings": warning_gap_count > 0,
        "blocking_gap_count": blocking_gap_count,
        "warning_gap_count": warning_gap_count,
    }


# ---------------------------------------------------------------------------
# Mock indicators (CODE-B: derive from real rows when available)
# ---------------------------------------------------------------------------
def _update_mock_indicators(
    *,
    draft: dict[str, Any] | None,
    cve_records: list[dict[str, Any]],
    sq_rows: list[dict[str, Any]],
    ce_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the ``mock_indicators`` section deriving from real data.

    Derivation rules (PHASE_8_8B_REPORT_PRE.md §7.7):

      uses_mock_source_quality:
        - True if at least one source_quality_assessments row relevant
          to the task has either evaluator_name equal to the known mock
          evaluator name OR payload.mock == True;
        - True fallback when no SQ data is present (MVP-0 default).

      uses_mock_claim_entailment:
        - True if at least one claim_entailment_checks row relevant to
          the task has either checker_name equal to the known mock
          checker name OR payload.mock == True;
        - True fallback when no CE data is present (MVP-0 default).

      uses_mock_compiler:
        - if draft exists: True iff draft.compiler_name == mock
          compiler name; False otherwise;
        - if no draft: True fallback (compiler did not run yet).

      uses_mock_cve_lite:
        - True if at least one verification_records row of kind
          'cve_lite' relevant to the task has check_name equal to the
          known mock CVE-lite check name;
        - True fallback when no CVE-lite data is present.

    The ``notes`` array carries the anti-hallucination disclaimers. The
    CODE-B note replaces CODE-A's "claims/evidence aggregation is
    deferred" wording.
    """
    notes: list[str] = [
        "Una fonte citata non implica un claim vero.",
        "Una quote testualmente presente non implica che la quote "
        "sostenga il claim.",
        "Un verdict 'entailed' non implica che il claim sia vero nel "
        "mondo.",
        "Il payload JSONB è esposto verbatim; RBAC/redaction non "
        "implementata.",
        "Lifecycle/source-loss event details are not included in this "
        "report; eventual coverage_gap_statements with "
        "kind='source_loss' may still appear in gate.coverage_gaps.",
        "claims/evidence/CVE-lite/Source Quality/Claim Entailment "
        "aggregation inclusa in questo report; lifecycle/source-loss "
        "event details restano fuori dalla v1.",
    ]

    # Source Quality.
    uses_mock_source_quality: bool
    if not sq_rows:
        uses_mock_source_quality = True
        notes.append(
            "uses_mock_source_quality reflects the MVP-0 fallback: "
            "no source_quality_assessments rows are linked to claims "
            "of this task yet."
        )
    else:
        uses_mock_source_quality = any(
            r.get("evaluator_name") == _MOCK_SOURCE_QUALITY_EVALUATOR_NAME
            or _is_payload_mock(r.get("payload"))
            for r in sq_rows
        )

    # Claim Entailment.
    uses_mock_claim_entailment: bool
    if not ce_rows:
        uses_mock_claim_entailment = True
        notes.append(
            "uses_mock_claim_entailment reflects the MVP-0 fallback: "
            "no claim_entailment_checks rows are linked to claims of "
            "this task yet."
        )
    else:
        uses_mock_claim_entailment = any(
            r.get("checker_name") == _MOCK_ENTAILMENT_CHECKER_NAME
            or _is_payload_mock(r.get("payload"))
            for r in ce_rows
        )

    # Compiler.
    uses_mock_compiler: bool
    if draft is None:
        uses_mock_compiler = True
        notes.append(
            "uses_mock_compiler reflects the MVP-0 fallback: no draft "
            "has been compiled yet for this task."
        )
    else:
        compiler_name = draft.get("compiler_name")
        uses_mock_compiler = compiler_name == _MOCK_COMPILER_NAME

    # CVE-lite.
    uses_mock_cve_lite: bool
    if not cve_records:
        uses_mock_cve_lite = True
        notes.append(
            "uses_mock_cve_lite reflects the MVP-0 fallback: no "
            "verification_records of kind 'cve_lite' are present for "
            "claims of this task yet."
        )
    else:
        uses_mock_cve_lite = any(
            r.get("check_name") == _MOCK_CVE_LITE_CHECK_NAME
            for r in cve_records
        )

    return {
        "uses_mock_source_quality": uses_mock_source_quality,
        "uses_mock_claim_entailment": uses_mock_claim_entailment,
        "uses_mock_compiler": uses_mock_compiler,
        "uses_mock_cve_lite": uses_mock_cve_lite,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Limitations
# ---------------------------------------------------------------------------
def _limitations() -> list[str]:
    """Return the textual limitations that MUST accompany every report.

    Even on a clean task (every axis green, published answer active),
    these disclaimers stay in the response. They communicate the
    architectural commitments of the system and are NOT optional.
    See PHASE_8_8B_REPORT_PRE.md §9.
    """
    return [
        "Una fonte citata non implica che il claim sia vero.",
        "Una quote testualmente presente non implica supporto "
        "semantico del claim.",
        "Un verdict 'entailed' non implica verità nel mondo.",
        "Il payload JSONB è esposto verbatim; RBAC/redaction non "
        "implementata.",
        "I dettagli evento lifecycle/source-loss non sono inclusi in "
        "questo report; eventuali coverage_gap_statements con "
        "kind='source_loss' possono comunque comparire in "
        "gate.coverage_gaps.",
        "claims/evidence/CVE-lite/Source Quality/Claim Entailment "
        "aggregation inclusa in questo report; lifecycle/source-loss "
        "event details restano fuori dalla v1, ma eventuali "
        "coverage_gap_statements con kind='source_loss' possono "
        "comparire in gate.coverage_gaps.",
    ]


# ---------------------------------------------------------------------------
# Section builders — task / publication / gate
# ---------------------------------------------------------------------------
def _build_task_section(task: dict[str, Any]) -> dict[str, Any]:
    """Serialize the ``task`` metadata section of the report."""
    return {
        "status": task.get("status"),
        "objective": task.get("objective"),
        "mode": task.get("mode"),
        "created_at": _dt(task.get("created_at")),
        "updated_at": _dt(task.get("updated_at")),
    }


def _build_publication_section(
    *,
    publication_status: str,
    published: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serialize the ``publication`` section of the report.

    All optional identifiers are stringified UUIDs (or ``None``).
    ``summary_text`` is NOT loaded here because the ``published_answers``
    table does not store summary_text directly: it lives on
    ``draft_final_answers``. CODE-B leaves it as None; a future block
    may fold in the draft's summary when the broader aggregation is
    wired.
    """
    if published is None:
        return {
            "status": publication_status,
            "published_answer_id": None,
            "published_answer_status": None,
            "summary_text": None,
            "content_hash": None,
            "final_gate_report_id": None,
        }
    return {
        "status": publication_status,
        "published_answer_id": _uuid_str(published.get("id")),
        "published_answer_status": published.get("status"),
        "summary_text": None,
        "content_hash": published.get("content_hash"),
        "final_gate_report_id": _uuid_str(
            published.get("final_gate_report_id")
        ),
    }


def _build_gate_section(
    *,
    gate: dict[str, Any] | None,
    coverage_gaps_view: list[dict[str, Any]],
) -> dict[str, Any]:
    """Serialize the ``gate`` section of the report.

    When no gate report exists (task not yet through the gate, or
    pipeline failed before the gate step), ``decision`` and
    ``reason_code`` are ``None`` and ``payload`` is an empty dict.
    ``coverage_gaps`` is still emitted: if a draft exists with gaps
    (e.g. only some pre-gate gaps were inserted), they are surfaced
    independently. In practice today the gate is the only writer of
    ``coverage_gap_statements``, so this list is empty when no gate
    report exists.

    JSONB ``payload`` is exposed VERBATIM, no redaction.
    """
    if gate is None:
        return {
            "decision": None,
            "reason_code": None,
            "payload": {},
            "coverage_gaps": coverage_gaps_view,
        }
    return {
        "decision": gate.get("decision"),
        "reason_code": gate.get("reason_code"),
        "payload": _normalize_jsonb(gate.get("payload")) or {},
        "coverage_gaps": coverage_gaps_view,
    }


# ---------------------------------------------------------------------------
# Endpoint — GET /api/v1/tasks/{task_id}/anti-hallucination-report
# ---------------------------------------------------------------------------
@router.get("/tasks/{task_id}/anti-hallucination-report")
def get_task_anti_hallucination_report(
    task_id: uuid.UUID,
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Return the task-level Anti-Hallucination Report (CODE-A + CODE-B).

    Behavior summary:
      - 404 RESOURCE_NOT_FOUND with ``details.resource="task_masters"``
        when the task does not exist (immediate, no further SELECTs).
      - 200 in every other case, with the shape documented in
        PHASE_8_8B_REPORT_PRE.md §4 (top-level keys: ``task_id``,
        ``project_id``, ``tenant_id``, ``task``, ``publication``,
        ``gate``, ``claims``, ``evidence``, ``axis_summary``,
        ``mock_indicators``, ``limitations``).
      - ``claims`` is one entry per logical_claim of the task,
        carrying ``latest_entry_id``, ``latest_state``,
        ``support_scope``, structural ``evidence_links`` (scoped to
        the latest ledger entry), CVE-lite records, latest Source
        Quality per linked span, and latest Claim Entailment per
        (latest_entry, span) pair.
      - ``evidence`` is one entry per evidence_span attached to the
        task via task_documents.
      - ``axis_summary.final_gate`` is derived from the coverage gaps
        attached to the latest draft.
      - ``axis_summary.cve_lite`` counts CVE-lite outcomes on the
        latest ledger entries.
      - ``axis_summary.source_quality`` counts the LATEST
        overall_quality per evidence_span linked to claims; missing
        rows fall into ``missing_count``.
      - ``axis_summary.claim_entailment`` counts the LATEST verdict
        per (latest_entry, span) pair derived from claim_evidence_links;
        missing rows fall into ``missing_count``. Spans attached to
        the task via task_documents but never linked to a claim are
        NOT counted as missing — this avoids inflating missing_count
        for unused document spans.
      - ``coverage_gaps`` are returned ordered severity-first
        (block > warn > info), then created_at ASC, then id ASC. The
        derived ``axis`` decoration is added per gap.

    Strict scope reminder:
      - This endpoint is purely a derived view. It does NOT call any
        worker service, never mutates DB state, and is NOT a new
        source of truth. The append-only tables and the specialist
        read APIs (8.6 / 8.7F / 8.8A-READ-A / 8.4 answers) remain
        authoritative.
      - JSONB content is exposed verbatim; RBAC redaction is a known
        debt and is acknowledged in ``limitations``.
      - ``claim_type`` is surfaced as None: the column does not exist
        on logical_claims / claim_ledger_entries in MVP-0 (it lives on
        ``classified_claims`` and is excluded from v1 per
        PHASE_8_8B_REPORT_PRE.md §15).
    """
    # --- Existence check and core context ----------------------------------
    task = _task_or_404(conn, task_id)

    draft = _select_latest_draft(conn, task_id)

    gate: dict[str, Any] | None
    coverage_gaps_view: list[dict[str, Any]]
    if draft is not None:
        draft_id = uuid.UUID(str(draft["id"]))
        gate = _select_final_gate_report(conn, draft_id)
        raw_gaps = _select_coverage_gaps(conn, draft_id)
        coverage_gaps_view = [_build_coverage_gap_view(g) for g in raw_gaps]
        coverage_gaps_view = _apply_severity_first_ordering(coverage_gaps_view)
    else:
        gate = None
        coverage_gaps_view = []

    published = _select_published_answer(conn, task_id)

    publication_status = _derive_publication_status(
        task=task, gate=gate, published=published
    )

    # --- CODE-B: claim / evidence aggregation ------------------------------
    logical_claims = _select_logical_claims(conn, task_id)
    logical_ids = [uuid.UUID(str(lc["id"])) for lc in logical_claims]

    ledger_rows = _select_ledger_entries_for_logical_ids(conn, logical_ids)
    latest_entry_by_logical = _latest_ledger_by_logical_id(ledger_rows)

    latest_entry_ids: list[uuid.UUID] = [
        uuid.UUID(str(row["id"])) for row in latest_entry_by_logical.values()
    ]

    raw_links = _select_claim_evidence_links(conn, latest_entry_ids)

    # Group links by claim_ledger_entry_id for downstream assembly. Same
    # ordering as the SQL fetch is preserved by Python's stable list.
    links_by_entry: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for link in raw_links:
        eid = uuid.UUID(str(link["claim_ledger_entry_id"]))
        links_by_entry.setdefault(eid, []).append(link)

    cve_records = _select_cve_lite_records(conn, latest_entry_ids)
    cve_records_by_entry: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for r in cve_records:
        # CVE-lite records are normally attached to the v1 parent entry,
        # while the report is keyed by the latest v2 entry. The SELECT
        # provides report_claim_ledger_entry_id to map the source record
        # back under the latest entry without falsifying the record's own
        # claim_ledger_entry_id.
        report_entry_id = r.get("report_claim_ledger_entry_id") or r[
            "claim_ledger_entry_id"
        ]
        eid = uuid.UUID(str(report_entry_id))
        cve_records_by_entry.setdefault(eid, []).append(r)

    # Compute the set of evidence spans that are RELEVANT to claim-level
    # axes (i.e. reached by a claim_evidence_links targeting a latest
    # ledger entry). Spans attached via task_documents that are not
    # linked to any claim are EXCLUDED from missing_count semantics.
    relevant_span_ids: list[uuid.UUID] = []
    seen_spans: set[uuid.UUID] = set()
    for link in raw_links:
        ev_id_raw = link["evidence_span_id"]
        if ev_id_raw is None:
            continue
        sid = uuid.UUID(str(ev_id_raw))
        if sid in seen_spans:
            continue
        seen_spans.add(sid)
        relevant_span_ids.append(sid)

    sq_rows = _select_source_quality_rows(conn, relevant_span_ids)
    latest_sq_by_span = _latest_source_quality_by_span(sq_rows)

    ce_rows = _select_entailment_rows(conn, latest_entry_ids)
    latest_ce_by_pair = _latest_entailment_by_pair(ce_rows)

    # Relevant pair set for the entailment axis: union of
    # (latest_entry_id, evidence_span_id) across all links targeting a
    # latest ledger entry.
    relevant_pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for link in raw_links:
        ev_id_raw = link["evidence_span_id"]
        if ev_id_raw is None:
            continue
        eid = uuid.UUID(str(link["claim_ledger_entry_id"]))
        sid = uuid.UUID(str(ev_id_raw))
        pair_key = (eid, sid)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        relevant_pairs.append(pair_key)

    claims_view = _build_claim_items(
        logical_claims=logical_claims,
        latest_entry_by_logical=latest_entry_by_logical,
        links_by_entry=links_by_entry,
        cve_records_by_entry=cve_records_by_entry,
        latest_sq_by_span=latest_sq_by_span,
        latest_ce_by_pair=latest_ce_by_pair,
    )

    evidence_rows = _select_evidence_for_task(conn, task_id)
    evidence_view = _build_evidence_items(evidence_rows)

    # --- axis_summary -------------------------------------------------------
    axis_summary = {
        "cve_lite": _build_axis_summary_cve(cve_records),
        "source_quality": _build_axis_summary_source_quality(
            relevant_span_ids=relevant_span_ids,
            latest_sq_by_span=latest_sq_by_span,
        ),
        "claim_entailment": _build_axis_summary_entailment(
            relevant_pairs=relevant_pairs,
            latest_ce_by_pair=latest_ce_by_pair,
        ),
        "final_gate": _build_axis_summary_final_gate(coverage_gaps_view),
    }

    mock_indicators = _update_mock_indicators(
        draft=draft,
        cve_records=cve_records,
        sq_rows=sq_rows,
        ce_rows=ce_rows,
    )

    return {
        "task_id": _uuid_str(task["id"]),
        "project_id": _uuid_str(task["project_id"]),
        "tenant_id": _uuid_str(task["tenant_id"]),
        "task": _build_task_section(task),
        "publication": _build_publication_section(
            publication_status=publication_status,
            published=published,
        ),
        "gate": _build_gate_section(
            gate=gate, coverage_gaps_view=coverage_gaps_view
        ),
        "claims": claims_view,
        "evidence": evidence_view,
        "axis_summary": axis_summary,
        "mock_indicators": mock_indicators,
        "limitations": _limitations(),
    }
