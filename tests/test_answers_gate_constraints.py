"""Constraint-level tests on Answers Gate schema (Phase 8.4, root, DB-only).

Coverage:
  - final_answer_spans APPEND-ONLY (UPDATE / DELETE rejected by trigger).
  - final_gate_reports APPEND-ONLY (UPDATE / DELETE rejected by trigger).
  - UNIQUE (task_id, version_no) on draft_final_answers.
  - UNIQUE (task_id, version_no) on published_answers.
  - UNIQUE composite (id, task_id) on draft_final_answers (FK target).
  - UNIQUE composite (id, task_id, draft_final_answer_id) on final_gate_reports
    (FK target).
  - UNIQUE composite (id, task_id) on published_answers (introspective check).
  - UNIQUE (draft_final_answer_id) on final_gate_reports.
  - coverage_gap_statements idempotency on (draft_final_answer_id, kind, gap_key).
  - lc_block_delete_if_published: DELETE on logical_claims referenced by an
    active published_answers must fail with RaiseException; conversely, DELETE
    on a logical_claims NOT referenced by any active published_answers must
    actually succeed and remove the row.
  - task_masters.status CHECK accepts 'compiling' and 'published'; rejects
    arbitrary values like 'publication_held'.
  - Composite consistency constraints exist on final_gate_reports and
    published_answers (referential integrity to draft and gate).
  - published_answers no-self-supersede CHECK.

Rerun-safety:
  All identifiers/hashes are unique per invocation.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import psycopg
from sqlalchemy import text  # noqa: F401 (kept for parity with sibling tests)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ensure_migrations(db_conn) -> None:
    spec = importlib.util.spec_from_file_location(
        "migrate_module", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    rc = module.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_dev(cur) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per test invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
    cur.execute(
        "INSERT INTO tenants (name, slug, status) VALUES ('Dev','dev','active') "
        "ON CONFLICT (slug) DO NOTHING RETURNING id"
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM tenants WHERE slug = 'dev'")
        row = cur.fetchone()
    tenant_id = uuid.UUID(str(row[0]))

    cur.execute(
        "INSERT INTO users (tenant_id, email, display_name, status) "
        "VALUES (%s,'dev@local','Dev','active') "
        "ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id",
        (tenant_id,),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND email = 'dev@local'",
            (tenant_id,),
        )
        row = cur.fetchone()
    user_id = uuid.UUID(str(row[0]))

    project_name = f"answers-gate-test-{uuid.uuid4()}"
    cur.execute(
        "INSERT INTO projects (tenant_id, name, mode_default) "
        "VALUES (%s, %s, 'closed_corpus') RETURNING id",
        (tenant_id, project_name),
    )
    project_id = uuid.UUID(str(cur.fetchone()[0]))

    cur.execute(
        """
        INSERT INTO task_masters
            (tenant_id, project_id, created_by, mode, objective, status)
        VALUES (%s, %s, %s, 'closed_corpus', %s, 'created')
        RETURNING id
        """,
        (tenant_id, project_id, user_id, f"obj-{uuid.uuid4()}"),
    )
    task_id = uuid.UUID(str(cur.fetchone()[0]))
    return tenant_id, project_id, user_id, task_id


def _make_logical_claim(
    cur, *, tenant_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO logical_claims
            (id, tenant_id, project_id, task_id,
             canonical_claim_text, canonical_claim_hash)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tenant_id,
            project_id,
            task_id,
            f"canonical-{uuid.uuid4()}",
            _unique_hash(),
        ),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_ledger_v(
    cur,
    *,
    claim_logical_id: uuid.UUID,
    version_no: int,
    state: str,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO claim_ledger_entries
            (id, claim_logical_id, version_no, state,
             support_scope, user_provided_dependency,
             transition_reason)
        VALUES (gen_random_uuid(), %s, %s, %s,
                'supported_by_user_corpus_only',
                'supported_by_user_corpus_only',
                %s)
        RETURNING id
        """,
        (claim_logical_id, version_no, state, f"reason-{uuid.uuid4()}"),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_draft(cur, *, task_id: uuid.UUID, version_no: int = 1) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO draft_final_answers
            (id, task_id, version_no, compiler_name, compiler_version, summary_text)
        VALUES (gen_random_uuid(), %s, %s, 'mvp0_compiler_v1', '0.1.0', %s)
        RETURNING id
        """,
        (task_id, version_no, f"summary-{uuid.uuid4()}"),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_final_span(
    cur, *, draft_id: uuid.UUID, span_index: int, span_text: str
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO final_answer_spans
            (id, draft_final_answer_id, span_index, char_start, char_end, span_text, span_hash)
        VALUES (gen_random_uuid(), %s, %s, 0, %s, %s, %s)
        RETURNING id
        """,
        (draft_id, span_index, len(span_text), span_text, _unique_hash()),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_span_claim_link(
    cur,
    *,
    span_id: uuid.UUID,
    ledger_entry_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO final_answer_span_claim_links
            (id, final_answer_span_id, claim_ledger_entry_id, claim_logical_id, link_role)
        VALUES (gen_random_uuid(), %s, %s, %s, 'primary_support')
        RETURNING id
        """,
        (span_id, ledger_entry_id, claim_logical_id),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_gate_report(
    cur,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    decision: str = "approved",
    reason_code: str = "all_spans_verified",
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO final_gate_reports
            (id, task_id, draft_final_answer_id, decision, reason_code)
        VALUES (gen_random_uuid(), %s, %s, %s, %s)
        RETURNING id
        """,
        (task_id, draft_id, decision, reason_code),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_published_answer(
    cur,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    gate_report_id: uuid.UUID,
    version_no: int = 1,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO published_answers
            (id, task_id, draft_final_answer_id, final_gate_report_id,
             version_no, content_hash, status)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 'published')
        RETURNING id
        """,
        (task_id, draft_id, gate_report_id, version_no, _unique_hash()),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_coverage_gap(
    cur,
    *,
    draft_id: uuid.UUID,
    kind: str,
    gap_key: str,
    severity: str = "block",
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO coverage_gap_statements
            (id, draft_final_answer_id, kind, severity, gap_key, details)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (draft_id, kind, severity, gap_key),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _constraint_exists(cur, *, table: str, conname: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = %s::regclass
          AND conname  = %s
        """,
        (table, conname),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# task_masters.status CHECK
# ---------------------------------------------------------------------------
def test_task_status_accepts_compiling_and_published(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()

    cur.execute("UPDATE task_masters SET status = 'compiling' WHERE id = %s", (task_id,))
    db_conn.commit()
    cur.execute("UPDATE task_masters SET status = 'published' WHERE id = %s", (task_id,))
    db_conn.commit()

    cur.execute("SELECT status FROM task_masters WHERE id = %s", (task_id,))
    assert str(cur.fetchone()[0]) == "published"


def test_task_status_rejects_unknown_value(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE task_masters SET status = 'publication_held' WHERE id = %s",
            (task_id,),
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# draft_final_answers UNIQUE
# ---------------------------------------------------------------------------
def test_draft_final_answers_unique_task_version(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_draft(cur, task_id=task_id, version_no=1)
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# final_answer_spans append-only + UNIQUE
# ---------------------------------------------------------------------------
def test_final_answer_spans_reject_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    span_id = _insert_final_span(cur, draft_id=draft_id, span_index=0, span_text="hello")
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE final_answer_spans SET span_text = 'mutated' WHERE id = %s",
            (span_id,),
        )
        db_conn.commit()
    db_conn.rollback()


def test_final_answer_spans_reject_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    span_id = _insert_final_span(cur, draft_id=draft_id, span_index=0, span_text="hello")
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM final_answer_spans WHERE id = %s", (span_id,))
        db_conn.commit()
    db_conn.rollback()


def test_final_answer_spans_unique_index_per_draft(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    _insert_final_span(cur, draft_id=draft_id, span_index=0, span_text="a")
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_final_span(cur, draft_id=draft_id, span_index=0, span_text="b")
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# final_gate_reports append-only + UNIQUE per draft
# ---------------------------------------------------------------------------
def test_final_gate_reports_reject_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    gate_id = _insert_gate_report(cur, task_id=task_id, draft_id=draft_id)
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE final_gate_reports SET decision = 'rejected' WHERE id = %s",
            (gate_id,),
        )
        db_conn.commit()
    db_conn.rollback()


def test_final_gate_reports_reject_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    gate_id = _insert_gate_report(cur, task_id=task_id, draft_id=draft_id)
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM final_gate_reports WHERE id = %s", (gate_id,))
        db_conn.commit()
    db_conn.rollback()


def test_final_gate_reports_unique_per_draft(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    _insert_gate_report(cur, task_id=task_id, draft_id=draft_id, decision="approved")
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_gate_report(cur, task_id=task_id, draft_id=draft_id, decision="rejected")
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# published_answers UNIQUE (task, version) + composite (id, task) + no-self-supersede
# ---------------------------------------------------------------------------
def test_published_answers_unique_task_version(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    gate_id = _insert_gate_report(cur, task_id=task_id, draft_id=draft_id)
    db_conn.commit()
    _insert_published_answer(
        cur, task_id=task_id, draft_id=draft_id, gate_report_id=gate_id, version_no=1
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_published_answer(
            cur, task_id=task_id, draft_id=draft_id, gate_report_id=gate_id, version_no=1
        )
        db_conn.commit()
    db_conn.rollback()


def test_published_answers_no_self_supersede_check(db_conn):
    """superseded_by_id must not equal id."""
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    gate_id = _insert_gate_report(cur, task_id=task_id, draft_id=draft_id)
    pa_id = _insert_published_answer(
        cur, task_id=task_id, draft_id=draft_id, gate_report_id=gate_id, version_no=1
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE published_answers SET superseded_by_id = %s WHERE id = %s",
            (pa_id, pa_id),
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# coverage_gap_statements: UNIQUE (draft, kind, gap_key)
# ---------------------------------------------------------------------------
def test_coverage_gap_statements_unique_per_kind_gap_key(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()
    _insert_coverage_gap(
        cur, draft_id=draft_id, kind="missing_evidence", gap_key="no_verified_claims"
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_coverage_gap(
            cur, draft_id=draft_id, kind="missing_evidence", gap_key="no_verified_claims"
        )
        db_conn.commit()
    db_conn.rollback()


def test_coverage_gap_statements_distinct_kinds_or_keys_allowed(db_conn):
    """Different (kind, gap_key) combinations must coexist on the same draft."""
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()
    _insert_coverage_gap(
        cur, draft_id=draft_id, kind="missing_evidence", gap_key="no_verified_claims"
    )
    _insert_coverage_gap(
        cur, draft_id=draft_id, kind="unverified_claim", gap_key=f"span:{uuid.uuid4()}"
    )
    _insert_coverage_gap(
        cur, draft_id=draft_id, kind="missing_evidence", gap_key=f"other:{uuid.uuid4()}"
    )
    db_conn.commit()
    cur.execute(
        "SELECT COUNT(*) FROM coverage_gap_statements WHERE draft_final_answer_id = %s",
        (draft_id,),
    )
    assert int(cur.fetchone()[0]) == 3


# ---------------------------------------------------------------------------
# Composite consistency constraints exist (FK targets and FKs themselves)
# ---------------------------------------------------------------------------
def test_composite_consistency_constraints_present(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    # UNIQUE composite targets:
    assert _constraint_exists(cur, table="draft_final_answers", conname="draft_final_answers_id_task_uq")
    assert _constraint_exists(cur, table="final_gate_reports", conname="final_gate_reports_id_task_draft_uq")
    assert _constraint_exists(cur, table="published_answers",  conname="published_answers_id_task_uq")

    # FK composite consistency (referencing constraints):
    assert _constraint_exists(cur, table="final_gate_reports", conname="final_gate_reports_draft_consistency")
    assert _constraint_exists(cur, table="published_answers",  conname="published_answers_draft_consistency")
    assert _constraint_exists(cur, table="published_answers",  conname="published_answers_gate_consistency")

    # No-self-supersede CHECK:
    assert _constraint_exists(cur, table="published_answers",  conname="published_answers_no_self_supersede")


def test_final_gate_reports_rejects_task_id_mismatch_with_draft(db_conn):
    """Inserting a final_gate_reports with task_id different from the draft's
    task_id must fail thanks to the composite FK final_gate_reports_draft_consistency.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    # Two distinct tasks under the same project.
    tenant_id, project_id, user_id, task_a = _seed_dev(cur)
    db_conn.commit()
    cur.execute(
        """
        INSERT INTO task_masters (tenant_id, project_id, created_by, mode, objective, status)
        VALUES (%s, %s, %s, 'closed_corpus', %s, 'created')
        RETURNING id
        """,
        (tenant_id, project_id, user_id, f"obj-{uuid.uuid4()}"),
    )
    task_b = uuid.UUID(str(cur.fetchone()[0]))
    draft_a = _insert_draft(cur, task_id=task_a, version_no=1)
    db_conn.commit()

    # Try to attach a gate report for draft_a but with task_b -> must fail
    # because (draft_final_answer_id, task_id) FK targets draft.(id, task_id).
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        cur.execute(
            """
            INSERT INTO final_gate_reports
                (id, task_id, draft_final_answer_id, decision, reason_code)
            VALUES (gen_random_uuid(), %s, %s, 'approved', 'mismatch_attempt')
            """,
            (task_b, draft_a),
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# lc_block_delete_if_published — both branches actually executed
# ---------------------------------------------------------------------------
def test_lc_block_delete_if_published_blocks_delete_when_active(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()

    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="verified_fact")
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    span_id = _insert_final_span(cur, draft_id=draft_id, span_index=0, span_text="x")
    _insert_span_claim_link(cur, span_id=span_id, ledger_entry_id=v1, claim_logical_id=lc)
    gate_id = _insert_gate_report(cur, task_id=task_id, draft_id=draft_id)
    _insert_published_answer(cur, task_id=task_id, draft_id=draft_id, gate_report_id=gate_id)
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM logical_claims WHERE id = %s", (lc,))
        db_conn.commit()
    db_conn.rollback()


def test_lc_block_delete_if_published_allows_delete_when_no_publication(db_conn):
    """When the logical_claim has no associated published_answers, DELETE must
    succeed and the row must disappear.

    Setup: a fresh logical_claim with NO ledger entry, NO draft, NO span, NO
    publication. Other tables (raw_claims, classified_claims, evidence_links,
    verification_records) are NOT populated for this claim, so no ON DELETE
    RESTRICT FK can prevent the deletion.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()

    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    db_conn.commit()

    cur.execute("DELETE FROM logical_claims WHERE id = %s", (lc,))
    db_conn.commit()

    cur.execute("SELECT 1 FROM logical_claims WHERE id = %s", (lc,))
    assert cur.fetchone() is None
