"""Constraint-level tests for migration 0010_coverage_gap_entailment.

Coverage (per Block 8.8A-GATE-SCHEMA requirements):

  1. coverage_gap_statements accepts kind='entailment_block' with
     severity='block'.
  2. coverage_gap_statements accepts kind='entailment_warning' with
     severity='warn'.
  3. coverage_gap_statements still accepts all six legacy kinds:
       - 'unverified_claim'         (from 0005)
       - 'missing_evidence'         (from 0005)
       - 'out_of_scope'             (from 0005)
       - 'source_loss'              (from 0005)
       - 'source_quality_block'     (from 0008)
       - 'source_quality_warning'   (from 0008)
  4. coverage_gap_statements rejects unknown / typo-ed kinds.
  5. UNIQUE (draft_final_answer_id, kind, gap_key) keeps working:
       - exact duplicate on (draft, 'entailment_block', gap_key) -> rejected;
       - same draft + same gap_key with different kinds
         (entailment_block / entailment_warning /
          source_quality_block / source_quality_warning) -> all accepted.
  6. severity='block' works with kind='entailment_block'.
  7. severity='warn' works with kind='entailment_warning'.
     (Severity codomain is invariant from 0005: {info, warn, block};
      invalid severity is still rejected.)
  8. The new CHECK on coverage_gap_statements.kind is explicitly named
     'coverage_gap_statements_kind_check' (same name 0008 used; 0010
     drops and re-creates it under that same explicit name).
  9. The migration does NOT mutate unrelated schema:
       - claim_entailment_checks still exists (3 CHECK constraints from 0009);
       - source_quality_assessments still exists (12 CHECK constraints from 0007);
       - final_gate_reports still exists (1 CHECK constraint from 0005);
       - no new trigger has been added to coverage_gap_statements.
 10. No regression on 0008: 'source_quality_block' and
     'source_quality_warning' continue to be accepted.
 11. details JSONB roundtrip: inserting an 'entailment_block' row with
     a structured details payload (e.g. policy identity) preserves the
     content on read.

Conventions (per tests/README.md):
  - Local helpers only; no imports from other test files.
  - No ORM, no subprocess.
  - psycopg connection from the shared db_conn fixture.
  - Migrations are guaranteed to be applied via the _ensure_migrations
    helper that reloads scripts/migrate.py as a module (identical
    pattern used by test_migration_0008_coverage_gap_source_quality.py
    and test_migration_0009_claim_entailment_checks.py).
  - Rerun-safe: every invocation uses fresh UUIDs / hashes.
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# migration bootstrap
# ---------------------------------------------------------------------------
def _ensure_migrations(db_conn) -> None:
    """Apply all pending migrations idempotently.

    Mirrors the helper used in tests/test_migration_0008_*.py and
    tests/test_migration_0009_*.py so that a fresh dev DB is brought up
    to the latest schema (including 0010) without requiring the operator
    to run `make migrate` manually.
    """
    spec = importlib.util.spec_from_file_location(
        "migrate_module_0010", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    rc = module.cmd_apply(db_conn, target=None, dry_run=False)
    assert rc == 0


# ---------------------------------------------------------------------------
# rerun-safe helpers
# ---------------------------------------------------------------------------
def _unique_hash() -> str:
    """Return a 64-hex string unique to this invocation."""
    return uuid.uuid4().hex + uuid.uuid4().hex


# Whitelist of tables we are allowed to introspect via _count_check_constraints.
# Hard-coded to avoid any risk of SQL injection through f-string interpolation.
# Same pattern as test_migration_0008_*.py.
_ALLOWED_TABLES_FOR_INTROSPECTION: frozenset[str] = frozenset({
    "coverage_gap_statements",
    "claim_entailment_checks",
    "source_quality_assessments",
    "final_gate_reports",
})


def _count_check_constraints(cur, *, table: str) -> int:
    """Return the number of CHECK constraints on `table`.

    Used to assert that migration 0010 does not change the constraint
    topology of tables it must not touch (claim_entailment_checks,
    source_quality_assessments, final_gate_reports).
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
# seed helpers — fully local, no cross-test imports
# ---------------------------------------------------------------------------
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

    project_name = f"mig-0010-test-{uuid.uuid4()}"
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
    """Insert one draft_final_answers row for the given task.

    Honors the UNIQUE (task_id, version_no) from 0005. Each task only
    gets one draft per test invocation; version_no defaults to 1.
    """
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
    details: dict | None = None,
) -> uuid.UUID:
    """Insert one coverage_gap_statements row.

    ``details`` defaults to '{}'::jsonb when not provided. When provided
    it is JSON-serialized and cast to jsonb in the INSERT statement.
    """
    if details is None:
        cur.execute(
            """
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key, details)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, '{}'::jsonb)
            RETURNING id
            """,
            (draft_id, kind, severity, gap_key),
        )
    else:
        cur.execute(
            """
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key, details)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (draft_id, kind, severity, gap_key, json.dumps(details)),
        )
    return uuid.UUID(str(cur.fetchone()[0]))


# ===========================================================================
# 1) entailment_block is accepted with severity='block'.
# ===========================================================================
def test_kind_entailment_block_is_accepted_with_severity_block(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="entailment_block",
        gap_key=f"span:{uuid.uuid4()}:entailment_block",
        severity="block",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind, severity FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    row = cur.fetchone()
    assert row is not None
    assert (str(row[0]), str(row[1])) == ("entailment_block", "block")


# ===========================================================================
# 2) entailment_warning is accepted with severity='warn'.
# ===========================================================================
def test_kind_entailment_warning_is_accepted_with_severity_warn(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="entailment_warning",
        gap_key=f"span:{uuid.uuid4()}:entailment_warning",
        severity="warn",
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind, severity FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    row = cur.fetchone()
    assert row is not None
    assert (str(row[0]), str(row[1])) == ("entailment_warning", "warn")


# ===========================================================================
# 3) Legacy kinds (from 0005 + 0008) still accepted.
# ===========================================================================
@pytest.mark.parametrize(
    "legacy_kind",
    [
        "unverified_claim",
        "missing_evidence",
        "out_of_scope",
        "source_loss",
        "source_quality_block",
        "source_quality_warning",
    ],
)
def test_legacy_kinds_still_accepted(db_conn, legacy_kind):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    severity = "warn" if legacy_kind.endswith("_warning") else "block"
    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind=legacy_kind,
        gap_key=f"legacy:{legacy_kind}:{uuid.uuid4()}",
        severity=severity,
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    assert str(cur.fetchone()[0]) == legacy_kind


# ===========================================================================
# 4) Unknown kinds rejected — including near-misses around entailment.
# ===========================================================================
@pytest.mark.parametrize(
    "bad_kind",
    [
        "entailment",                # missing suffix
        "entailment_warn",           # typo (should be entailment_warning)
        "entailment_blocked",        # typo (should be entailment_block)
        "entailment_warning_extra",  # trailing garbage
        "",                          # empty
        "bogus",                     # entirely unrelated
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


# ===========================================================================
# 5a) UNIQUE composite rejects exact duplicate on entailment_block.
# ===========================================================================
def test_unique_kind_gap_key_rejects_exact_duplicate_for_entailment_block(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    gap_key = "same-key"

    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="entailment_block",
        gap_key=gap_key,
        severity="block",
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_coverage_gap(
            cur,
            draft_id=draft_id,
            kind="entailment_block",
            gap_key=gap_key,
            severity="block",
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 5b) UNIQUE composite allows same gap_key with DIFFERENT kinds.
# ===========================================================================
def test_unique_kind_gap_key_allows_same_gap_key_with_different_kinds(db_conn):
    """Same draft + same gap_key but different kinds must coexist.

    This is the explicit invariant the Final Answer Gate will rely on in
    8.8A-GATE-CODE: when the same span has both an entailment and a
    source-quality observation, both gaps are emitted in parallel, each
    keyed by its own kind. Per PHASE_8_8A_GATE_PRE.md §9.4 the gap_key
    does NOT include the reason; the UNIQUE composite includes kind, so
    collision is per-kind.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    shared_gap_key = "same-key"

    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="entailment_block",
        gap_key=shared_gap_key,
        severity="block",
    )
    _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="entailment_warning",
        gap_key=shared_gap_key,
        severity="warn",
    )
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
    assert int(cur.fetchone()[0]) == 4


