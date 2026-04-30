"""Constraint-level tests on Claim Ledger schema (Phase 8.3, root, DB-only).

Coverage:
  - claim_ledger_entries APPEND-ONLY (UPDATE / DELETE rejected by trigger).
  - UNIQUE (claim_logical_id, version_no) on claim_ledger_entries.
  - claim_lineage CHECK: parent_entry_id != child_entry_id.
  - verification_records UNIQUE (claim_ledger_entry_id, check_kind, check_name).
  - claim_evidence_links CHECK cel_origin_xor (in MVP-0 only evidence_span_id).

Rerun-safety:
  All identifiers are unique per invocation (uuid.uuid4()-derived). The dev tenant
  and the dev user are upserted; the project and task are fresh per test.
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import psycopg
from sqlalchemy import text  # noqa: F401  (kept for parity with sibling tests)

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
    """Return a 64-hex string unique to this invocation."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_dev(cur) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per test invocation.

    Returns (tenant_id, project_id, task_id).
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

    project_name = f"ledger-test-{uuid.uuid4()}"
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
    return tenant_id, project_id, task_id


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
    cur, *, claim_logical_id: uuid.UUID, version_no: int, state: str
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


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_claim_ledger_entries_reject_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE claim_ledger_entries SET state = 'verified_fact' WHERE id = %s",
            (v1,),
        )
        db_conn.commit()
    db_conn.rollback()


def test_claim_ledger_entries_reject_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    db_conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM claim_ledger_entries WHERE id = %s", (v1,))
        db_conn.commit()
    db_conn.rollback()


def test_claim_ledger_entries_unique_logical_version(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
        db_conn.commit()
    db_conn.rollback()


def test_claim_ledger_v1_then_v2_distinct_versions_ok(db_conn):
    """Two distinct versions for the same logical claim are allowed."""
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    v2 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=2, state="verified_fact")
    db_conn.commit()
    assert v1 != v2


def test_claim_lineage_no_self_reference(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    db_conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO claim_lineage
                (id, parent_entry_id, child_entry_id, relation_kind)
            VALUES (gen_random_uuid(), %s, %s, 'supersedes')
            """,
            (v1, v1),
        )
        db_conn.commit()
    db_conn.rollback()


def test_claim_lineage_supersedes_v1_v2_ok(db_conn):
    """Legitimate supersedes lineage v1 -> v2 inserts cleanly; duplicate rejected."""
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    v2 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=2, state="verified_fact")
    cur.execute(
        """
        INSERT INTO claim_lineage
            (id, parent_entry_id, child_entry_id, relation_kind)
        VALUES (gen_random_uuid(), %s, %s, 'supersedes')
        """,
        (v1, v2),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO claim_lineage
                (id, parent_entry_id, child_entry_id, relation_kind)
            VALUES (gen_random_uuid(), %s, %s, 'supersedes')
            """,
            (v1, v2),
        )
        db_conn.commit()
    db_conn.rollback()


def test_verification_records_unique_per_check(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO verification_records
            (id, claim_logical_id, claim_ledger_entry_id, check_kind, check_name,
             outcome, evaluator_id)
        VALUES (gen_random_uuid(), %s, %s, 'cve_lite',
                'quote_hash_and_substring_v1', 'pass', 'mvp0_cve_lite_v1')
        """,
        (lc, v1),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO verification_records
                (id, claim_logical_id, claim_ledger_entry_id, check_kind, check_name,
                 outcome, evaluator_id)
            VALUES (gen_random_uuid(), %s, %s, 'cve_lite',
                    'quote_hash_and_substring_v1', 'fail', 'mvp0_cve_lite_v1')
            """,
            (lc, v1),
        )
        db_conn.commit()
    db_conn.rollback()


def test_claim_evidence_links_origin_xor_check(db_conn):
    """In MVP-0 cel_origin_xor requires evidence_span_id NOT NULL and
    retrieved_source_span_id IS NULL.

    We exercise the CHECK by attempting to insert a row with retrieved_source_span_id
    set: this MUST fail with a CheckViolation BEFORE the FK on evidence_span_id is
    even checked, because both columns are present in the row and the CHECK forbids
    that combination.
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, task_id = _seed_dev(cur)
    db_conn.commit()
    lc = _make_logical_claim(cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id)
    v1 = _insert_ledger_v(cur, claim_logical_id=lc, version_no=1, state="candidate")
    db_conn.commit()

    bogus_span_a = uuid.uuid4()
    bogus_span_b = uuid.uuid4()
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO claim_evidence_links
                (id, claim_logical_id, claim_ledger_entry_id,
                 evidence_span_id, retrieved_source_span_id, link_role)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, 'primary_support')
            """,
            (lc, v1, bogus_span_a, bogus_span_b),
        )
        db_conn.commit()
    db_conn.rollback()