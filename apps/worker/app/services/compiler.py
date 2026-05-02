"""Deterministic, mock-driven compiler (Phase 8.4).

For each task in state 'analyzed_partial' (or resuming from 'compiling') with
attached documents, this module:

  1. selects the LATEST claim_ledger_entries per claim_logical_id of the task,
     filtered to state='verified_fact', ordered deterministically by
     (logical_claims.created_at, logical_claims.id);

  2. builds a deterministic summary_text by concatenating the canonical_claim_text
     of each verified claim, separated by '\\n', with a trailing '\\n' if at
     least one claim is present (so each span ends on a newline boundary);

  3. records:
       - agent_runs (run_kind='compile_draft', attempt_no=1, status='succeeded');
       - draft_final_answers (task_id, version_no=1);
       - final_answer_spans 1:1 with verified claims (span_index in insertion order);
       - final_answer_span_claim_links with link_role='primary_support'.

Phase 8.4 invariants honored:

  - Compiler creates final_answer_spans ONLY from claim_ledger_entries.state =
    'verified_fact' (latest per logical_claim). Never from unverifiable, disputed,
    rejected or any other state.

  - Zero-verified case: draft_final_answers v1 is created with summary_text=''
    and zero final_answer_spans. The gate (separate module) is responsible for
    emitting decision='rejected' with reason_code='no_verified_claims' and the
    coverage_gap_statements row. The compiler does NOT decide.

  - claim_ledger_entries is append-only: this module performs zero UPDATE/DELETE
    on it.

  - agent_outputs is intentionally NOT populated in 8.4. The pipeline is
    deterministic and does not pass through agent completions.

Idempotency:

  - All INSERTs use ON CONFLICT DO NOTHING on the appropriate UNIQUE constraints
    declared in 0005_answers_gate.sql:
      * agent_runs: (task_id, run_kind, attempt_no)
      * draft_final_answers: (task_id, version_no)
      * final_answer_spans: (draft_final_answer_id, span_index)
      * final_answer_span_claim_links: (final_answer_span_id, claim_ledger_entry_id)

  - Re-running run_compilation on the same task is a no-op after the first
    successful execution, regardless of how many times the consumer is replayed
    or whether the task is in 'analyzed_partial' or 'compiling'.

Returns: dict with deterministic counts useful for audit payloads:
  {
    "verified_claim_count": int,
    "draft_created": bool,
    "spans_inserted": int,
    "links_inserted": int,
    "summary_chars": int,
    "draft_id": str | None,
    "agent_run_id": str,
  }
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


COMPILER_NAME = "mvp0_compiler_v1"
COMPILER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _select_verified_latest_for_task(conn: Connection, task_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return the LATEST claim_ledger_entries (by version_no DESC) per
    claim_logical_id, restricted to the given task and to state='verified_fact'.

    Deterministic ordering: (logical_claims.created_at ASC, logical_claims.id ASC).
    """
    rows = conn.execute(
        text(
            """
            SELECT
              lc.id                  AS claim_logical_id,
              lc.canonical_claim_text AS canonical_claim_text,
              latest.entry_id        AS claim_ledger_entry_id,
              latest.version_no      AS version_no,
              lc.created_at          AS lc_created_at
            FROM logical_claims lc
            JOIN LATERAL (
              SELECT cle.id AS entry_id, cle.version_no, cle.state
              FROM claim_ledger_entries cle
              WHERE cle.claim_logical_id = lc.id
              ORDER BY cle.version_no DESC
              LIMIT 1
            ) latest ON TRUE
            WHERE lc.task_id = :tid
              AND latest.state = 'verified_fact'
            ORDER BY lc.created_at ASC, lc.id ASC
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _upsert_compile_draft_run(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> uuid.UUID:
    """Idempotent upsert of an agent_runs row for run_kind='compile_draft', attempt_no=1.

    Returns the canonical agent_runs.id (existing or newly inserted).
    """
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO agent_runs (
              id, tenant_id, project_id, task_id,
              run_kind, attempt_no, status, payload
            ) VALUES (
              :id, :t, :p, :tid,
              'compile_draft', 1, 'succeeded',
              jsonb_build_object('compiler_name', CAST(:cn AS TEXT), 'compiler_version', CAST(:cv AS TEXT))
            )
            ON CONFLICT (task_id, run_kind, attempt_no) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "t": tenant_id,
            "p": project_id,
            "tid": task_id,
            "cn": COMPILER_NAME,
            "cv": COMPILER_VERSION,
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0]))
    row = conn.execute(
        text(
            """
            SELECT id FROM agent_runs
            WHERE task_id = :tid AND run_kind = 'compile_draft' AND attempt_no = 1
            """
        ),
        {"tid": task_id},
    ).one()
    return uuid.UUID(str(row[0]))


def _upsert_draft_v1(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    summary_text: str,
    verified_claim_count: int,
) -> tuple[uuid.UUID, bool]:
    """Idempotent upsert of draft_final_answers v1 for the task.

    Returns (draft_id, inserted_flag).
    """
    candidate_id = uuid.uuid4()
    payload = {
        "compiler_name": COMPILER_NAME,
        "compiler_version": COMPILER_VERSION,
        "verified_claim_count": int(verified_claim_count),
    }
    inserted = conn.execute(
        text(
            """
            INSERT INTO draft_final_answers (
              id, task_id, version_no,
              compiler_name, compiler_version,
              summary_text, payload
            ) VALUES (
              :id, :tid, 1,
              :cn, :cv,
              :st, CAST(:pl AS JSONB)
            )
            ON CONFLICT (task_id, version_no) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "tid": task_id,
            "cn": COMPILER_NAME,
            "cv": COMPILER_VERSION,
            "st": summary_text,
            "pl": _jsonb(payload),
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            "SELECT id FROM draft_final_answers WHERE task_id = :tid AND version_no = 1"
        ),
        {"tid": task_id},
    ).one()
    return uuid.UUID(str(row[0])), False