# ===========================================================================
# 6 + 7) severity behaviour unchanged.
#
# 6) severity='block' accepted with kind='entailment_block'  ->  already
#    covered by test_kind_entailment_block_is_accepted_with_severity_block.
# 7) severity='warn' accepted with kind='entailment_warning' ->  already
#    covered by test_kind_entailment_warning_is_accepted_with_severity_warn.
#
# Below: severity codomain remains invariant from 0005. An invalid
# severity value must still be rejected by the original severity CHECK,
# regardless of the chosen kind.
# ===========================================================================
def test_invalid_severity_still_rejected_with_new_kinds(db_conn):
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
            kind="entailment_block",
            gap_key=f"span:{uuid.uuid4()}",
            severity="bogus_severity",
        )
        db_conn.commit()
    db_conn.rollback()


# ===========================================================================
# 8) The new CHECK on coverage_gap_statements.kind is explicitly named.
# ===========================================================================
def test_new_kind_check_constraint_is_explicitly_named(db_conn):
    """0010 drops the previous CHECK (re-created by 0008 under the name
    'coverage_gap_statements_kind_check') and re-creates it under the
    SAME explicit name, preserving 0008's discipline of keeping the
    constraint nameable for future migrations.
    """
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
        "'coverage_gap_statements_kind_check' after 0010. "
        "If the migration runner did not apply 0010, run `make migrate`."
    )


