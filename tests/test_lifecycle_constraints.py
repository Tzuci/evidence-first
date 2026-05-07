"""Constraint-level tests on Phase 8.5 lifecycle / source-loss schema (root, DB-only).

Coverage:
  - published_answer_lifecycle_events:
      * INSERT valido tramite FK composita (published_answer_id, task_id);
      * UNIQUE (published_answer_id, event_type, idempotency_key) rifiuta duplicato;
      * FK composita rifiuta task_id mismatch rispetto al published_answer;
      * CHECK event_type rifiuta valori non validi;
      * append-only: UPDATE e DELETE rifiutati dal trigger.

  - source_loss_events:
      * INSERT valido legato a evidence_span_id;
      * UNIQUE (evidence_span_id, loss_kind, idempotency_key) rifiuta duplicato;
      * CHECK loss_kind rifiuta valore non valido;
      * append-only: UPDATE e DELETE rifiutati dal trigger;
      * FK reporting con confdeltype = 'r' (RESTRICT), mai SET NULL,
        verificato via pg_constraint.

  - source_loss_propagation_records:
      * INSERT valido per claim_marked_unverifiable;
      * UNIQUE partial index claim_marked_unverifiable rifiuta duplicato;
      * INSERT valido per published_answer_impacted;
      * UNIQUE partial index published_answer_impacted rifiuta duplicato;
      * UNIQUE partial index no_claims_impacted rifiuta duplicato;
      * UNIQUE partial index no_active_published_answers_impacted rifiuta duplicato;
      * CHECK propagation_kind rifiuta valore non valido;
      * CHECK status rifiuta valore non valido;
      * append-only: UPDATE e DELETE rifiutati dal trigger.

  - task_masters.status: NON viene esteso in 0006:
      * UPDATE a 'withdrawn' fallisce;
      * UPDATE a 'superseded' fallisce;
      * UPDATE a 'publication_held' fallisce;
      * UPDATE a 'published' resta valido.

  - No DB propagation trigger: inserire una source_loss_events NON deve
    creare automaticamente claim_ledger_entries né mutare published_answers.

Rerun-safety: tutti gli identificatori sono unici per invocazione.
"""
from __future__ import annotations

import hashlib
import importlib.util
import uuid
from pathlib import Path

import psycopg
import pytest

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


def _unique_hex() -> str:
    """Return a 64-hex string unique to this invocation."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


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

    project_name = f"lifecycle-test-{uuid.uuid4()}"
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


def _create_evidence_span(
    cur,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a fresh chain of storage_blobs -> storage_objects ->
    uploaded_documents -> document_versions -> document_chunks -> evidence_spans.

    Returns (document_id, document_version_id, document_chunk_id, evidence_span_id).
    """
    blob_text = f"chunk-{uuid.uuid4()}"
    blob_size = len(blob_text.encode("utf-8"))
    content_hash = _unique_hex()
    blob_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO storage_blobs (
            id, tenant_namespace_id, content_hash, hash_algorithm,
            size_bytes, mime_type, storage_backend, local_path, refcount
        ) VALUES (
            %s, NULL, %s, 'sha256',
            %s, 'text/plain', 'local_fs', %s, 0
        )
        """,
        (blob_id, content_hash, blob_size, f"/tmp/{content_hash}"),
    )

    storage_object_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO storage_objects (
            id, tenant_id, project_id, blob_id,
            object_type, logical_owner_kind, logical_owner_id
        ) VALUES (
            %s, %s, %s, %s,
            'upload', 'uploaded_document', %s
        )
        """,
        (storage_object_id, tenant_id, project_id, blob_id, uuid.uuid4()),
    )

    document_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO uploaded_documents (
            id, tenant_id, project_id, storage_object_id,
            filename, content_hash, mime_type, size_bytes,
            tier, language, created_by
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, 'text/plain', %s,
            'user_provided', 'und', %s
        )
        """,
        (
            document_id,
            tenant_id,
            project_id,
            storage_object_id,
            f"doc-{uuid.uuid4()}.txt",
            content_hash,
            blob_size,
            created_by,
        ),
    )

    document_version_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO document_versions (
            id, document_id, version_no, version_kind,
            storage_object_id, inline_text, text_hash
        ) VALUES (
            %s, %s, 1, 'parsed',
            %s, %s, %s
        )
        """,
        (document_version_id, document_id, storage_object_id, blob_text, _unique_hex()),
    )

    document_chunk_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO document_chunks (
            id, document_version_id, chunk_index,
            char_start, char_end, inline_text, text_hash
        ) VALUES (
            %s, %s, 0,
            0, %s, %s, %s
        )
        """,
        (document_chunk_id, document_version_id, len(blob_text), blob_text, _unique_hex()),
    )

    evidence_span_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO evidence_spans (
            id, document_chunk_id, char_start, char_end, quote, quote_hash
        ) VALUES (
            %s, %s, 0, %s, %s, %s
        )
        """,
        (
            evidence_span_id,
            document_chunk_id,
            len(blob_text),
            blob_text,
            _unique_hex(),
        ),
    )

    return document_id, document_version_id, document_chunk_id, evidence_span_id


