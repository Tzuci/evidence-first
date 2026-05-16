"""Worker consumer tests for the with-docs branch (Phase 8.4 + Phase 8.7E +
Phase 8.8A).

Coverage:
  - With-docs full pipeline (approved): task ends in status='published',
    audit chain has the 15 worker events of the 8.8A approved sequence.
  - With-docs rejected zero-verified: task ends in status='analyzed_partial'
    with a final_gate_reports row, audit chain ends with task.publication_held.
  - No-docs branch invariato: task ends in status='blocked'.
  - Redelivery with fresh idempotency_key after the approved/rejected run
    completes returns 'skipped_terminal' and does not append any audit row.

Phase 8.8A worker audit sequence (approved with-docs):
  1  task.analyzing
  2  task.docs_loaded
  3  task.claims_extracted
  4  task.claims_classified
  5  task.claims_ledger_initialized
  6  task.cve_lite_started
  7  task.cve_lite_completed
  8  task.analyzed_partial
  9  task.source_quality_assessed
  10 task.entailment_checked
  11 task.compiling
  12 task.draft_compiled
  13 task.final_gate_started
  14 task.final_gate_completed
  15 task.published

Phase 8.8A worker audit sequence (rejected zero-verified with-docs):
  1  task.analyzing
  2  task.docs_loaded
  3  task.claims_extracted
  4  task.claims_classified
  5  task.claims_ledger_initialized
  6  task.cve_lite_started
  7  task.cve_lite_completed
  8  task.analyzed_partial
  9  task.source_quality_assessed
  10 task.entailment_checked
  11 task.compiling
  12 task.draft_compiled
  13 task.final_gate_started
  14 task.final_gate_completed
  15 task.publication_held

No-docs branch (invariato):
  1  task.analyzing
  2  task.blocked

Notes:
  - verify_task_audit_chain expects a Connection, not an Engine. Always wrap
    the call in `with get_engine().connect() as conn: ...` before invoking it.
  - All tests are rerun-safe (UUID/hash/marker unique per invocation).
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.consumers.task_created import handle_task_created
from app.db import get_engine
from evidencefirst_shared.db.audit import verify_task_audit_chain


CONSUMER_NAME = "test_consumer_with_documents"


# Phase 8.8A expected sequences. Audit rows for chain_scope='task' are listed
# in the order they appear (chain_seq ASC). The two API-emitted events
# (task.created, task.docs_attached) are NOT included here: these tests
# exercise only the worker, so they assert on the worker subset.
EXPECTED_PIPELINE_EVENTS_8_8A_APPROVED: list[str] = [
    "task.analyzing",
    "task.docs_loaded",
    "task.claims_extracted",
    "task.claims_classified",
    "task.claims_ledger_initialized",
    "task.cve_lite_started",
    "task.cve_lite_completed",
    "task.analyzed_partial",
    "task.source_quality_assessed",
    "task.entailment_checked",
    "task.compiling",
    "task.draft_compiled",
    "task.final_gate_started",
    "task.final_gate_completed",
    "task.published",
]

EXPECTED_PIPELINE_EVENTS_8_8A_REJECTED: list[str] = [
    "task.analyzing",
    "task.docs_loaded",
    "task.claims_extracted",
    "task.claims_classified",
    "task.claims_ledger_initialized",
    "task.cve_lite_started",
    "task.cve_lite_completed",
    "task.analyzed_partial",
    "task.source_quality_assessed",
    "task.entailment_checked",
    "task.compiling",
    "task.draft_compiled",
    "task.final_gate_started",
    "task.final_gate_completed",
    "task.publication_held",
]

EXPECTED_PIPELINE_EVENTS_NO_DOCS: list[str] = [
    "task.analyzing",
    "task.blocked",
]


# ---------------------------------------------------------------------------
# helpers (rerun-safe; aligned with the real schema in 0002/0003)
# ---------------------------------------------------------------------------
def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seeded_dev(conn: Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    row = conn.execute(
        text(
            """
            INSERT INTO tenants (name, slug, status) VALUES ('Dev','dev','active')
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """
        )
    ).first()
    if row is None:
        row = conn.execute(text("SELECT id FROM tenants WHERE slug = 'dev'")).one()
    tenant_id = uuid.UUID(str(row[0]))

    row = conn.execute(
        text(
            """
            INSERT INTO users (tenant_id, email, display_name, status)
            VALUES (:t, 'dev@local', 'Dev', 'active')
            ON CONFLICT (tenant_id, email) DO NOTHING
            RETURNING id
            """
        ),
        {"t": tenant_id},
    ).first()
    if row is None:
        row = conn.execute(
            text("SELECT id FROM users WHERE tenant_id = :t AND email = 'dev@local'"),
            {"t": tenant_id},
        ).one()
    user_id = uuid.UUID(str(row[0]))

    project_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO projects (tenant_id, name, mode_default)
                    VALUES (:t, :n, 'closed_corpus')
                    RETURNING id
                    """
                ),
                {"t": tenant_id, "n": f"consumer-with-docs-{uuid.uuid4()}"},
            ).first()[0]
        )
    )

    task_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO task_masters
                        (tenant_id, project_id, created_by, mode, objective, status)
                    VALUES (:t, :p, :u, 'closed_corpus', :o, 'created')
                    RETURNING id
                    """
                ),
                {
                    "t": tenant_id,
                    "p": project_id,
                    "u": user_id,
                    "o": f"obj-{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )
    return tenant_id, project_id, user_id, task_id


def _create_doc_with_chunk_and_span(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
    quote_hash_override: str | None = None,
) -> uuid.UUID:
    marker = uuid.uuid4().hex[:12]
    quote = f"Sales grew by 37 percent in {marker}."
    chunk_text = (
        f"Q3 report summary {marker}. {quote} "
        f"There were 3412 new customers in {marker}."
    )
    content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    size_bytes = len(chunk_text.encode("utf-8"))

    blob_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO storage_blobs
                        (id, tenant_namespace_id, content_hash, hash_algorithm,
                         size_bytes, mime_type, storage_backend, local_path, refcount)
                    VALUES
                        (:id, NULL, :h, 'sha256',
                         :sz, 'text/plain', 'local_fs', :lp, 0)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "h": content_hash + "-" + uuid.uuid4().hex,
                    "sz": size_bytes,
                    "lp": f"/dev/null/{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )

    storage_object_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO storage_objects
                        (id, tenant_id, project_id, blob_id,
                         object_type, logical_owner_kind, logical_owner_id)
                    VALUES
                        (:id, :t, :p, :b,
                         'upload', 'uploaded_document', :oid)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "b": blob_id,
                    "oid": uuid.uuid4(),
                },
            ).first()[0]
        )
    )

    doc_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO uploaded_documents
                        (id, tenant_id, project_id, storage_object_id,
                         filename, content_hash, mime_type, size_bytes,
                         tier, language, created_by)
                    VALUES
                        (:id, :t, :p, :so,
                         :fn, :h, 'text/plain', :sz,
                         'user_provided', 'und', :u)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "so": storage_object_id,
                    "fn": f"doc-{marker}.txt",
                    "h": content_hash,
                    "sz": size_bytes,
                    "u": created_by,
                },
            ).first()[0]
        )
    )

    dv_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO document_versions
                        (id, document_id, version_no, version_kind,
                         storage_object_id, inline_text, text_hash)
                    VALUES
                        (:id, :did, 1, 'parsed',
                         :so, :it, :th)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "did": doc_id,
                    "so": storage_object_id,
                    "it": chunk_text,
                    "th": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    chunk_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO document_chunks
                        (id, document_version_id, chunk_index,
                         char_start, char_end, inline_text, text_hash)
                    VALUES
                        (:id, :dv, 0,
                         0, :ce, :it, :th)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "dv": dv_id,
                    "ce": len(chunk_text),
                    "it": chunk_text,
                    "th": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    quote_hash = (
        quote_hash_override
        if quote_hash_override is not None
        else hashlib.sha256(quote.encode("utf-8")).hexdigest()
    )
    char_start = chunk_text.index(quote)
    char_end = char_start + len(quote)
    conn.execute(
        text(
            """
            INSERT INTO evidence_spans
                (id, document_chunk_id, char_start, char_end, quote, quote_hash)
            VALUES
                (:id, :cid, :cs, :ce, :q, :qh)
            """
        ),
        {
            "id": uuid.uuid4(),
            "cid": chunk_id,
            "cs": char_start,
            "ce": char_end,
            "q": quote,
            "qh": quote_hash,
        },
    )

    return doc_id


def _attach_doc_to_task(
    conn: Connection, *, task_id: uuid.UUID, document_id: uuid.UUID
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO task_documents (task_id, document_id, role, position)
            VALUES (:t, :d, 'reference', 0)
            ON CONFLICT (task_id, document_id) DO NOTHING
            """
        ),
        {"t": task_id, "d": document_id},
    )