# ---------------------------------------------------------------------------
# 9) The migration does NOT mutate unrelated schema.
# ---------------------------------------------------------------------------
def test_migration_does_not_touch_claim_entailment_checks(db_conn):
    """claim_entailment_checks must still have exactly the CHECK
    constraints declared by 0009: cec_verdict_chk, cec_version_no_chk,
    cec_confidence_range. We assert the EXACT expected count plus the
    name of one of them as a representative subset.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    # 0009 declares 3 CHECK constraints on claim_entailment_checks.
    n = _count_check_constraints(cur, table="claim_entailment_checks")
    assert n == 3, (
        f"claim_entailment_checks has {n} CHECK constraints; expected 3. "
        "Migration 0010 must not modify this table."
    )

    expected_subset = {
        "cec_verdict_chk",
        "cec_version_no_chk",
        "cec_confidence_range",
    }
    cur.execute(
        """
        SELECT conname
          FROM pg_constraint
         WHERE conrelid = 'claim_entailment_checks'::regclass
           AND contype  = 'c'
        """,
    )
    names = {str(r[0]) for r in cur.fetchall()}
    missing = expected_subset - names
    assert not missing, (
        f"missing expected CHECKs on claim_entailment_checks: {missing}"
    )


def test_migration_does_not_touch_source_quality_assessments(db_conn):
    """source_quality_assessments must still have exactly the CHECK
    constraints declared by 0007 (12 total): sqa_target_xor,
    sqa_version_no_chk, sqa_confidence_range, plus the nine codomain
    CHECKs.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    n = _count_check_constraints(cur, table="source_quality_assessments")
    assert n == 12, (
        f"source_quality_assessments has {n} CHECK constraints; expected 12. "
        "Migration 0010 must not modify this table."
    )