def _upsert_final_span(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    span_index: int,
    char_start: int,
    char_end: int,
    span_text: str,
) -> tuple[uuid.UUID, bool]:
    """Idempotent upsert of one final_answer_spans row.

    Returns (span_id, inserted_flag).
    """
    candidate_id = uuid.uuid4()
    span_hash = hashlib.sha256(span_text.encode("utf-8")).hexdigest()
    inserted = conn.execute(
        text(
            """
            INSERT INTO final_answer_spans (
              id, draft_final_answer_id, span_index,
              char_start, char_end, span_text, span_hash
            ) VALUES (
              :id, :did, :idx,
              :cs, :ce, :st, :sh
            )
            ON CONFLICT (draft_final_answer_id, span_index) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "did": draft_id,
            "idx": span_index,
            "cs": char_start,
            "ce": char_end,
            "st": span_text,
            "sh": span_hash,
        },
    ).first()
    if inserted is not None:
        return uuid.UUID(str(inserted[0])), True
    row = conn.execute(
        text(
            """
            SELECT id FROM final_answer_spans
            WHERE draft_final_answer_id = :did AND span_index = :idx
            """
        ),
        {"did": draft_id, "idx": span_index},
    ).one()
    return uuid.UUID(str(row[0])), False


def _upsert_span_claim_link(
    conn: Connection,
    *,
    span_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
) -> bool:
    """Idempotent upsert of one final_answer_span_claim_links row.

    Returns inserted_flag.
    """
    candidate_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO final_answer_span_claim_links (
              id, final_answer_span_id, claim_ledger_entry_id,
              claim_logical_id, link_role
            ) VALUES (
              :id, :span, :entry, :lc, 'primary_support'
            )
            ON CONFLICT (final_answer_span_id, claim_ledger_entry_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": candidate_id,
            "span": span_id,
            "entry": claim_ledger_entry_id,
            "lc": claim_logical_id,
        },
    ).first()
    return inserted is not None


def _jsonb(payload: dict[str, Any]) -> str:
    """Serialize a small payload deterministically for JSONB storage."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------
def run_compilation(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    """Run the deterministic mock-driven compilation pipeline. Idempotent.

    Returns:
      {
        "verified_claim_count": int,
        "draft_created": bool,
        "spans_inserted": int,
        "links_inserted": int,
        "summary_chars": int,
        "draft_id": str | None,
        "agent_run_id": str,
      }
    """
    # 1) Track the run (idempotent).
    agent_run_id = _upsert_compile_draft_run(
        conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )

    # 2) Select verified claims (latest per logical, deterministic order).
    verified = _select_verified_latest_for_task(conn, task_id)
    verified_claim_count = len(verified)

    # 3) Build summary_text deterministically.
    if verified_claim_count == 0:
        summary_text = ""
    else:
        summary_text = "".join(
            f"{row['canonical_claim_text']}\n" for row in verified
        )

    # 4) Upsert draft v1.
    draft_id, draft_created = _upsert_draft_v1(
        conn,
        task_id=task_id,
        summary_text=summary_text,
        verified_claim_count=verified_claim_count,
    )

    # 5) Compute span offsets and upsert spans + links 1:1.
    spans_inserted = 0
    links_inserted = 0
    cursor = 0
    for idx, row in enumerate(verified):
        text_value: str = str(row["canonical_claim_text"])
        line = f"{text_value}\n"
        char_start = cursor
        char_end = cursor + len(line)
        cursor = char_end

        span_id, span_was_inserted = _upsert_final_span(
            conn,
            draft_id=draft_id,
            span_index=idx,
            char_start=char_start,
            char_end=char_end,
            span_text=line,
        )
        if span_was_inserted:
            spans_inserted += 1

        link_was_inserted = _upsert_span_claim_link(
            conn,
            span_id=span_id,
            claim_ledger_entry_id=uuid.UUID(str(row["claim_ledger_entry_id"])),
            claim_logical_id=uuid.UUID(str(row["claim_logical_id"])),
        )
        if link_was_inserted:
            links_inserted += 1

    return {
        "verified_claim_count": verified_claim_count,
        "draft_created": draft_created,
        "spans_inserted": spans_inserted,
        "links_inserted": links_inserted,
        "summary_chars": len(summary_text),
        "draft_id": str(draft_id),
        "agent_run_id": str(agent_run_id),
    }
