"""API routes for Phase 8.8B-REPORT-CODE-A — Anti-Hallucination Report (read-only).

Endpoint exposed by this module:

  GET /api/v1/tasks/{task_id}/anti-hallucination-report      (Phase 8.8B-REPORT-CODE-A)

Strict invariants (Phase 8.8B-REPORT-CODE-A — first read-only skeleton):

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
        * ``claims`` and ``evidence`` left as empty lists in CODE-A
          (claim-level aggregation arrives in CODE-B);
        * ``axis_summary.final_gate`` derived from
          ``coverage_gap_statements`` collected on the latest draft;
        * ``axis_summary.cve_lite``, ``axis_summary.source_quality``,
          ``axis_summary.claim_entailment`` initialized with zeroed
          counters (claim-level aggregation arrives in CODE-B);
        * ``mock_indicators`` reflect what we already know from
          ``draft_final_answers.compiler_name`` plus the MVP-0 fallback
          values for the remaining axes;
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

CODE-A explicitly DOES NOT yet implement:
  - claim-level entries in ``claims``;
  - evidence rows in ``evidence``;
  - CVE-lite verification record aggregation;
  - Source Quality latest-per-target aggregation;
  - Claim Entailment latest-per-pair aggregation;
  - realistic flow tests on the aggregated axes.

These arrive in 8.8B-REPORT-CODE-B.
"""
from __future__ import annotations