def _make_event(
    *, tenant_id: uuid.UUID, project_id: uuid.UUID, task_id: uuid.UUID
) -> dict[str, str]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": str(uuid.uuid4()),
    }


def _setup_task_with_doc(
    *,
    quote_hash_override: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        doc_id = _create_doc_with_chunk_and_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
            quote_hash_override=quote_hash_override,
        )
        _attach_doc_to_task(conn, task_id=task_id, document_id=doc_id)
    return tenant_id, project_id, user_id, task_id


def _setup_task_without_doc() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with engine.begin() as conn:
        return _seeded_dev(conn)


def _fetch_worker_audit_event_types(conn: Connection, task_id: uuid.UUID) -> list[str]:
    """Return the worker-emitted event_types for chain_scope='task'.

    The API emits task.created (always) and task.docs_attached (when documents
    are attached on creation). In these tests we never call the API: tasks are
    INSERTed directly into task_masters, so there are no API audit rows to
    filter out. We filter them out defensively anyway, to make the assertions
    robust to any future direct INSERT path that might emit them.
    """
    rows = conn.execute(
        text(
            """
            SELECT event_type
            FROM audit_records
            WHERE chain_scope = 'task' AND scope_id = :t
            ORDER BY chain_seq ASC
            """
        ),
        {"t": task_id},
    ).fetchall()
    api_events = {"task.created", "task.docs_attached"}
    return [str(r[0]) for r in rows if str(r[0]) not in api_events]