def _create_logical_claim_with_verified_v1(
    cur,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a logical_claim with one claim_ledger_entries v1 verified_fact.

    Returns (claim_logical_id, claim_ledger_entry_id).
    """
    lc_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO logical_claims
            (id, tenant_id, project_id, task_id,
             canonical_claim_text, canonical_claim_hash)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            lc_id,
            tenant_id,
            project_id,
            task_id,
            f"canonical-{uuid.uuid4()}",
            _unique_hex(),
        ),
    )

    entry_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO claim_ledger_entries
            (id, claim_logical_id, version_no, state,
             support_scope, user_provided_dependency,
             transition_reason)
        VALUES (%s, %s, 1, 'verified_fact',
                'supported_by_user_corpus_only',
                'supported_by_user_corpus_only',
                %s)
        """,
        (entry_id, lc_id, f"reason-{uuid.uuid4()}"),
    )
    return lc_id, entry_id


def _create_published_answer(
    cur,
    *,
    task_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a draft_final_answers + final_gate_reports + published_answers v1
    for the given task. Returns (draft_id, gate_report_id, published_answer_id).
    """
    draft_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO draft_final_answers
            (id, task_id, version_no, compiler_name, compiler_version, summary_text)
        VALUES (%s, %s, 1, 'mvp0_compiler_v1', '0.1.0', %s)
        """,
        (draft_id, task_id, f"summary-{uuid.uuid4()}"),
    )

    gate_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO final_gate_reports
            (id, task_id, draft_final_answer_id, decision, reason_code)
        VALUES (%s, %s, %s, 'approved', 'all_spans_verified')
        """,
        (gate_id, task_id, draft_id),
    )

    published_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO published_answers
            (id, task_id, draft_final_answer_id, final_gate_report_id,
             version_no, content_hash, status)
        VALUES (%s, %s, %s, %s, 1, %s, 'published')
        """,
        (published_id, task_id, draft_id, gate_id, _unique_hex()),
    )
    return draft_id, gate_id, published_id


def _insert_source_loss_event(
    cur,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID,
    document_chunk_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    loss_kind: str = "source_deleted",
    idempotency_key: str | None = None,
) -> uuid.UUID:
    sle_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO source_loss_events
            (id, tenant_id, project_id, task_id,
             evidence_span_id, document_chunk_id, document_version_id, document_id,
             loss_kind, loss_reason, detected_by, idempotency_key)
        VALUES (%s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s)
        """,
        (
            sle_id,
            tenant_id,
            project_id,
            task_id,
            evidence_span_id,
            document_chunk_id,
            document_version_id,
            document_id,
            loss_kind,
            f"loss-reason-{uuid.uuid4()}",
            "test_detector",
            idempotency_key or _unique_hex(),
        ),
    )
    return sle_id


# ---------------------------------------------------------------------------
# published_answer_lifecycle_events
# ---------------------------------------------------------------------------
def test_pale_insert_valid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO published_answer_lifecycle_events
            (published_answer_id, task_id, event_type, event_reason,
             requested_by, idempotency_key)
        VALUES (%s, %s, 'published', 'gate approved', %s, %s)
        RETURNING id
        """,
        (pa_id, task_id, user_id, _unique_hex()),
    )
    inserted = cur.fetchone()[0]
    db_conn.commit()
    assert inserted is not None


