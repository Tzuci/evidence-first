"""Worker behavior with attached documents (Phase 8.3 pipeline).

Rationale for rewrite: in 8.2 this file asserted exactly 3 audit rows for a task
with attached documents (analyzing, docs_loaded, analyzed_partial). In 8.3 the
pipeline writes 8 rows: analyzing, docs_loaded, claims_extracted, claims_classified,
claims_ledger_initialized, cve_lite_started, cve_lite_completed, analyzed_partial.

This test focuses on the consumer-level invariants:
  - the task transitions to 'analyzed_partial';
  - the audit chain contains the full 8.3 sequence in order;
  - double delivery is idempotent (same audit count and chain ok).

Detailed claim-table coverage lives in test_extractor_and_cve_lite.py.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import text

from evidencefirst_shared.db.audit import verify_task_audit_chain

from app.consumers.task_created import handle_task_created
from app.db import get_engine, transaction


EXPECTED_PIPELINE_EVENTS_8_3 = [
    "task.analyzing",
    "task.docs_loaded",
    "task.claims_extracted",
    "task.claims_classified",
    "task.claims_ledger_initialized",
    "task.cve_lite_started",
    "task.cve_lite_completed",
    "task.analyzed_partial",
]


def _seeded_dev() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(text(
            """
            SELECT t.id AS tenant_id, p.id AS project_id, u.id AS user_id
            FROM tenants t
            JOIN projects p ON p.tenant_id = t.id AND p.name = 'default'
            LEFT JOIN users u ON u.tenant_id = t.id AND u.email = 'dev@local'
            WHERE t.slug = 'dev'
            """
        )).first()
    assert row is not None, "Run `make seed` first."
    return (
        uuid.UUID(str(row.tenant_id)),
        uuid.UUID(str(row.project_id)),
        uuid.UUID(str(row.user_id)),
    )


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _create_task_with_doc(tenant_id, project_id, user_id) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a minimal document (with a chunk and a clean span) attached to a fresh task.

    Hashes are unique per call so the test is rerun-safe.
    """
    marker = uuid.uuid4().hex[:8]
    chunk_text = f"Sales grew by 12 percent in {marker}.\nNew customers: 3412.\n"
    quote = chunk_text
    quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()

    content_hash = _unique_hash()
    text_hash = _unique_hash()
    chunk_hash = _unique_hash()

    doc_id = uuid.uuid4()
    blob_id = uuid.uuid4()
    obj_id = uuid.uuid4()
    dv_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    span_id = uuid.uuid4()
    task_id: uuid.UUID

    with transaction() as conn:
        conn.execute(
            text(
                """
                INSERT INTO storage_blobs (id, tenant_namespace_id, content_hash, hash_algorithm,
                                           size_bytes, mime_type, storage_backend, local_path, refcount)
                VALUES (:id, NULL, :h, 'sha256', :sz, 'text/plain', 'local_fs', :lp, 0)
                """
            ),
            {"id": blob_id, "h": content_hash, "sz": len(chunk_text), "lp": f"/tmp/{content_hash}"},
        )
        conn.execute(
            text(
                """
                INSERT INTO storage_objects (id, tenant_id, project_id, blob_id,
                                             object_type, logical_owner_kind, logical_owner_id)
                VALUES (:id, :t, :p, :b, 'upload', 'uploaded_document', :oid)
                """
            ),
            {"id": obj_id, "t": tenant_id, "p": project_id, "b": blob_id, "oid": doc_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO uploaded_documents (id, tenant_id, project_id, storage_object_id,
                                                filename, content_hash, mime_type, size_bytes,
                                                tier, language, created_by)
                VALUES (:id, :t, :p, :so, :fn, :h, 'text/plain', :sz,
                        'user_provided', 'und', :u)
                """
            ),
            {
                "id": doc_id,
                "t": tenant_id,
                "p": project_id,
                "so": obj_id,
                "fn": f"docs-83-{uuid.uuid4()}.txt",
                "h": content_hash,
                "sz": len(chunk_text),
                "u": user_id,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO document_versions (id, document_id, version_no, version_kind,
                                               storage_object_id, inline_text, text_hash)
                VALUES (:id, :did, 1, 'parsed', :so, :it, :th)
                """
            ),
            {"id": dv_id, "did": doc_id, "so": obj_id, "it": chunk_text, "th": text_hash},
        )
        conn.execute(
            text(
                """
                INSERT INTO document_chunks (id, document_version_id, chunk_index,
                                             char_start, char_end, inline_text, text_hash)
                VALUES (:id, :dv, 0, 0, :ce, :it, :th)
                """
            ),
            {"id": chunk_id, "dv": dv_id, "ce": len(chunk_text), "it": chunk_text, "th": chunk_hash},
        )
        conn.execute(
            text(
                """
                INSERT INTO evidence_spans (id, document_chunk_id, char_start, char_end, quote, quote_hash)
                VALUES (:id, :cid, 0, :ce, :q, :qh)
                """
            ),
            {"id": span_id, "cid": chunk_id, "ce": len(quote), "q": quote, "qh": quote_hash},
        )
        task_row = conn.execute(
            text(
                """
                INSERT INTO task_masters
                    (tenant_id, project_id, created_by, mode, objective, status)
                VALUES (:t, :p, :u, 'closed_corpus', :obj, 'created')
                RETURNING id
                """
            ),
            {"t": tenant_id, "p": project_id, "u": user_id, "obj": f"obj-{uuid.uuid4()}"},
        ).one()
        task_id = uuid.UUID(str(task_row[0]))

        conn.execute(
            text(
                """
                INSERT INTO task_documents (task_id, document_id, role, position)
                VALUES (:tid, :did, 'source', 0)
                """
            ),
            {"tid": task_id, "did": doc_id},
        )
    return task_id, doc_id


