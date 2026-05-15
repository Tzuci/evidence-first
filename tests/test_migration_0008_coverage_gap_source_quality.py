"""Constraint-level tests for migration 0008_coverage_gap_source_quality.

Coverage (per Block 8.7G-1 requirements):

  1. coverage_gap_statements accepts kind='source_quality_block'.
  2. coverage_gap_statements accepts kind='source_quality_warning'.
  3. coverage_gap_statements still accepts the legacy kinds:
       - 'unverified_claim'
       - 'missing_evidence'
       - 'out_of_scope'
       - 'source_loss'
  4. coverage_gap_statements rejects an unknown kind.
  5. UNIQUE (draft_final_answer_id, kind, gap_key) keeps working:
       - same (draft, kind, gap_key) -> rejected;
       - same draft + different kind + same gap_key -> allowed.
  6. severity='warn' works with kind='source_quality_warning'.
  7. severity='block' works with kind='source_quality_block'.
  8. The migration does NOT touch source_quality_assessments (the table's
     constraint topology is identical before and after the migration is
     applied).
  9. The migration does NOT touch final_gate_reports (same check as 8).
 10. Rerun-safe: every invocation uses fresh UUIDs.

Conventions (per tests/README.md):
  - Local helpers only; no imports from other test files.
  - No ORM, no subprocess.
  - psycopg connection from the shared db_conn fixture.
  - sqlalchemy.text() not strictly needed here; we use psycopg cursors with
    bound parameters as in test_answers_gate_constraints.py.
  - Migrations are guaranteed to be applied via the _ensure_migrations
    helper that reloads scripts/migrate.py as a module.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_migrations(db_conn) -> None:
    """Apply all pending migrations idempotently.

    Mirrors the helper used in test_answers_gate_constraints.py so that a
    fresh dev DB is brought up to the latest schema (including 0008) without
    requiring the operator to run `make migrate` manually.
    """
    spec = importlib.util.spec_from_file_location(
        "migrate_module_0008", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    rc = module.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0


def _unique_hash() -> str:
    """Return a deterministically-unique hex blob for *_hash columns."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_dev(cur) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per invocation.

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

    project_name = f"mig-0008-test-{uuid.uuid4()}"
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


# Whitelist of tables we are allowed to introspect via _count_constraints.
# Hard-coded to avoid any risk of SQL injection through f-string interpolation.
_ALLOWED_TABLES_FOR_INTROSPECTION: frozenset[str] = frozenset({
    "coverage_gap_statements",
    "source_quality_assessments",
    "final_gate_reports",
})


def _count_check_constraints(cur, *, table: str) -> int:
    """Return the number of CHECK constraints on `table`.

    Used to assert that migration 0008 does not change the constraint
    topology of tables it must not touch (source_quality_assessments,
    final_gate_reports).
    """
    if table not in _ALLOWED_TABLES_FOR_INTROSPECTION:
        raise ValueError(f"table not whitelisted for introspection: {table!r}")
    cur.execute(
        """
        SELECT COUNT(*)
          FROM pg_constraint
         WHERE conrelid = %s::regclass
           AND contype  = 'c'
        """,
        (table,),
    )
    return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Test 1 + 2 — new kinds accepted
# ---------------------------------------------------------------------------
def test_kind_source_quality_block_is_accepted(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_block",
        gap_key=f"span:{uuid.uuid4()}:source_quality_block",
        severity="block",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    assert str(cur.fetchone()[0]) == "source_quality_block"


def test_kind_source_quality_warning_is_accepted(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_warning",
        gap_key=f"span:{uuid.uuid4()}:source_quality_warning",
        severity="warn",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    assert str(cur.fetchone()[0]) == "source_quality_warning"


# ---------------------------------------------------------------------------
# Test 3 — legacy kinds still accepted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "legacy_kind",
    ["unverified_claim", "missing_evidence", "out_of_scope", "source_loss"],
)
def test_legacy_kinds_still_accepted(db_conn, legacy_kind):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind=legacy_kind,
        gap_key=f"legacy:{legacy_kind}:{uuid.uuid4()}",
        severity="block",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    assert str(cur.fetchone()[0]) == legacy_kind


# ---------------------------------------------------------------------------
# Test 4 — invalid kinds still rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_kind",
    [
        "source_quality",            # missing suffix
        "source_quality_blocked",    # typo
        "source_quality_warn",       # typo
        "unverified",                # truncated
        "publication_held",          # event name, not a kind
        "",                          # empty
    ],
)
def test_unknown_kind_is_rejected(db_conn, bad_kind):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_coverage_gap(
            cur,
            draft_id=draft_id,
            kind=bad_kind,
            gap_key=f"bad:{uuid.uuid4()}",
            severity="block",
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# Test 5 — UNIQUE (draft, kind, gap_key) still works
# ---------------------------------------------------------------------------
def test_unique_kind_gap_key_rejects_exact_duplicate(db_conn):
    """Same (draft_id, kind, gap_key) must be rejected by the existing
    UNIQUE composite constraint, including for the new kinds.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_key = f"span:{uuid.uuid4()}"

    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_block",
        gap_key=gap_key,
        severity="block",
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_coverage_gap(
            cur,
            draft_id=draft_id,
            kind="source_quality_block",
            gap_key=gap_key,
            severity="block",
        )
        db_conn.commit()
    db_conn.rollback()


def test_unique_kind_gap_key_allows_same_gap_key_with_different_kind(db_conn):
    """Same draft + same gap_key but different kind must coexist.

    This is the explicit invariant the Gate will rely on in 8.7G-CODE when
    it emits, for the same span, both a 'source_quality_warning' (from the
    warning branch) and possibly a legacy 'unverified_claim' (from the CVE
    branch on a different occasion). The UNIQUE composite includes kind,
    so collision is per-kind.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    shared_gap_key = f"span:{uuid.uuid4()}"

    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_block",
        gap_key=shared_gap_key,
        severity="block",
    )
    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_warning",
        gap_key=shared_gap_key,
        severity="warn",
    )
    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="unverified_claim",
        gap_key=shared_gap_key,
        severity="block",
    )
    db_conn.commit()

    cur.execute(
        """
        SELECT COUNT(*)
          FROM coverage_gap_statements
         WHERE draft_final_answer_id = %s
           AND gap_key = %s
        """,
        (draft_id, shared_gap_key),
    )
    assert int(cur.fetchone()[0]) == 3