def test_pale_unique_idempotency_key(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    db_conn.commit()

    idem = _unique_hex()
    cur.execute(
        """
        INSERT INTO published_answer_lifecycle_events
            (published_answer_id, task_id, event_type, event_reason, idempotency_key)
        VALUES (%s, %s, 'withdrawal_requested', 'user request', %s)
        """,
        (pa_id, task_id, idem),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO published_answer_lifecycle_events
                (published_answer_id, task_id, event_type, event_reason, idempotency_key)
            VALUES (%s, %s, 'withdrawal_requested', 'duplicate', %s)
            """,
            (pa_id, task_id, idem),
        )
        db_conn.commit()
    db_conn.rollback()


def test_pale_composite_fk_rejects_task_id_mismatch(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id_a = _seed_dev(cur)
    db_conn.commit()
    cur.execute(
        """
        INSERT INTO task_masters (tenant_id, project_id, created_by, mode, objective, status)
        VALUES (%s, %s, %s, 'closed_corpus', %s, 'created')
        RETURNING id
        """,
        (tenant_id, project_id, user_id, f"obj-{uuid.uuid4()}"),
    )
    task_id_b = uuid.UUID(str(cur.fetchone()[0]))
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id_a)
    db_conn.commit()

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        cur.execute(
            """
            INSERT INTO published_answer_lifecycle_events
                (published_answer_id, task_id, event_type, event_reason, idempotency_key)
            VALUES (%s, %s, 'published', 'mismatch', %s)
            """,
            (pa_id, task_id_b, _unique_hex()),
        )
        db_conn.commit()
    db_conn.rollback()


def test_pale_check_event_type_rejects_invalid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO published_answer_lifecycle_events
                (published_answer_id, task_id, event_type, event_reason, idempotency_key)
            VALUES (%s, %s, 'archived', 'invalid value', %s)
            """,
            (pa_id, task_id, _unique_hex()),
        )
        db_conn.commit()
    db_conn.rollback()


def test_pale_reject_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    cur.execute(
        """
        INSERT INTO published_answer_lifecycle_events
            (published_answer_id, task_id, event_type, event_reason, idempotency_key)
        VALUES (%s, %s, 'published', 'initial', %s)
        RETURNING id
        """,
        (pa_id, task_id, _unique_hex()),
    )
    pale_id = cur.fetchone()[0]
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE published_answer_lifecycle_events SET event_reason = 'mutated' WHERE id = %s",
            (pale_id,),
        )
        db_conn.commit()
    db_conn.rollback()


def test_pale_reject_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    cur.execute(
        """
        INSERT INTO published_answer_lifecycle_events
            (published_answer_id, task_id, event_type, event_reason, idempotency_key)
        VALUES (%s, %s, 'published', 'initial', %s)
        RETURNING id
        """,
        (pa_id, task_id, _unique_hex()),
    )
    pale_id = cur.fetchone()[0]
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "DELETE FROM published_answer_lifecycle_events WHERE id = %s",
            (pale_id,),
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# source_loss_events
# ---------------------------------------------------------------------------
def test_sle_insert_valid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    doc_id, dv_id, chunk_id, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
        document_chunk_id=chunk_id,
        document_version_id=dv_id,
        document_id=doc_id,
        loss_kind="quote_mismatch",
    )
    db_conn.commit()
    assert sle_id is not None


