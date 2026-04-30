"""Worker pipeline tests for Phase 8.3 (extractor + CVE-lite + double delivery).

Rerun-safety: every payload, hash and marker is unique per invocation.
Append-only respected: corrupted spans are inserted corrupt, never modified.
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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
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


def _create_doc_with_chunk_and_span(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    chunk_text: str,
    quote: str,
    quote_hash_override: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one document + version + chunk + evidence_span. Returns (doc_id, span_id).

    quote_hash_override lets us simulate a corrupt span at INSERT time, which is the
    only way to exercise the FAIL branch since evidence_spans is append-only.
    """
    content_hash = _unique_hash()
    text_hash = _unique_hash()
    chunk_hash = _unique_hash()
    quote_hash = quote_hash_override or hashlib.sha256(quote.encode("utf-8")).hexdigest()
    doc_id = uuid.uuid4()
    blob_id = uuid.uuid4()
    obj_id = uuid.uuid4()
    dv_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    span_id = uuid.uuid4()

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
                "fn": f"doc-{uuid.uuid4()}.txt",
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
    return doc_id, span_id


def _create_task_with_doc(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
) -> uuid.UUID:
    with transaction() as conn:
        row = conn.execute(
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
        task_id = uuid.UUID(str(row[0]))
        conn.execute(
            text(
                """
                INSERT INTO task_documents (task_id, document_id, role, position)
                VALUES (:tid, :did, 'source', 0)
                """
            ),
            {"tid": task_id, "did": doc_id},
        )
    return task_id


def _task_status(task_id: uuid.UUID) -> str:
    with get_engine().connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM task_masters WHERE id = :id"), {"id": task_id}
            ).scalar_one()
        )


def _audit_count(task_id: uuid.UUID) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM audit_records WHERE task_id = :t AND chain_scope = 'task'"),
                {"t": task_id},
            ).scalar_one()
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