def _task_status(task_id: uuid.UUID) -> str:
    with get_engine().connect() as conn:
        return str(
            conn.execute(text("SELECT status FROM task_masters WHERE id = :id"), {"id": task_id}).scalar_one()
        )


def _audit_event_types(task_id: uuid.UUID) -> list[str]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT event_type FROM audit_records
                WHERE task_id = :t AND chain_scope = 'task'
                ORDER BY chain_seq ASC
                """
            ),
            {"t": task_id},
        ).fetchall()
    return [r[0] for r in rows]


def _audit_count(task_id: uuid.UUID) -> int:
    return len(_audit_event_types(task_id))


def test_worker_with_docs_runs_full_8_3_pipeline_to_analyzed_partial():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    task_id, _ = _create_task_with_doc(tenant_id, project_id, user_id)
    idem = f"idem-docs-{task_id}"
    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": idem,
    }
    s1 = handle_task_created(event, consumer_name="worker_test_docs_83")
    assert s1 == "processed"
    assert _task_status(task_id) == "analyzed_partial"

    events = _audit_event_types(task_id)
    assert events == EXPECTED_PIPELINE_EVENTS_8_3, events
    audit_count_after_first = _audit_count(task_id)
    assert audit_count_after_first == len(EXPECTED_PIPELINE_EVENTS_8_3)

    # Double delivery is idempotent.
    s2 = handle_task_created(event, consumer_name="worker_test_docs_83")
    assert s2 == "skipped_already_succeeded"
    assert _task_status(task_id) == "analyzed_partial"
    assert _audit_count(task_id) == audit_count_after_first

    with get_engine().begin() as conn:
        result = verify_task_audit_chain(conn, task_id=task_id)
    assert result["ok"] is True


def test_worker_with_docs_redelivery_with_new_idempotency_key_is_terminal():
    """A redelivery with a different idempotency_key against an already-analyzed_partial
    task must mark the new epr 'succeeded' WITHOUT appending audit rows.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    task_id, _ = _create_task_with_doc(tenant_id, project_id, user_id)
    e1 = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": f"first-{task_id}",
    }
    assert handle_task_created(e1, consumer_name="worker_test_docs_83_term") == "processed"
    audit_after_first = _audit_count(task_id)
    assert audit_after_first == len(EXPECTED_PIPELINE_EVENTS_8_3)

    e2 = {**e1, "event_id": str(uuid.uuid4()), "idempotency_key": f"second-{task_id}"}
    assert (
        handle_task_created(e2, consumer_name="worker_test_docs_83_term")
        == "skipped_terminal"
    )
    assert _audit_count(task_id) == audit_after_first

    with get_engine().begin() as conn:
        result = verify_task_audit_chain(conn, task_id=task_id)
    assert result["ok"] is True