import datetime as _dt_mod
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.engine import Connection

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
# axis_summary.final_gate
# ---------------------------------------------------------------------------
def _build_axis_summary_final_gate(
    coverage_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the ``axis_summary.final_gate`` block from coverage gaps.

    This is the ONLY axis_summary block actually computed in CODE-A
    (the others — cve_lite, source_quality, claim_entailment — are
    initialized with zeroed counters by ``_build_zeroed_axis_summary``
    and will be populated in CODE-B).

    Counters:
      - ``blocking_gap_count``: number of gaps with severity == "block".
      - ``warning_gap_count``: number of gaps with severity == "warn".
      - ``has_blocking_gaps``: ``blocking_gap_count > 0``.
      - ``has_warnings``: ``warning_gap_count > 0``.

    Note: info-severity gaps are not counted toward either bucket;
    they are surfaced in ``gate.coverage_gaps`` but do not influence
    these two summary booleans. Mirrors the report's UI intent
    described in PHASE_8_8B_REPORT_PRE.md §8.7.
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


def _build_zeroed_axis_summary() -> dict[str, Any]:
    """Build the ``axis_summary`` block with zeroed counters for CODE-A.

    Only ``final_gate`` is populated meaningfully; the other three
    axes return shape-compatible zero counters so consumers can rely
    on every key being present from day one. The full population of
    cve_lite / source_quality / claim_entailment arrives in CODE-B.
    """
    return {
        "cve_lite": {
            "verified_claims_count": 0,
            "unverified_claims_count": 0,
            "inconclusive_count": 0,
        },
        "source_quality": {
            "strong_count": 0,
            "adequate_count": 0,
            "weak_count": 0,
            "unsuitable_count": 0,
            "unknown_count": 0,
            "missing_count": 0,
        },
        "claim_entailment": {
            "entailed_count": 0,
            "partially_supported_count": 0,
            "not_supported_count": 0,
            "contradicted_count": 0,
            "uncertain_count": 0,
            "missing_count": 0,
        },
        "final_gate": {
            "has_blocking_gaps": False,
            "has_warnings": False,
            "blocking_gap_count": 0,
            "warning_gap_count": 0,
        },
    }


# ---------------------------------------------------------------------------
# Mock indicators
# ---------------------------------------------------------------------------
def _build_mock_indicators(
    draft: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``mock_indicators`` section.

    Today every axis ships with a mock implementation in MVP-0
    (PROVIDERS_ENABLED=mock, MAX_COST_PER_TASK=0). The base
    indicators are therefore ``True`` by default. The one indicator
    we can derive non-trivially from CODE-A's surface area is
    ``uses_mock_compiler``, which inspects
    ``draft_final_answers.compiler_name``:
      - if a draft exists and its ``compiler_name`` matches the mock
        compiler shipped today, ``uses_mock_compiler`` is True;
      - if a draft exists with a different ``compiler_name``, the
        compiler has been upgraded for this task and the indicator
        flips to False;
      - if no draft exists yet, we keep True as the MVP-0 fallback
        and add an explanatory note (the compiler simply has not run
        yet).

    The remaining axes (source_quality / claim_entailment / cve_lite)
    will be derived from real per-record provenance in CODE-B; for
    CODE-A they remain at the MVP-0 fallback ``True`` value because
    we do not yet inspect those tables here.

    ``notes`` carries the mandatory anti-hallucination disclaimers.
    Consumers MUST display at least these notes verbatim alongside
    any rendering of the report.
    """
    notes: list[str] = [
        "Una fonte citata non implica un claim vero.",
        "Una quote testualmente presente non implica che la quote "
        "sostenga il claim.",
        "Un verdict 'entailed' non implica che il claim sia vero nel "
        "mondo.",
        "Il payload JSONB è esposto verbatim; RBAC/redaction non "
        "implementata.",
        "Lifecycle/source-loss event details are not included in "
        "CODE-A; coverage_gap_statements with kind='source_loss', if "
        "present, may still appear in gate.coverage_gaps.",
        "claims/evidence/CVE-lite/Source Quality/Claim Entailment "
        "aggregation sarà completata in 8.8B-REPORT-CODE-B.",
    ]

    uses_mock_compiler: bool
    if draft is None:
        # No draft yet: the compiler simply has not run. The MVP-0
        # default is mock, so we keep True and add a disambiguating
        # note so consumers do not mistake this for "we proved the
        # compiler is mock by inspecting a draft".
        uses_mock_compiler = True
        notes.append(
            "uses_mock_compiler reflects the MVP-0 fallback: no draft "
            "has been compiled yet for this task."
        )
    else:
        compiler_name = draft.get("compiler_name")
        uses_mock_compiler = compiler_name == _MOCK_COMPILER_NAME

    return {
        "uses_mock_source_quality": True,
        "uses_mock_claim_entailment": True,
        "uses_mock_compiler": uses_mock_compiler,
        "uses_mock_cve_lite": True,
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
        "CODE-A; eventuali coverage_gap_statements con "
        "kind='source_loss' possono comunque comparire in "
        "gate.coverage_gaps.",
        "CODE-A non include ancora claims/evidence/CVE-lite/Source "
        "Quality/Claim Entailment aggregation; arriverà in "
        "8.8B-REPORT-CODE-B.",
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
    ``draft_final_answers``. To keep CODE-A minimal we surface the
    published-answer-side ``content_hash`` (which is derived from
    summary_text per the gate's invariant) and leave ``summary_text``
    as ``None`` for now; CODE-B can fold in the draft's summary when
    the broader aggregation is wired.
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
    """Return the task-level Anti-Hallucination Report (CODE-A skeleton).

    Behavior summary:
      - 404 RESOURCE_NOT_FOUND with ``details.resource="task_masters"``
        when the task does not exist (immediate, no further SELECTs).
      - 200 in every other case, with the shape documented in
        PHASE_8_8B_REPORT_PRE.md §4 (top-level keys: ``task_id``,
        ``project_id``, ``tenant_id``, ``task``, ``publication``,
        ``gate``, ``claims``, ``evidence``, ``axis_summary``,
        ``mock_indicators``, ``limitations``).
      - ``claims`` and ``evidence`` are empty lists in CODE-A. The
        full per-claim aggregation (CVE-lite, Source Quality,
        Claim Entailment) arrives in CODE-B.
      - ``axis_summary.final_gate`` is derived from the coverage
        gaps attached to the latest draft. The other three axes
        carry zeroed counters (CODE-B).
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
    """
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

    axis_summary = _build_zeroed_axis_summary()
    axis_summary["final_gate"] = _build_axis_summary_final_gate(
        coverage_gaps_view
    )

    mock_indicators = _build_mock_indicators(draft)

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
        "claims": [],
        "evidence": [],
        "axis_summary": axis_summary,
        "mock_indicators": mock_indicators,
        "limitations": _limitations(),
    }