# ---------------------------------------------------------------------------
# Test 6 + 7 — severity pairings with the new kinds
# ---------------------------------------------------------------------------
def test_severity_warn_works_with_source_quality_warning(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_warning",
        gap_key=f"span:{uuid.uuid4()}:source_quality_warning",
        severity="warn",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind, severity FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    row = cur.fetchone()
    assert (str(row[0]), str(row[1])) == ("source_quality_warning", "warn")


def test_severity_block_works_with_source_quality_block(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_block",
        gap_key=f"span:{uuid.uuid4()}:source_quality_block",
        severity="block",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind, severity FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    row = cur.fetchone()
    assert (str(row[0]), str(row[1])) == ("source_quality_block", "block")


# ---------------------------------------------------------------------------
# Test 8 + 9 — migration does not touch unrelated tables
# ---------------------------------------------------------------------------
def test_migration_does_not_touch_source_quality_assessments(db_conn):
    """source_quality_assessments must still have exactly the CHECK
    constraints declared by 0007: target XOR, version_no >= 1, confidence
    range, and the nine codomain CHECKs. We assert the EXACT expected count
    and a few representative names exist.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    # 0007 declares 12 CHECK constraints on source_quality_assessments:
    #   sqa_target_xor, sqa_version_no_chk, sqa_confidence_range, and the
    #   nine codomain CHECKs (sqa_source_type_chk, sqa_source_role_chk,
    #   sqa_authority_level_chk, sqa_independence_level_chk,
    #   sqa_freshness_chk, sqa_relevance_chk, sqa_extract_quality_chk,
    #   sqa_contradiction_status_chk, sqa_overall_quality_chk).
    n = _count_check_constraints(cur, table="source_quality_assessments")
    assert n == 12, (
        f"source_quality_assessments has {n} CHECK constraints; expected 12. "
        "Migration 0008 must not modify this table."
    )

    # Sanity: a representative subset of CHECK names still exists.
    expected_subset = {
        "sqa_target_xor",
        "sqa_overall_quality_chk",
        "sqa_contradiction_status_chk",
        "sqa_confidence_range",
    }
    cur.execute(
        """
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'source_quality_assessments'::regclass
           AND contype  = 'c'
        """,
    )
    names = {str(r[0]) for r in cur.fetchall()}
    missing = expected_subset - names
    assert not missing, f"missing expected CHECKs on source_quality_assessments: {missing}"


def test_migration_does_not_touch_final_gate_reports(db_conn):
    """final_gate_reports must still have exactly its CHECK constraint on
    `decision` (from 0005). We assert the topology is unchanged.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    # 0005 declares 1 CHECK constraint on final_gate_reports: the inline
    # CHECK on decision IN ('approved','rejected','held_for_review').
    n = _count_check_constraints(cur, table="final_gate_reports")
    assert n == 1, (
        f"final_gate_reports has {n} CHECK constraints; expected 1. "
        "Migration 0008 must not modify this table."
    )


# ---------------------------------------------------------------------------
# Additional safety: the new CHECK on coverage_gap_statements.kind is
# explicitly named, so future migrations can target it without DO-block
# discovery.
# ---------------------------------------------------------------------------
def test_new_kind_check_constraint_is_explicitly_named(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'coverage_gap_statements'::regclass
           AND conname  = 'coverage_gap_statements_kind_check'
           AND contype  = 'c'
        """,
    )
    assert cur.fetchone() is not None, (
        "Expected an explicitly-named CHECK constraint "
        "'coverage_gap_statements_kind_check' after 0008. "
        "If the migration runner did not apply 0008, run `make migrate`."
    )
