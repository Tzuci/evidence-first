"""
Test minimi sul database di foundation.

Prerequisiti:
  - `make up`
  - `make migrate` (oppure il test sopra in test_migrate.py li applica)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import psycopg
import uuid

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_migration_applied(conn):
    spec = importlib.util.spec_from_file_location(
        "migrate_module", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rc = module.cmd_apply(conn, target=None, dry_run=False)
    assert rc == 0


def _row_exists(cur, table: str, col: str, value: str) -> bool:
    cur.execute(f"SELECT 1 FROM {table} WHERE {col} = %s LIMIT 1", (value,))
    return cur.fetchone() is not None


def _get_or_create_tenant(cur, slug: str, name: str) -> str:
    cur.execute(
        "INSERT INTO tenants (name, slug, status) VALUES (%s, %s, %s) "
        "ON CONFLICT (slug) DO NOTHING RETURNING id",
        (name, slug, "active"),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM tenants WHERE slug = %s", (slug,))
        row = cur.fetchone()
    return row[0]


def _get_or_create_user(cur, tenant_id: str, email: str, display_name: str) -> str:
    cur.execute(
        "INSERT INTO users (tenant_id, email, display_name, status) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id",
        (tenant_id, email, display_name, "active"),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND email = %s",
            (tenant_id, email),
        )
        row = cur.fetchone()
    return row[0]


def _get_or_create_project(cur, tenant_id: str, name: str, created_by: str) -> str:
    cur.execute(
        "INSERT INTO projects (tenant_id, name, mode_default, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id",
        (tenant_id, name, "closed_corpus", created_by),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM projects WHERE tenant_id = %s AND name = %s",
            (tenant_id, name),
        )
        row = cur.fetchone()
    return row[0]


def _create_task(cur, tenant_id: str, project_id: str, created_by: str, objective: str) -> str:
    cur.execute(
        "INSERT INTO task_masters "
        "(tenant_id, project_id, created_by, mode, objective, status) "
        "VALUES (%s, %s, %s, 'closed_corpus', %s, 'created') "
        "RETURNING id",
        (tenant_id, project_id, created_by, objective),
    )
    return cur.fetchone()[0]


def test_extensions_present(db_conn):
    _ensure_migration_applied(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension")
        names = {row[0] for row in cur.fetchall()}
    assert "pgcrypto" in names
    assert "citext" in names


def test_insert_tenant_user_project(db_conn):
    _ensure_migration_applied(db_conn)
    cur = db_conn.cursor()
    tenant_id = _get_or_create_tenant(cur, "test-basic", "Test Tenant")
    user_id = _get_or_create_user(cur, tenant_id, "alice@test.local", "Alice")
    project_id = _get_or_create_project(cur, tenant_id, "test-project", user_id)
    db_conn.commit()
    assert tenant_id and user_id and project_id


def test_unique_constraint_tenants_slug(db_conn):
    _ensure_migration_applied(db_conn)
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO tenants (name, slug, status) VALUES (%s, %s, %s) "
        "ON CONFLICT (slug) DO NOTHING",
        ("Dup A", "dup-slug-test", "active"),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        with db_conn.cursor() as cur2:
            cur2.execute(
                "INSERT INTO tenants (name, slug, status) VALUES (%s, %s, %s)",
                ("Dup B", "dup-slug-test", "active"),
            )
        db_conn.commit()
    db_conn.rollback()


def test_audit_records_append_only(db_conn):
    """
    Inserisce un audit record con scope='task' e scope_id=task_id.
    Verifica che UPDATE e DELETE siano rifiutati dal trigger append-only.
    """
    _ensure_migration_applied(db_conn)
    cur = db_conn.cursor()

    # Setup minimo: tenant -> user -> project -> task
    tenant_id = _get_or_create_tenant(cur, "audit-test", "Audit Tenant")
    user_id = _get_or_create_user(cur, tenant_id, "audit@test.local", "Audit User")
    project_id = _get_or_create_project(cur, tenant_id, "audit-project", user_id)
    task_id = _create_task(cur, tenant_id, project_id, user_id, "test-objective")
    db_conn.commit()

    # Inserisce un audit record con coerenza scope: chain_scope='task' e scope_id=task_id
    cur.execute(
        """
        INSERT INTO audit_records (
            tenant_id, project_id, task_id,
            chain_scope, scope_id, chain_seq,
            event_hash,
            event_type, actor_type, actor_id, redacted_payload
        ) VALUES (
            %s, %s, %s,
            'task', %s, 1,
            decode(%s, 'hex'),
            'test.event', 'system', 'test', '{}'::jsonb
        )
        RETURNING id
        """,
        (tenant_id, project_id, task_id, task_id, "00" * 32),
    )
    audit_id = cur.fetchone()[0]
    db_conn.commit()

    # Tentativo di UPDATE → rifiutato dal trigger
    with pytest.raises(psycopg.errors.RaiseException):
        with db_conn.cursor() as cur2:
            cur2.execute(
                "UPDATE audit_records SET event_type = 'changed' WHERE id = %s",
                (audit_id,),
            )
        db_conn.commit()
    db_conn.rollback()

    # Tentativo di DELETE → rifiutato
    with pytest.raises(psycopg.errors.RaiseException):
        with db_conn.cursor() as cur2:
            cur2.execute("DELETE FROM audit_records WHERE id = %s", (audit_id,))
        db_conn.commit()
    db_conn.rollback()


def test_audit_scope_consistency_violation(db_conn):
    """
    Verifica il CHECK audit_scope_consistency:
    chain_scope='task' richiede task_id IS NOT NULL e scope_id = task_id.
    Tentare di inserire con scope_id diverso da task_id deve fallire.
    """
    _ensure_migration_applied(db_conn)
    cur = db_conn.cursor()
    tenant_id = _get_or_create_tenant(cur, "audit-scope-test", "Scope Tenant")
    user_id = _get_or_create_user(cur, tenant_id, "scope@test.local", "Scope User")
    project_id = _get_or_create_project(cur, tenant_id, "scope-project", user_id)
    task_id = _create_task(cur, tenant_id, project_id, user_id, "scope-test")
    db_conn.commit()

    # scope_id deliberatamente diverso da task_id → deve fallire il CHECK
    bogus_uuid = "11111111-1111-1111-1111-111111111111"
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.cursor() as cur2:
            cur2.execute(
                """
                INSERT INTO audit_records (
                    tenant_id, project_id, task_id,
                    chain_scope, scope_id, chain_seq,
                    event_hash,
                    event_type, actor_type, actor_id, redacted_payload
                ) VALUES (
                    %s, %s, %s,
                    'task', %s, 1,
                    decode(%s, 'hex'),
                    'test.event', 'system', 'test', '{}'::jsonb
                )
                """,
                (tenant_id, project_id, task_id, bogus_uuid, "00" * 32),
            )
        db_conn.commit()
    db_conn.rollback()


def test_event_processing_records_immutable_fields(db_conn):
    _ensure_migration_applied(db_conn)
    cur = db_conn.cursor()

    tenant_id = _get_or_create_tenant(cur, "epr-test", "EPR Tenant")
    db_conn.commit()

    consumer_name = f"test_consumer_{uuid.uuid4().hex}"
    idempotency_key = f"idem-{uuid.uuid4().hex}"

    cur.execute(
        """
        INSERT INTO event_processing_records
            (event_id, event_type, consumer_name, idempotency_key,
             tenant_id, processing_status)
        VALUES (gen_random_uuid(), 'test.event', %s, %s,
                %s, 'started')
        RETURNING id
        """,
        (consumer_name, idempotency_key, tenant_id),
    )
    epr_id = cur.fetchone()[0]
    db_conn.commit()

    # Update legittimo dello status: passa
    with db_conn.cursor() as cur2:
        cur2.execute(
            "UPDATE event_processing_records SET processing_status = 'succeeded', "
            "completed_at = NOW() WHERE id = %s",
            (epr_id,),
        )
    db_conn.commit()

    # Tentativo di modificare un campo immutabile: deve fallire
    with pytest.raises(psycopg.errors.RaiseException):
        with db_conn.cursor() as cur2:
            cur2.execute(
                "UPDATE event_processing_records SET event_type = 'hacked' WHERE id = %s",
                (epr_id,),
            )
        db_conn.commit()
    db_conn.rollback()
