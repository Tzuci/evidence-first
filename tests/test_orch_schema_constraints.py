"""Constraint-level tests on the Orchestration schema (Phase ORCH-SCHEMA-A,
root, DB-only).

These are real DB schema tests. They:
  - use the `db_conn` fixture from tests/conftest.py;
  - apply migrations through the project migration runner (scripts/migrate.py)
    via the `_ensure_migrations` helper, on the same model as the sibling root
    tests test_answers_gate_constraints.py and test_claim_ledger_constraints.py;
  - use psycopg directly, with no import from apps/api or apps/worker and no
    application service started;
  - are rerun-safe: every identifier / hash is unique per invocation.

Coverage (migration 0011_orchestration_schema.sql):
  - presence of the 19 new tables;
  - master_prompt_versions UNIQUE (master_prompt_id, version_no) and
    APPEND-ONLY (UPDATE / DELETE rejected by trigger);
  - orchestration_runs UNIQUE (tenant_id, idempotency_key) and CHECK on
    mode / status / execution_mode;
  - orchestration_events UNIQUE (orchestration_run_id, sequence_no) and
    APPEND-ONLY;
  - orchestration_agent_runs exists and does NOT reuse the 0005 placeholder
    agent_runs: agent_runs keeps its CHECK run_kind IN
    ('compile_draft','final_answer_gate');
  - source_candidates has NO column evidence_span_id, has CHECK on
    candidate_type / status, and the path toward evidence runs through
    source_verifications;
  - source_verifications can link a real evidence_span (FK exercised);
  - candidate_syntheses UNIQUE (orchestration_run_id, version_no) for
    versioning AND UNIQUE (orchestration_run_id, idempotency_key) for
    idempotency, CHECK on status, and append-only links;
  - provider_invocations CHECK on status, presence of is_mock, APPEND-ONLY,
    and absence of any api_key / secret / credential column;
  - token_usage_records FK toward run / agent run / provider invocation,
    idempotency via the two partial UNIQUE indexes (provider_invocation_id
    NOT NULL vs NULL), and APPEND-ONLY;
  - token_budgets has NO column orchestration_run_id, has CHECK on
    budget_level / overflow_policy, and the per_agent target CHECK.

Rerun-safety:
  All identifiers/hashes are unique per invocation. The dev tenant and the dev
  user are upserted; project and task are fresh per test.
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

    project_name = f"orch-schema-test-{uuid.uuid4()}"
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


# ---------------------------------------------------------------------------
# local insert helpers for the 0011 tables
# ---------------------------------------------------------------------------
def _insert_master_prompt(
    cur, *, tenant_id: uuid.UUID, project_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO master_prompts
            (id, tenant_id, project_id, created_by, prompt_text, title, status)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 'draft')
        RETURNING id
        """,
        (tenant_id, project_id, user_id, f"prompt-{uuid.uuid4()}", "t"),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_master_prompt_version(
    cur, *, master_prompt_id: uuid.UUID, version_no: int = 1
) -> tuple[uuid.UUID, str]:
    text_hash = _unique_hash()
    cur.execute(
        """
        INSERT INTO master_prompt_versions
            (id, master_prompt_id, version_no, prompt_text, prompt_text_hash)
        VALUES (gen_random_uuid(), %s, %s, %s, %s)
        RETURNING id
        """,
        (master_prompt_id, version_no, f"frozen-{uuid.uuid4()}", text_hash),
    )
    return uuid.UUID(str(cur.fetchone()[0])), text_hash