def _fetch_task_status(conn: Connection, task_id: uuid.UUID) -> str:
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


def _audit_count(conn: Connection, task_id: uuid.UUID) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM audit_records
                WHERE chain_scope = 'task' AND scope_id = :t
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )


def _verify_chain(task_id: uuid.UUID) -> None:
    """verify_task_audit_chain expects a Connection. Wrap explicitly."""
    with get_engine().connect() as conn:
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
    assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 1 — with-docs full 8.8A pipeline ends in 'published'
# ---------------------------------------------------------------------------
def test_worker_with_docs_runs_full_8_4_pipeline_to_published():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "published"

        worker_events = _fetch_worker_audit_event_types(conn, task_id)
        assert worker_events == EXPECTED_PIPELINE_EVENTS_8_8A_APPROVED

    _verify_chain(task_id)


# ---------------------------------------------------------------------------
# Test 2 — with-docs rejected zero-verified ends 'analyzed_partial' + publication_held
# ---------------------------------------------------------------------------
def test_worker_with_docs_rejected_zero_verified_runs_full_8_4_to_publication_held():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc(
        quote_hash_override=_unique_hash(),
    )

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "analyzed_partial"

        worker_events = _fetch_worker_audit_event_types(conn, task_id)
        assert worker_events == EXPECTED_PIPELINE_EVENTS_8_8A_REJECTED

        gate_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM final_gate_reports WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert gate_count == 1

    _verify_chain(task_id)


# ---------------------------------------------------------------------------
# Test 3 — no-docs branch invariato (task.analyzing, task.blocked)
# ---------------------------------------------------------------------------
def test_worker_without_docs_goes_to_blocked():
    tenant_id, project_id, _user_id, task_id = _setup_task_without_doc()

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "blocked"

        worker_events = _fetch_worker_audit_event_types(conn, task_id)
        assert worker_events == EXPECTED_PIPELINE_EVENTS_NO_DOCS

    _verify_chain(task_id)


# ---------------------------------------------------------------------------
# Test 4 — redelivery with fresh idempotency_key after approved is terminal
# ---------------------------------------------------------------------------
def test_worker_with_docs_redelivery_with_new_idempotency_key_is_terminal():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    rc1 = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc1 == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        audit_count_before = _audit_count(conn, task_id)
        assert _fetch_task_status(conn, task_id) == "published"
        assert audit_count_before == len(EXPECTED_PIPELINE_EVENTS_8_8A_APPROVED)

    fresh_event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": str(uuid.uuid4()),
    }
    rc2 = handle_task_created(fresh_event, consumer_name=CONSUMER_NAME)
    assert rc2 == "skipped_terminal"

    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "published"
        audit_count_after = _audit_count(conn, task_id)
        assert audit_count_after == audit_count_before

    _verify_chain(task_id)


# ---------------------------------------------------------------------------
# Test 5 — redelivery with fresh idempotency_key after rejected is terminal
# ---------------------------------------------------------------------------
def test_worker_with_docs_redelivery_after_rejected_is_terminal():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc(
        quote_hash_override=_unique_hash(),
    )

    rc1 = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc1 == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        audit_count_before = _audit_count(conn, task_id)
        assert _fetch_task_status(conn, task_id) == "analyzed_partial"
        assert audit_count_before == len(EXPECTED_PIPELINE_EVENTS_8_8A_REJECTED)

    fresh_event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": str(uuid.uuid4()),
    }
    rc2 = handle_task_created(fresh_event, consumer_name=CONSUMER_NAME)
    assert rc2 == "skipped_terminal"

    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "analyzed_partial"
        audit_count_after = _audit_count(conn, task_id)
        assert audit_count_after == audit_count_before

    _verify_chain(task_id)