def test_migration_does_not_touch_final_gate_reports(db_conn):
    """final_gate_reports must still have exactly its CHECK constraint
    on `decision` (from 0005).
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    n = _count_check_constraints(cur, table="final_gate_reports")
    assert n == 1, (
        f"final_gate_reports has {n} CHECK constraints; expected 1. "
        "Migration 0010 must not modify this table."
    )


def test_migration_does_not_create_new_trigger_on_coverage_gap_statements(db_conn):
    """0010 must NOT introduce any trigger on coverage_gap_statements.

    Per PHASE_8_8A_GATE_PRE.md §13.2 and the explicit constraint list in
    the prompt: 'no trigger' for this block. The table remained
    operationally insert-only (without DB-level append-only enforcement)
    after 0008; 0010 must preserve that property.

    We assert that no non-internal user trigger exists on the table.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        """
        SELECT t.tgname
          FROM pg_trigger t
         WHERE t.tgrelid = 'coverage_gap_statements'::regclass
           AND NOT t.tgisinternal
        """
    )
    triggers = [str(r[0]) for r in cur.fetchall()]
    assert triggers == [], (
        f"unexpected user trigger(s) on coverage_gap_statements: {triggers!r}. "
        "Migration 0010 must not introduce triggers."
    )


# ---------------------------------------------------------------------------
# 10) No regression on 0008.
# ---------------------------------------------------------------------------
def test_no_regression_on_0008_source_quality_block(db_conn):
    """After 0010, the 8.7G kinds source_quality_block and
    source_quality_warning continue to be accepted with a structured
    details JSONB payload (the shape used by the Source Quality Gate).
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    sq_details = {
        "policy": {
            "name": "mvp0_source_quality_gate_policy",
            "version": "0.1.0",
        },
        "reasons": [{"reason_code": "source_quality_unsuitable"}],
    }
    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="source_quality_block",
        gap_key=f"span:{uuid.uuid4()}:source_quality_block",
        severity="block",
        details=sq_details,
    )
    db_conn.commit()

    cur.execute(
        "SELECT kind, severity, details FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    kind, severity, details = cur.fetchone()
    if isinstance(details, str):
        details = json.loads(details)
    assert str(kind) == "source_quality_block"
    assert str(severity) == "block"
    assert details["policy"]["name"] == "mvp0_source_quality_gate_policy"
    assert details["reasons"][0]["reason_code"] == "source_quality_unsuitable"


# ---------------------------------------------------------------------------
# 11) details JSONB roundtrip on an entailment_block row.
# ---------------------------------------------------------------------------
def test_entailment_block_details_jsonb_roundtrip(db_conn):
    """A structured details payload (with the policy identity used by
    PHASE_8_8A_GATE_PRE.md §6 / §13.1) survives the INSERT and is
    readable verbatim.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()
    draft_id = _insert_draft(cur, task_id=task_id, version_no=1)
    db_conn.commit()

    entailment_details = {
        "policy": {
            "name": "mvp0_entailment_gate_policy",
            "version": "0.1.0",
        },
        "reasons": [
            {
                "reason_code": "entailment_contradicted",
                "evidence_span_id": str(uuid.uuid4()),
                "assessment_id": str(uuid.uuid4()),
                "verdict": "contradicted",
                "confidence": 0.6,
            }
        ],
    }

    gap_id = _insert_coverage_gap(
        cur,
        draft_id=draft_id,
        kind="entailment_block",
        gap_key=f"span:{uuid.uuid4()}:entailment_block",
        severity="block",
        details=entailment_details,
    )
    db_conn.commit()

    cur.execute(
        "SELECT details FROM coverage_gap_statements WHERE id = %s",
        (gap_id,),
    )
    details = cur.fetchone()[0]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["policy"]["name"] == "mvp0_entailment_gate_policy"
    assert details["policy"]["version"] == "0.1.0"
    assert details["reasons"][0]["reason_code"] == "entailment_contradicted"
    assert details["reasons"][0]["verdict"] == "contradicted"
    assert details["reasons"][0]["confidence"] == pytest.approx(0.6)