def _insert_role_prompt(cur, *, tenant_id: uuid.UUID, version_no: int = 1) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO agent_role_prompts
            (id, tenant_id, name, role_category,
             system_prompt_text, task_prompt_text, version_no)
        VALUES (gen_random_uuid(), %s, %s, 'researcher', 'sys', 'task', %s)
        RETURNING id
        """,
        (tenant_id, f"role-{uuid.uuid4()}", version_no),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_agent_config(
    cur,
    *,
    tenant_id: uuid.UUID,
    master_prompt_id: uuid.UUID,
    role_prompt_id: uuid.UUID,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO agent_configs
            (id, tenant_id, master_prompt_id, agent_role_prompt_id,
             name, provider, model)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, 'mock', 'mock-model')
        RETURNING id
        """,
        (tenant_id, master_prompt_id, role_prompt_id, f"agent-{uuid.uuid4()}"),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_run(
    cur,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    master_prompt_version_id: uuid.UUID,
    mode: str = "multi_ai_orchestration",
    execution_mode: str = "independent",
    status: str = "pending",
    idempotency_key: str | None = None,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO orchestration_runs
            (id, tenant_id, project_id, master_prompt_version_id,
             mode, execution_mode, status, master_prompt_text_hash,
             idempotency_key, policy_name, policy_version)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s,
                'mvp0_orch_policy', '0.1.0')
        RETURNING id
        """,
        (
            tenant_id,
            project_id,
            master_prompt_version_id,
            mode,
            execution_mode,
            status,
            _unique_hash(),
            idempotency_key or f"idem-{uuid.uuid4()}",
        ),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_config_snapshot(
    cur, *, run_id: uuid.UUID, agent_config_id: uuid.UUID
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO agent_config_snapshots
            (id, orchestration_run_id, agent_config_id,
             snapshot_payload, agent_role_prompt_text_hash)
        VALUES (gen_random_uuid(), %s, %s, '{}'::jsonb, %s)
        RETURNING id
        """,
        (run_id, agent_config_id, _unique_hash()),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_agent_run(
    cur,
    *,
    run_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    status: str = "pending",
    attempt_no: int = 1,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO orchestration_agent_runs
            (id, orchestration_run_id, agent_config_snapshot_id,
             status, attempt_no)
        VALUES (gen_random_uuid(), %s, %s, %s, %s)
        RETURNING id
        """,
        (run_id, snapshot_id, status, attempt_no),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_agent_output(cur, *, agent_run_id: uuid.UUID, sequence_no: int = 0) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO orchestration_agent_outputs
            (id, agent_run_id, output_kind, content_text, sequence_no)
        VALUES (gen_random_uuid(), %s, 'free_text', %s, %s)
        RETURNING id
        """,
        (agent_run_id, f"out-{uuid.uuid4()}", sequence_no),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_event(
    cur,
    *,
    run_id: uuid.UUID,
    event_type: str = "run_created",
    sequence_no: int = 0,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO orchestration_events
            (id, orchestration_run_id, event_type, sequence_no, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, %s)
        RETURNING id
        """,
        (run_id, event_type, sequence_no, idempotency_key or f"ev-{uuid.uuid4()}"),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_source_candidate(
    cur,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    candidate_type: str = "agent_cited",
    status: str = "proposed",
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO source_candidates
            (id, tenant_id, orchestration_run_id, candidate_type, status)
        VALUES (gen_random_uuid(), %s, %s, %s, %s)
        RETURNING id
        """,
        (tenant_id, run_id, candidate_type, status),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_source_resolution(
    cur, *, candidate_id: uuid.UUID, outcome: str = "resolved"
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO source_resolutions
            (id, source_candidate_id, resolution_target_kind, outcome,
             idempotency_key)
        VALUES (gen_random_uuid(), %s, 'uploaded_document', %s, %s)
        RETURNING id
        """,
        (candidate_id, outcome, f"res-{uuid.uuid4()}"),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _insert_candidate_synthesis(
    cur,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    version_no: int = 1,
    status: str = "draft",
    idempotency_key: str | None = None,
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO candidate_syntheses
            (id, tenant_id, orchestration_run_id, version_no,
             synthesis_text, synthesis_text_hash, status, is_mock,
             idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, TRUE, %s)
        RETURNING id
        """,
        (
            tenant_id,
            run_id,
            version_no,
            f"synthesis-{uuid.uuid4()}",
            _unique_hash(),
            status,
            idempotency_key or f"syn-{uuid.uuid4()}",
        ),
    )
    return uuid.UUID(str(cur.fetchone()[0]))


def _make_real_evidence_span(
    cur, *, tenant_id: uuid.UUID, project_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a real document -> version -> chunk -> evidence_span chain,
    reusing the existing 0002/0003 tables. Returns
    (evidence_span_id, document_version_id, document_chunk_id).
    """
    # storage blob + object (local_fs backend, per 0002 blob_location_present).
    cur.execute(
        """
        INSERT INTO storage_blobs
            (id, content_hash, hash_algorithm, size_bytes, mime_type,
             storage_backend, local_path)
        VALUES (gen_random_uuid(), %s, 'sha256', 4, 'text/plain',
                'local_fs', %s)
        RETURNING id
        """,
        (_unique_hash(), f"/tmp/{uuid.uuid4()}.txt"),
    )
    blob_id = uuid.UUID(str(cur.fetchone()[0]))
    cur.execute(
        """
        INSERT INTO storage_objects
            (id, tenant_id, project_id, blob_id, object_type,
             logical_owner_kind, logical_owner_id)
        VALUES (gen_random_uuid(), %s, %s, %s, 'document', 'document',
                gen_random_uuid())
        RETURNING id
        """,
        (tenant_id, project_id, blob_id),
    )
    storage_object_id = uuid.UUID(str(cur.fetchone()[0]))
    cur.execute(
        """
        INSERT INTO uploaded_documents
            (id, tenant_id, project_id, storage_object_id, filename,
             content_hash, mime_type, size_bytes, tier, created_by)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 'text/plain', 4,
                'user_provided', %s)
        RETURNING id
        """,
        (tenant_id, project_id, storage_object_id, f"{uuid.uuid4()}.txt",
         _unique_hash(), user_id),
    )
    document_id = uuid.UUID(str(cur.fetchone()[0]))
    cur.execute(
        """
        INSERT INTO document_versions
            (id, document_id, version_no, version_kind, inline_text, text_hash)
        VALUES (gen_random_uuid(), %s, 1, 'parsed', 'text', %s)
        RETURNING id
        """,
        (document_id, _unique_hash()),
    )
    document_version_id = uuid.UUID(str(cur.fetchone()[0]))
    cur.execute(
        """
        INSERT INTO document_chunks
            (id, document_version_id, chunk_index, char_start, char_end,
             inline_text, text_hash)
        VALUES (gen_random_uuid(), %s, 0, 0, 4, 'text', %s)
        RETURNING id
        """,
        (document_version_id, _unique_hash()),
    )
    document_chunk_id = uuid.UUID(str(cur.fetchone()[0]))
    cur.execute(
        """
        INSERT INTO evidence_spans
            (id, document_chunk_id, char_start, char_end, quote, quote_hash)
        VALUES (gen_random_uuid(), %s, 0, 4, 'text', %s)
        RETURNING id
        """,
        (document_chunk_id, _unique_hash()),
    )
    evidence_span_id = uuid.UUID(str(cur.fetchone()[0]))
    return evidence_span_id, document_version_id, document_chunk_id


# ---------------------------------------------------------------------------
# 1. tables present
# ---------------------------------------------------------------------------
def test_orch_schema_tables_exist(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    expected = [
        "master_prompts",
        "agent_role_prompts",
        "agent_configs",
        "token_budgets",
        "master_prompt_versions",
        "orchestration_runs",
        "agent_config_snapshots",
        "orchestration_events",
        "orchestration_agent_runs",
        "orchestration_agent_messages",
        "orchestration_agent_outputs",
        "source_candidates",
        "source_resolutions",
        "source_verifications",
        "provider_invocations",
        "token_usage_records",
        "candidate_syntheses",
        "synthesis_source_links",
        "synthesis_claim_links",
    ]
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        """,
        (expected,),
    )
    found = {str(r[0]) for r in cur.fetchall()}
    missing = sorted(set(expected) - found)
    assert not missing, f"missing 0011 tables: {missing}"
    assert len(expected) == 19


# ---------------------------------------------------------------------------
# 2. master_prompt_versions: UNIQUE + append-only
# ---------------------------------------------------------------------------
def test_master_prompt_versions_are_unique_and_append_only(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp, version_no=1)
    db_conn.commit()

    # UNIQUE (master_prompt_id, version_no): second v1 rejected.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_master_prompt_version(cur, master_prompt_id=mp, version_no=1)
        db_conn.commit()
    db_conn.rollback()

    # A distinct version_no is accepted.
    _insert_master_prompt_version(cur, master_prompt_id=mp, version_no=2)
    db_conn.commit()

    # APPEND-ONLY: UPDATE rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE master_prompt_versions SET prompt_text = 'mutated' WHERE id = %s",
            (mpv,),
        )
        db_conn.commit()
    db_conn.rollback()

    # APPEND-ONLY: DELETE rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM master_prompt_versions WHERE id = %s", (mpv,))
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# 3. orchestration_runs: idempotency UNIQUE + mode/status/execution_mode CHECK
# ---------------------------------------------------------------------------
def test_orchestration_run_idempotency_and_mode_check(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    db_conn.commit()

    shared_key = f"idem-{uuid.uuid4()}"
    _insert_run(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        master_prompt_version_id=mpv,
        idempotency_key=shared_key,
    )
    db_conn.commit()

    # UNIQUE (tenant_id, idempotency_key): second run, same key, rejected.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_run(
            cur,
            tenant_id=tenant_id,
            project_id=project_id,
            master_prompt_version_id=mpv,
            idempotency_key=shared_key,
        )
        db_conn.commit()
    db_conn.rollback()

    # CHECK on mode.
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(
            cur,
            tenant_id=tenant_id,
            project_id=project_id,
            master_prompt_version_id=mpv,
            mode="not_a_mode",
        )
        db_conn.commit()
    db_conn.rollback()

    # CHECK on execution_mode.
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(
            cur,
            tenant_id=tenant_id,
            project_id=project_id,
            master_prompt_version_id=mpv,
            execution_mode="not_an_execution_mode",
        )
        db_conn.commit()
    db_conn.rollback()

    # CHECK on status.
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_run(
            cur,
            tenant_id=tenant_id,
            project_id=project_id,
            master_prompt_version_id=mpv,
            status="not_a_status",
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# 4. orchestration_events: sequence UNIQUE + append-only
# ---------------------------------------------------------------------------
def test_orchestration_events_are_append_only_and_sequence_unique(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    run = _insert_run(
        cur, tenant_id=tenant_id, project_id=project_id, master_prompt_version_id=mpv
    )
    ev = _insert_event(cur, run_id=run, event_type="run_created", sequence_no=0)
    db_conn.commit()

    # UNIQUE (orchestration_run_id, sequence_no): second seq 0 rejected.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_event(cur, run_id=run, event_type="agent_run_started", sequence_no=0)
        db_conn.commit()
    db_conn.rollback()

    # A distinct sequence_no is accepted.
    _insert_event(cur, run_id=run, event_type="agent_run_started", sequence_no=1)
    db_conn.commit()

    # APPEND-ONLY: UPDATE rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE orchestration_events SET event_type = 'run_failed' WHERE id = %s",
            (ev,),
        )
        db_conn.commit()
    db_conn.rollback()

    # APPEND-ONLY: DELETE rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM orchestration_events WHERE id = %s", (ev,))
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# 5. orchestration_agent_runs does NOT reuse the 0005 placeholder agent_runs
# ---------------------------------------------------------------------------
def test_orchestration_agent_tables_do_not_reuse_0005_agent_runs(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    # orchestration_agent_runs exists.
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='orchestration_agent_runs'"
    )
    assert cur.fetchone() is not None

    # The 0005 placeholder agent_runs still exists as a distinct table.
    cur.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='agent_runs'"
    )
    assert cur.fetchone() is not None

    # agent_runs (0005) keeps its compiler/gate CHECK on run_kind untouched:
    # 0011 must NOT have turned it into a multi-AI orchestration table.
    cur.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_attribute a
          ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid = 'agent_runs'::regclass
          AND c.contype  = 'c'
          AND a.attname  = 'run_kind'
        """
    )
    row = cur.fetchone()
    assert row is not None, "agent_runs.run_kind CHECK is missing"
    cdef = str(row[0])
    assert "compile_draft" in cdef
    assert "final_answer_gate" in cdef

    # agent_runs (0005) is keyed on task_id, NOT on an orchestration_run_id:
    # confirms its semantics were not migrated.
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='agent_runs'
          AND column_name='orchestration_run_id'
        """
    )
    assert cur.fetchone() is None