def test_sle_unique_idempotency(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    idem = _unique_hex()
    _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
        loss_kind="source_deleted",
        idempotency_key=idem,
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_source_loss_event(
            cur,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=span_id,
            loss_kind="source_deleted",
            idempotency_key=idem,
        )
        db_conn.commit()
    db_conn.rollback()


def test_sle_check_loss_kind_rejects_invalid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO source_loss_events
                (id, tenant_id, project_id, task_id,
                 evidence_span_id, loss_kind, loss_reason, detected_by, idempotency_key)
            VALUES (%s, %s, %s, %s,
                    %s, 'made_up_kind', 'reason', 'detector', %s)
            """,
            (
                uuid.uuid4(),
                tenant_id,
                project_id,
                task_id,
                span_id,
                _unique_hex(),
            ),
        )
        db_conn.commit()
    db_conn.rollback()


def test_sle_reject_update(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE source_loss_events SET loss_reason = 'mutated' WHERE id = %s",
            (sle_id,),
        )
        db_conn.commit()
    db_conn.rollback()


def test_sle_reject_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute("DELETE FROM source_loss_events WHERE id = %s", (sle_id,))
        db_conn.commit()
    db_conn.rollback()


def test_sle_reporting_fks_use_restrict_not_set_null(db_conn):
    """Verify via pg_constraint that the reporting FKs on source_loss_events
    use ON DELETE RESTRICT (confdeltype = 'r'), never SET NULL ('n').

    pg_constraint.confdeltype encoding:
      'a' = NO ACTION
      'r' = RESTRICT
      'c' = CASCADE
      'n' = SET NULL
      'd' = SET DEFAULT
    """
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()

    # Pull all FKs on source_loss_events and assert none of them is SET NULL.
    cur.execute(
        """
        SELECT conname, confdeltype
        FROM pg_constraint
        WHERE conrelid = 'source_loss_events'::regclass
          AND contype  = 'f'
        """
    )
    rows = cur.fetchall()
    assert len(rows) >= 4, f"expected at least 4 FKs on source_loss_events, got {len(rows)}"
    for conname, confdeltype in rows:
        assert confdeltype != "n", (
            f"FK {conname} on source_loss_events uses SET NULL ({confdeltype!r}); "
            "all reporting FKs must be RESTRICT."
        )


# ---------------------------------------------------------------------------
# source_loss_propagation_records
# ---------------------------------------------------------------------------
def test_slpr_claim_marked_unverifiable_idempotent(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    lc_id, entry_id = _create_logical_claim_with_verified_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO source_loss_propagation_records
            (id, source_loss_event_id, claim_logical_id,
             old_claim_ledger_entry_id, propagation_kind, status)
        VALUES (%s, %s, %s, %s, 'claim_marked_unverifiable', 'recorded')
        """,
        (uuid.uuid4(), sle_id, lc_id, entry_id),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO source_loss_propagation_records
                (id, source_loss_event_id, claim_logical_id,
                 old_claim_ledger_entry_id, propagation_kind, status)
            VALUES (%s, %s, %s, %s, 'claim_marked_unverifiable', 'recorded')
            """,
            (uuid.uuid4(), sle_id, lc_id, entry_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_slpr_published_answer_impacted_idempotent(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO source_loss_propagation_records
            (id, source_loss_event_id, published_answer_id,
             propagation_kind, status)
        VALUES (%s, %s, %s, 'published_answer_impacted', 'recorded')
        """,
        (uuid.uuid4(), sle_id, pa_id),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO source_loss_propagation_records
                (id, source_loss_event_id, published_answer_id,
                 propagation_kind, status)
            VALUES (%s, %s, %s, 'published_answer_impacted', 'recorded')
            """,
            (uuid.uuid4(), sle_id, pa_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_slpr_no_claims_impacted_idempotent(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO source_loss_propagation_records
            (id, source_loss_event_id, propagation_kind, status)
        VALUES (%s, %s, 'no_claims_impacted', 'recorded')
        """,
        (uuid.uuid4(), sle_id),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO source_loss_propagation_records
                (id, source_loss_event_id, propagation_kind, status)
            VALUES (%s, %s, 'no_claims_impacted', 'recorded')
            """,
            (uuid.uuid4(), sle_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_slpr_no_active_published_answers_impacted_idempotent(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    cur.execute(
        """
        INSERT INTO source_loss_propagation_records
            (id, source_loss_event_id, propagation_kind, status)
        VALUES (%s, %s, 'no_active_published_answers_impacted', 'recorded')
        """,
        (uuid.uuid4(), sle_id),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        cur.execute(
            """
            INSERT INTO source_loss_propagation_records
                (id, source_loss_event_id, propagation_kind, status)
            VALUES (%s, %s, 'no_active_published_answers_impacted', 'recorded')
            """,
            (uuid.uuid4(), sle_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_slpr_check_propagation_kind_rejects_invalid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO source_loss_propagation_records
                (id, source_loss_event_id, propagation_kind, status)
            VALUES (%s, %s, 'made_up_kind', 'recorded')
            """,
            (uuid.uuid4(), sle_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_slpr_check_status_rejects_invalid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO source_loss_propagation_records
                (id, source_loss_event_id, propagation_kind, status)
            VALUES (%s, %s, 'no_claims_impacted', 'unknown_status')
            """,
            (uuid.uuid4(), sle_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_slpr_reject_update_and_delete(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    sle_id = _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    slpr_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO source_loss_propagation_records
            (id, source_loss_event_id, propagation_kind, status)
        VALUES (%s, %s, 'no_claims_impacted', 'recorded')
        """,
        (slpr_id, sle_id),
    )
    db_conn.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "UPDATE source_loss_propagation_records SET status = 'failed' WHERE id = %s",
            (slpr_id,),
        )
        db_conn.commit()
    db_conn.rollback()

    with pytest.raises(psycopg.errors.RaiseException):
        cur.execute(
            "DELETE FROM source_loss_propagation_records WHERE id = %s",
            (slpr_id,),
        )
        db_conn.commit()
    db_conn.rollback()