def _ledger_for_task(task_id: uuid.UUID) -> dict[uuid.UUID, list[tuple[int, str]]]:
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT cle.claim_logical_id, cle.version_no, cle.state
                FROM claim_ledger_entries cle
                JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                WHERE lc.task_id = :t
                ORDER BY cle.claim_logical_id, cle.version_no
                """
            ),
            {"t": task_id},
        ).fetchall()
    out: dict[uuid.UUID, list[tuple[int, str]]] = {}
    for r in rows:
        out.setdefault(uuid.UUID(str(r[0])), []).append((int(r[1]), str(r[2])))
    return out


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_extractor_produces_v1_and_cve_lite_verifies_pass():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    marker = uuid.uuid4().hex[:8]
    chunk_text = (
        f"Annual report {marker}.\n"
        f"Sales grew by 12 percent in {marker}.\n"
        f"There were 3412 new customers.\n"
    )
    quote = chunk_text  # whole chunk: substring is trivially true
    doc_id, _span_id = _create_doc_with_chunk_and_span(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        chunk_text=chunk_text,
        quote=quote,
    )
    task_id = _create_task_with_doc(
        tenant_id=tenant_id, project_id=project_id, user_id=user_id, doc_id=doc_id
    )

    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": f"idem-{task_id}",
    }
    assert handle_task_created(event, consumer_name="worker_test_8_3_pass") == "processed"
    assert _task_status(task_id) == "analyzed_partial"

    seq = _audit_event_types(task_id)
    expected = [
        "task.analyzing",
        "task.docs_loaded",
        "task.claims_extracted",
        "task.claims_classified",
        "task.claims_ledger_initialized",
        "task.cve_lite_started",
        "task.cve_lite_completed",
        "task.analyzed_partial",
    ]
    assert seq == expected, seq

    states = _ledger_for_task(task_id)
    assert len(states) >= 1, "extractor must produce at least one logical claim"
    for lc_id, versions in states.items():
        assert versions[0] == (1, "candidate"), (lc_id, versions)
        assert versions[-1] == (2, "verified_fact"), (lc_id, versions)

    # Each v1 must have a verification_records row with outcome='pass'.
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT vr.outcome
                FROM verification_records vr
                JOIN logical_claims lc ON lc.id = vr.claim_logical_id
                WHERE lc.task_id = :t AND vr.check_kind = 'cve_lite'
                """
            ),
            {"t": task_id},
        ).fetchall()
    assert rows, "CVE-lite must record at least one verification"
    assert all(r[0] == "pass" for r in rows), [r[0] for r in rows]

    # Lineage v1 -> v2 'supersedes' must exist for every logical claim.
    with get_engine().connect() as conn:
        n_lin = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM claim_lineage cl
                    JOIN claim_ledger_entries parent ON parent.id = cl.parent_entry_id
                    JOIN logical_claims lc ON lc.id = parent.claim_logical_id
                    WHERE lc.task_id = :t AND cl.relation_kind = 'supersedes'
                    """
                ),
                {"t": task_id},
            ).scalar_one()
        )
    assert n_lin == len(states), (n_lin, len(states))

    with get_engine().begin() as conn:
        assert verify_task_audit_chain(conn, task_id=task_id)["ok"] is True


def test_extractor_with_corrupt_span_marks_unverifiable():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    marker = uuid.uuid4().hex[:8]
    chunk_text = f"Industry sales grew by 12 percent in {marker}."
    quote = chunk_text
    bad_hash = _unique_hash()  # deliberately != sha256(quote)
    doc_id, _span_id = _create_doc_with_chunk_and_span(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        chunk_text=chunk_text,
        quote=quote,
        quote_hash_override=bad_hash,
    )
    task_id = _create_task_with_doc(
        tenant_id=tenant_id, project_id=project_id, user_id=user_id, doc_id=doc_id
    )

    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": f"idem-{task_id}",
    }
    assert handle_task_created(event, consumer_name="worker_test_8_3_fail") == "processed"
    assert _task_status(task_id) == "analyzed_partial"

    states = _ledger_for_task(task_id)
    assert len(states) >= 1
    for lc_id, versions in states.items():
        assert versions[0] == (1, "candidate"), (lc_id, versions)
        assert versions[-1] == (2, "unverifiable"), (lc_id, versions)

    with get_engine().connect() as conn:
        outcomes = [
            r[0]
            for r in conn.execute(
                text(
                    """
                    SELECT vr.outcome
                    FROM verification_records vr
                    JOIN logical_claims lc ON lc.id = vr.claim_logical_id
                    WHERE lc.task_id = :t AND vr.check_kind = 'cve_lite'
                    """
                ),
                {"t": task_id},
            ).fetchall()
        ]
    assert outcomes and all(o == "fail" for o in outcomes), outcomes


def test_double_delivery_does_not_duplicate_anything():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    tenant_id, project_id, user_id = _seeded_dev()
    marker = uuid.uuid4().hex[:8]
    chunk_text = f"Operating expenses fell 4 percent. Quote {marker}.\n"
    quote = chunk_text
    doc_id, _span_id = _create_doc_with_chunk_and_span(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        chunk_text=chunk_text,
        quote=quote,
    )
    task_id = _create_task_with_doc(
        tenant_id=tenant_id, project_id=project_id, user_id=user_id, doc_id=doc_id
    )

    event = {
        "event_id": str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": f"idem-{task_id}",
    }
    assert handle_task_created(event, consumer_name="worker_test_8_3_dd") == "processed"

    audit_after_first = _audit_count(task_id)

    def _counts() -> dict[str, int]:
        with get_engine().connect() as conn:
            return {
                "raw": int(
                    conn.execute(
                        text("SELECT COUNT(*) FROM raw_claims WHERE task_id = :t"),
                        {"t": task_id},
                    ).scalar_one()
                ),
                "logical": int(
                    conn.execute(
                        text("SELECT COUNT(*) FROM logical_claims WHERE task_id = :t"),
                        {"t": task_id},
                    ).scalar_one()
                ),
                "classified": int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM classified_claims cc
                            JOIN raw_claims rc ON rc.id = cc.raw_claim_id
                            WHERE rc.task_id = :t
                            """
                        ),
                        {"t": task_id},
                    ).scalar_one()
                ),
                "ledger": int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM claim_ledger_entries cle
                            JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                            WHERE lc.task_id = :t
                            """
                        ),
                        {"t": task_id},
                    ).scalar_one()
                ),
                "verification": int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM verification_records vr
                            JOIN logical_claims lc ON lc.id = vr.claim_logical_id
                            WHERE lc.task_id = :t
                            """
                        ),
                        {"t": task_id},
                    ).scalar_one()
                ),
                "lineage": int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM claim_lineage cl
                            JOIN claim_ledger_entries parent ON parent.id = cl.parent_entry_id
                            JOIN logical_claims lc ON lc.id = parent.claim_logical_id
                            WHERE lc.task_id = :t
                            """
                        ),
                        {"t": task_id},
                    ).scalar_one()
                ),
                "evidence_links": int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM claim_evidence_links cel
                            JOIN logical_claims lc ON lc.id = cel.claim_logical_id
                            WHERE lc.task_id = :t
                            """
                        ),
                        {"t": task_id},
                    ).scalar_one()
                ),
            }

    counts_after_first = _counts()

    # Replay.
    assert (
        handle_task_created(event, consumer_name="worker_test_8_3_dd")
        == "skipped_already_succeeded"
    )

    assert _audit_count(task_id) == audit_after_first
    counts_after_second = _counts()
    assert counts_after_first == counts_after_second, (counts_after_first, counts_after_second)

    with get_engine().begin() as conn:
        assert verify_task_audit_chain(conn, task_id=task_id)["ok"] is True