# ---------------------------------------------------------------------------
# 6. source_candidates is not evidence
# ---------------------------------------------------------------------------
def test_source_candidate_is_not_evidence(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    # source_candidates has NO evidence_span_id column.
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='source_candidates'
          AND column_name='evidence_span_id'
        """
    )
    assert cur.fetchone() is None

    # source_candidates carries NO FK toward evidence_spans /
    # claim_evidence_links / logical_claims.
    cur.execute(
        """
        SELECT confrelid::regclass::text
        FROM pg_constraint
        WHERE conrelid = 'source_candidates'::regclass
          AND contype  = 'f'
        """
    )
    fk_targets = {str(r[0]) for r in cur.fetchall()}
    assert "evidence_spans" not in fk_targets
    assert "claim_evidence_links" not in fk_targets
    assert "logical_claims" not in fk_targets

    # CHECK on candidate_type / status.
    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    run = _insert_run(
        cur, tenant_id=tenant_id, project_id=project_id, master_prompt_version_id=mpv
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_source_candidate(
            cur, tenant_id=tenant_id, run_id=run, candidate_type="not_a_type"
        )
        db_conn.commit()
    db_conn.rollback()

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_source_candidate(
            cur, tenant_id=tenant_id, run_id=run, status="not_a_status"
        )
        db_conn.commit()
    db_conn.rollback()

    # The bridge toward evidence runs through source_verifications: that table
    # is the one carrying the evidence_span_id FK.
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='source_verifications'
          AND column_name='evidence_span_id'
        """
    )
    assert cur.fetchone() is not None