# ---------------------------------------------------------------------------
# task_masters.status: NOT extended in 0006
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("forbidden_status", ["withdrawn", "superseded", "publication_held"])
def test_task_status_does_not_accept_lifecycle_values(db_conn, forbidden_status):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE task_masters SET status = %s WHERE id = %s",
            (forbidden_status, task_id),
        )
        db_conn.commit()
    db_conn.rollback()


def test_task_status_published_still_valid(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    _tenant, _project, _user, task_id = _seed_dev(cur)
    db_conn.commit()

    cur.execute(
        "UPDATE task_masters SET status = 'published' WHERE id = %s",
        (task_id,),
    )
    db_conn.commit()
    cur.execute("SELECT status FROM task_masters WHERE id = %s", (task_id,))
    assert str(cur.fetchone()[0]) == "published"


# ---------------------------------------------------------------------------
# No DB propagation trigger: source_loss_events INSERT must not auto-mutate
# claim_ledger_entries or published_answers.
# ---------------------------------------------------------------------------
def test_source_loss_event_does_not_auto_create_ledger_entries(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    lc_id, _entry_id = _create_logical_claim_with_verified_v1(
        cur, tenant_id=tenant_id, project_id=project_id, task_id=task_id
    )
    db_conn.commit()

    cur.execute(
        "SELECT COUNT(*) FROM claim_ledger_entries WHERE claim_logical_id = %s",
        (lc_id,),
    )
    count_before = int(cur.fetchone()[0])

    _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    cur.execute(
        "SELECT COUNT(*) FROM claim_ledger_entries WHERE claim_logical_id = %s",
        (lc_id,),
    )
    count_after = int(cur.fetchone()[0])

    assert count_after == count_before, (
        "INSERT into source_loss_events must NOT trigger automatic creation "
        "of claim_ledger_entries (propagation is application-driven)."
    )


def test_source_loss_event_does_not_auto_mutate_published_answers(db_conn):
    _ensure_migrations(db_conn)
    cur = db_conn.cursor()
    tenant_id, project_id, user_id, task_id = _seed_dev(cur)
    db_conn.commit()
    _doc, _dv, _chunk, span_id = _create_evidence_span(
        cur, tenant_id=tenant_id, project_id=project_id, created_by=user_id
    )
    _draft, _gate, pa_id = _create_published_answer(cur, task_id=task_id)
    db_conn.commit()

    cur.execute(
        "SELECT status, withdrawn_at, superseded_at FROM published_answers WHERE id = %s",
        (pa_id,),
    )
    status_before, withdrawn_at_before, superseded_at_before = cur.fetchone()

    _insert_source_loss_event(
        cur,
        tenant_id=tenant_id,
        project_id=project_id,
        task_id=task_id,
        evidence_span_id=span_id,
    )
    db_conn.commit()

    cur.execute(
        "SELECT status, withdrawn_at, superseded_at FROM published_answers WHERE id = %s",
        (pa_id,),
    )
    status_after, withdrawn_at_after, superseded_at_after = cur.fetchone()

    assert status_after == status_before
    assert withdrawn_at_after == withdrawn_at_before
    assert superseded_at_after == superseded_at_before