# ---------------------------------------------------------------------------
# 7. source_verifications can link a real evidence_span
# ---------------------------------------------------------------------------
def test_source_verification_can_link_to_evidence_span(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    evidence_span_id, document_version_id, document_chunk_id = _make_real_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    run = _insert_run(
        cur, tenant_id=tenant_id, project_id=project_id, master_prompt_version_id=mpv
    )
    candidate = _insert_source_candidate(cur, tenant_id=tenant_id, run_id=run)
    resolution = _insert_source_resolution(cur, candidate_id=candidate)
    db_conn.commit()

    # Insert a verification linking the real evidence_span.
    cur.execute(
        """
        INSERT INTO source_verifications
            (id, source_candidate_id, source_resolution_id,
             evidence_span_id, document_version_id, document_chunk_id,
             outcome, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s,
                'verified_as_retrieved', %s)
        RETURNING id, evidence_span_id
        """,
        (
            candidate,
            resolution,
            evidence_span_id,
            document_version_id,
            document_chunk_id,
            f"ver-{uuid.uuid4()}",
        ),
    )
    row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    assert uuid.UUID(str(row[1])) == evidence_span_id

    # The FK toward evidence_spans is real: a bogus span id is rejected.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        cur.execute(
            """
            INSERT INTO source_verifications
                (id, source_candidate_id, source_resolution_id,
                 evidence_span_id, outcome, idempotency_key)
            VALUES (gen_random_uuid(), %s, %s, %s,
                    'inconclusive', %s)
            """,
            (candidate, resolution, uuid.uuid4(), f"ver-{uuid.uuid4()}"),
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# 8. candidate_syntheses: versioning + status CHECK + append-only links
# ---------------------------------------------------------------------------
def test_candidate_synthesis_versioning_status_and_links(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    run = _insert_run(
        cur, tenant_id=tenant_id, project_id=project_id, master_prompt_version_id=mpv
    )
    db_conn.commit()

    # Versioning: v1 then v2 ok.
    syn1 = _insert_candidate_synthesis(
        cur, tenant_id=tenant_id, run_id=run, version_no=1
    )
    _insert_candidate_synthesis(cur, tenant_id=tenant_id, run_id=run, version_no=2)
    db_conn.commit()

    # UNIQUE (orchestration_run_id, version_no): second v1 rejected.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_candidate_synthesis(
            cur, tenant_id=tenant_id, run_id=run, version_no=1
        )
        db_conn.commit()
    db_conn.rollback()

    # CHECK on status.
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_candidate_synthesis(
            cur, tenant_id=tenant_id, run_id=run, version_no=3, status="not_a_status"
        )
        db_conn.commit()
    db_conn.rollback()

    # UNIQUE (orchestration_run_id, idempotency_key): distinct from the
    # versioning UNIQUE. Two syntheses on the same run sharing an idempotency
    # key are rejected, even with different version_no — this absorbs event
    # redelivery.
    shared_idem = f"syn-idem-{uuid.uuid4()}"
    _insert_candidate_synthesis(
        cur,
        tenant_id=tenant_id,
        run_id=run,
        version_no=3,
        idempotency_key=shared_idem,
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_candidate_synthesis(
            cur,
            tenant_id=tenant_id,
            run_id=run,
            version_no=4,
            idempotency_key=shared_idem,
        )
        db_conn.commit()
    db_conn.rollback()

    # synthesis_claim_links toward a real logical_claim is accepted, and is
    # append-only. No FK on this table points at published_answers /
    # final_gate_reports (it does not bypass the gate).
    cur.execute(
        """
        INSERT INTO logical_claims
            (id, tenant_id, project_id, task_id,
             canonical_claim_text, canonical_claim_hash)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (tenant_id, project_id, _task, f"canon-{uuid.uuid4()}", _unique_hash()),
    )
    logical_claim_id = uuid.UUID(str(cur.fetchone()[0]))
    cur.execute(
        """
        INSERT INTO synthesis_claim_links
            (id, candidate_synthesis_id, logical_claim_id)
        VALUES (gen_random_uuid(), %s, %s)
        RETURNING id
        """,
        (syn1, logical_claim_id),
    )
    claim_link_id = uuid.UUID(str(cur.fetchone()[0]))
    db_conn.commit()

    cur.execute(
        """
        SELECT confrelid::regclass::text
        FROM pg_constraint
        WHERE conrelid = 'synthesis_claim_links'::regclass
          AND contype  = 'f'
        """
    )
    fk_targets = {str(r[0]) for r in cur.fetchall()}
    assert "published_answers" not in fk_targets
    assert "final_gate_reports" not in fk_targets
    assert "logical_claims" in fk_targets

    # APPEND-ONLY on the join.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "DELETE FROM synthesis_claim_links WHERE id = %s", (claim_link_id,)
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# 9. provider_invocations: status CHECK + is_mock + append-only + no secrets
# ---------------------------------------------------------------------------
def test_provider_invocation_records_are_append_only_and_mock_explicit(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    role = _insert_role_prompt(cur, tenant_id=tenant_id)
    cfg = _insert_agent_config(
        cur, tenant_id=tenant_id, master_prompt_id=mp, role_prompt_id=role
    )
    run = _insert_run(
        cur, tenant_id=tenant_id, project_id=project_id, master_prompt_version_id=mpv
    )
    snap = _insert_config_snapshot(cur, run_id=run, agent_config_id=cfg)
    agent_run = _insert_agent_run(cur, run_id=run, snapshot_id=snap)
    db_conn.commit()

    # is_mock column exists.
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='provider_invocations'
          AND column_name='is_mock'
        """
    )
    assert cur.fetchone() is not None

    # Insert a valid mock invocation.
    cur.execute(
        """
        INSERT INTO provider_invocations
            (id, tenant_id, agent_run_id, orchestration_run_id,
             provider_name, model, status, is_mock, attempt_no, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, 'mock', 'mock-model',
                'succeeded', TRUE, 1, %s)
        RETURNING id
        """,
        (tenant_id, agent_run, run, f"pi-{uuid.uuid4()}"),
    )
    pi_id = uuid.UUID(str(cur.fetchone()[0]))
    db_conn.commit()

    # CHECK on status: 'timeout' is NOT a principal status (QA correction).
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO provider_invocations
                (id, tenant_id, agent_run_id, provider_name, model,
                 status, is_mock, attempt_no, idempotency_key)
            VALUES (gen_random_uuid(), %s, %s, 'mock', 'mock-model',
                    'timeout', TRUE, 1, %s)
            """,
            (tenant_id, agent_run, f"pi-{uuid.uuid4()}"),
        )
        db_conn.commit()
    db_conn.rollback()

    # APPEND-ONLY: UPDATE and DELETE rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE provider_invocations SET status = 'failed' WHERE id = %s",
            (pi_id,),
        )
        db_conn.commit()
    db_conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM provider_invocations WHERE id = %s", (pi_id,))
        db_conn.commit()
    db_conn.rollback()

    # No column whose name looks like a secret / credential.
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='provider_invocations'
          AND (column_name ILIKE '%api_key%'
            OR column_name ILIKE '%secret%'
            OR column_name ILIKE '%credential%'
            OR column_name ILIKE '%token_auth%'
            OR column_name ILIKE '%password%')
        """
    )
    leak_cols = [str(r[0]) for r in cur.fetchall()]
    assert leak_cols == [], f"secret-like columns present: {leak_cols}"


# ---------------------------------------------------------------------------
# 10. token_usage_records: FK chain + append-only
# ---------------------------------------------------------------------------
def test_token_usage_records_are_append_only(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv, _ = _insert_master_prompt_version(cur, master_prompt_id=mp)
    role = _insert_role_prompt(cur, tenant_id=tenant_id)
    cfg = _insert_agent_config(
        cur, tenant_id=tenant_id, master_prompt_id=mp, role_prompt_id=role
    )
    run = _insert_run(
        cur, tenant_id=tenant_id, project_id=project_id, master_prompt_version_id=mpv
    )
    snap = _insert_config_snapshot(cur, run_id=run, agent_config_id=cfg)
    agent_run = _insert_agent_run(cur, run_id=run, snapshot_id=snap)
    cur.execute(
        """
        INSERT INTO provider_invocations
            (id, tenant_id, agent_run_id, orchestration_run_id,
             provider_name, model, status, is_mock, attempt_no, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, 'mock', 'mock-model',
                'succeeded', TRUE, 1, %s)
        RETURNING id
        """,
        (tenant_id, agent_run, run, f"pi-{uuid.uuid4()}"),
    )
    pi_id = uuid.UUID(str(cur.fetchone()[0]))
    db_conn.commit()

    # FK chain toward run / agent run / provider invocation is exercised.
    cur.execute(
        """
        INSERT INTO token_usage_records
            (id, tenant_id, orchestration_run_id, agent_run_id,
             provider_invocation_id, pass_kind, tokens_input, tokens_output,
             attempt_no, is_mock, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, 'independent_answer',
                100, 50, 1, TRUE, %s)
        RETURNING id
        """,
        (tenant_id, run, agent_run, pi_id, f"tu-{uuid.uuid4()}"),
    )
    tu_id = uuid.UUID(str(cur.fetchone()[0]))
    db_conn.commit()

    # Idempotency, provider_invocation_id IS NOT NULL case: the partial UNIQUE
    # index token_usage_records_provider_idem_uq rejects a second record with
    # the same (orchestration_run_id, provider_invocation_id, idempotency_key).
    provider_idem = f"tu-prov-{uuid.uuid4()}"
    cur.execute(
        """
        INSERT INTO token_usage_records
            (id, tenant_id, orchestration_run_id, agent_run_id,
             provider_invocation_id, tokens_input, tokens_output,
             attempt_no, is_mock, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, %s, 10, 5, 1, TRUE, %s)
        """,
        (tenant_id, run, agent_run, pi_id, provider_idem),
    )
    db_conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO token_usage_records
                (id, tenant_id, orchestration_run_id, agent_run_id,
                 provider_invocation_id, tokens_input, tokens_output,
                 attempt_no, is_mock, idempotency_key)
            VALUES (gen_random_uuid(), %s, %s, %s, %s, 20, 10, 1, TRUE, %s)
            """,
            (tenant_id, run, agent_run, pi_id, provider_idem),
        )
        db_conn.commit()
    db_conn.rollback()

    # Idempotency, provider_invocation_id IS NULL case: a plain UNIQUE over a
    # nullable column would let NULL rows duplicate freely in PostgreSQL, so a
    # dedicated partial UNIQUE index token_usage_records_no_provider_idem_uq
    # protects (orchestration_run_id, idempotency_key) when there is no
    # provider invocation. A first NULL-provider record is accepted.
    null_idem = f"tu-noprov-{uuid.uuid4()}"
    cur.execute(
        """
        INSERT INTO token_usage_records
            (id, tenant_id, orchestration_run_id, agent_run_id,
             provider_invocation_id, tokens_input, tokens_output,
             attempt_no, is_mock, idempotency_key)
        VALUES (gen_random_uuid(), %s, %s, %s, NULL, 7, 3, 1, TRUE, %s)
        """,
        (tenant_id, run, agent_run, null_idem),
    )
    db_conn.commit()
    # A second NULL-provider record with the same
    # (orchestration_run_id, idempotency_key) is rejected.
    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO token_usage_records
                (id, tenant_id, orchestration_run_id, agent_run_id,
                 provider_invocation_id, tokens_input, tokens_output,
                 attempt_no, is_mock, idempotency_key)
            VALUES (gen_random_uuid(), %s, %s, %s, NULL, 8, 4, 1, TRUE, %s)
            """,
            (tenant_id, run, agent_run, null_idem),
        )
        db_conn.commit()
    db_conn.rollback()

    # APPEND-ONLY: UPDATE and DELETE rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE token_usage_records SET tokens_input = 999 WHERE id = %s",
            (tu_id,),
        )
        db_conn.commit()
    db_conn.rollback()
    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM token_usage_records WHERE id = %s", (tu_id,))
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# 11. token_budgets: pre-run config (no orchestration_run_id) + CHECKs
# ---------------------------------------------------------------------------
def test_token_budgets_are_pre_run_config(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, _task = _seed_dev(cur)
    db_conn.commit()

    # token_budgets has NO orchestration_run_id column (QA correction §6).
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='token_budgets'
          AND column_name='orchestration_run_id'
        """
    )
    assert cur.fetchone() is None

    # token_budgets carries NO FK toward orchestration_runs.
    cur.execute(
        """
        SELECT confrelid::regclass::text
        FROM pg_constraint
        WHERE conrelid = 'token_budgets'::regclass
          AND contype  = 'f'
        """
    )
    fk_targets = {str(r[0]) for r in cur.fetchall()}
    assert "orchestration_runs" not in fk_targets

    mp = _insert_master_prompt(
        cur, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    role = _insert_role_prompt(cur, tenant_id=tenant_id)
    cfg = _insert_agent_config(
        cur, tenant_id=tenant_id, master_prompt_id=mp, role_prompt_id=role
    )
    db_conn.commit()

    # A valid per_orchestration budget.
    cur.execute(
        """
        INSERT INTO token_budgets
            (id, tenant_id, master_prompt_id, budget_level,
             token_limit, overflow_policy)
        VALUES (gen_random_uuid(), %s, %s, 'per_orchestration', 100000,
                'hard_stop')
        """,
        (tenant_id, mp),
    )
    db_conn.commit()

    # CHECK on budget_level.
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO token_budgets
                (id, tenant_id, budget_level, token_limit, overflow_policy)
            VALUES (gen_random_uuid(), %s, 'not_a_level', 1, 'warn')
            """,
            (tenant_id,),
        )
        db_conn.commit()
    db_conn.rollback()

    # CHECK on overflow_policy.
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO token_budgets
                (id, tenant_id, budget_level, token_limit, overflow_policy)
            VALUES (gen_random_uuid(), %s, 'per_orchestration', 1, 'explode')
            """,
            (tenant_id,),
        )
        db_conn.commit()
    db_conn.rollback()

    # Conditional CHECK: a per_agent budget WITHOUT agent_config_id is rejected.
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO token_budgets
                (id, tenant_id, budget_level, token_limit, overflow_policy)
            VALUES (gen_random_uuid(), %s, 'per_agent', 1, 'warn')
            """,
            (tenant_id,),
        )
        db_conn.commit()
    db_conn.rollback()

    # A per_agent budget WITH a coherent agent_config_id is accepted.
    cur.execute(
        """
        INSERT INTO token_budgets
            (id, tenant_id, agent_config_id, budget_level,
             token_limit, overflow_policy)
        VALUES (gen_random_uuid(), %s, %s, 'per_agent', 5000, 'warn')
        """,
        (tenant_id, cfg),
    )
    db_conn.commit()
