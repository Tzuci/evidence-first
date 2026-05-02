"""Worker tests for the extractor + CVE-lite pipeline (Phase 8.3 invariants
re-validated under the Phase 8.4 single-consumer pipeline).

Coverage:
  - PASS scenario: extractor produces ledger v1 'candidate', CVE-lite verifies
    quote/hash and inserts ledger v2 'verified_fact', verification_records
    with outcome='pass'. Under 8.4 the consumer continues into compiler+gate,
    so the task ends in status='published'. The Phase 8.3 ledger invariants
    are explicitly re-asserted.
  - FAIL scenario: corrupt evidence_span quote_hash. Extractor produces v1
    'candidate', CVE-lite v2 'unverifiable' with outcome='fail'. Under 8.4 the
    consumer continues into compiler+gate, the gate rejects with
    reason_code='no_verified_claims', the task ends in status='analyzed_partial'
    with a final_gate_reports row present (terminal redelivery state).
  - Idempotency: a double delivery of the same event must not duplicate any
    8.3 row (raw_claims, classified_claims, logical_claims,
    claim_ledger_entries, claim_lineage, claim_evidence_links,
    verification_records) nor any 8.4 row (agent_runs, draft_final_answers,
    final_answer_spans, final_answer_span_claim_links, final_gate_reports,
    published_answers, coverage_gap_statements).

All tests rerun-safe via uuid.uuid4()-derived identifiers and content hashes.
"""
from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.consumers.task_created import handle_task_created
from app.db import get_engine


CONSUMER_NAME = "test_extractor_and_cve_lite"


# ---------------------------------------------------------------------------
# helpers
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
                {"t": tenant_id, "n": f"extractor-cve-lite-{uuid.uuid4()}"},
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


def _fetch_task_status(conn: Connection, task_id: uuid.UUID) -> str:
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


def _row_counts_full(conn: Connection, *, task_id: uuid.UUID) -> dict[str, int]:
    """Snapshot of row counts across both 8.3 and 8.4 tables for the task."""
    counts: dict[str, int] = {}

    counts["raw_claims"] = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM raw_claims rc
                JOIN logical_claims lc ON lc.id = rc.logical_claim_id
                WHERE lc.task_id = :t
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )
    counts["classified_claims"] = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM classified_claims cc
                JOIN logical_claims lc ON lc.id = cc.logical_claim_id
                WHERE lc.task_id = :t
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )
    counts["logical_claims"] = int(
        conn.execute(
            text("SELECT COUNT(*) FROM logical_claims WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
    )
    counts["claim_ledger_entries"] = int(
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
    )
    counts["claim_lineage"] = int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM claim_lineage cln
                JOIN claim_ledger_entries cle ON cle.id = cln.parent_entry_id
                JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                WHERE lc.task_id = :t
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )
    counts["claim_evidence_links"] = int(
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
    )
    counts["verification_records"] = int(
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
    )

    counts["agent_runs"] = int(
        conn.execute(
            text("SELECT COUNT(*) FROM agent_runs WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
    )
    counts["draft_final_answers"] = int(
        conn.execute(
            text("SELECT COUNT(*) FROM draft_final_answers WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
    )
    counts["final_gate_reports"] = int(
        conn.execute(
            text("SELECT COUNT(*) FROM final_gate_reports WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
    )
    counts["published_answers"] = int(
        conn.execute(
            text("SELECT COUNT(*) FROM published_answers WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
    )

    row = conn.execute(
        text(
            "SELECT id FROM draft_final_answers WHERE task_id = :t "
            "ORDER BY version_no DESC LIMIT 1"
        ),
        {"t": task_id},
    ).first()
    draft_id_local = uuid.UUID(str(row[0])) if row is not None else None

    if draft_id_local is None:
        counts["final_answer_spans"] = 0
        counts["final_answer_span_claim_links"] = 0
        counts["coverage_gap_statements"] = 0
    else:
        counts["final_answer_spans"] = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_answer_spans "
                    "WHERE draft_final_answer_id = :d"
                ),
                {"d": draft_id_local},
            ).scalar_one()
        )
        counts["final_answer_span_claim_links"] = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM final_answer_span_claim_links fascl
                    JOIN final_answer_spans fas ON fas.id = fascl.final_answer_span_id
                    WHERE fas.draft_final_answer_id = :d
                    """
                ),
                {"d": draft_id_local},
            ).scalar_one()
        )
        counts["coverage_gap_statements"] = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM coverage_gap_statements "
                    "WHERE draft_final_answer_id = :d"
                ),
                {"d": draft_id_local},
            ).scalar_one()
        )

    counts["audit_records"] = int(
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
    return counts


# ---------------------------------------------------------------------------
# Test 1 — extractor produces v1 + CVE-lite verifies PASS; under 8.4 task ends 'published'
# ---------------------------------------------------------------------------
def test_extractor_produces_v1_and_cve_lite_verifies_pass():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        # Under 8.4 the consumer continues past analyzed_partial.
        assert _fetch_task_status(conn, task_id) == "published"

        # Phase 8.3 ledger invariants (PASS path):
        # at least one logical_claim, at least one v1 'candidate' and one v2
        # 'verified_fact', and at least one verification_records 'pass'.
        lc_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM logical_claims WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert lc_count >= 1

        v1_candidate = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM claim_ledger_entries cle
                    JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                    WHERE lc.task_id = :t
                      AND cle.version_no = 1
                      AND cle.state = 'candidate'
                    """
                ),
                {"t": task_id},
            ).scalar_one()
        )
        assert v1_candidate >= 1

        v2_verified = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM claim_ledger_entries cle
                    JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                    WHERE lc.task_id = :t
                      AND cle.version_no = 2
                      AND cle.state = 'verified_fact'
                    """
                ),
                {"t": task_id},
            ).scalar_one()
        )
        assert v2_verified >= 1

        vr_pass = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM verification_records vr
                    JOIN logical_claims lc ON lc.id = vr.claim_logical_id
                    WHERE lc.task_id = :t
                      AND vr.outcome = 'pass'
                    """
                ),
                {"t": task_id},
            ).scalar_one()
        )
        assert vr_pass >= 1

        # 8.4 invariants (approved): one gate report 'approved',
        # one published_answers 'published'.
        gate = conn.execute(
            text(
                "SELECT decision, reason_code FROM final_gate_reports WHERE task_id = :t"
            ),
            {"t": task_id},
        ).one()
        assert str(gate[0]) == "approved"
        assert str(gate[1]) == "all_spans_verified"

        pa_status = conn.execute(
            text("SELECT status FROM published_answers WHERE task_id = :t"),
            {"t": task_id},
        ).scalar_one()
        assert str(pa_status) == "published"


# ---------------------------------------------------------------------------
# Test 2 — corrupt span: CVE-lite FAIL; under 8.4 gate rejects, task ends 'analyzed_partial'
# ---------------------------------------------------------------------------
def test_extractor_produces_v1_and_cve_lite_marks_unverifiable_on_corrupt_span():
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
        # Under 8.4 with zero verified facts, the gate rejects and the task
        # is brought back to analyzed_partial with a final_gate_report.
        assert _fetch_task_status(conn, task_id) == "analyzed_partial"

        v2_unverifiable = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM claim_ledger_entries cle
                    JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                    WHERE lc.task_id = :t
                      AND cle.version_no = 2
                      AND cle.state = 'unverifiable'
                    """
                ),
                {"t": task_id},
            ).scalar_one()
        )
        assert v2_unverifiable >= 1

        vr_fail = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM verification_records vr
                    JOIN logical_claims lc ON lc.id = vr.claim_logical_id
                    WHERE lc.task_id = :t
                      AND vr.outcome = 'fail'
                    """
                ),
                {"t": task_id},
            ).scalar_one()
        )
        assert vr_fail >= 1

        gate = conn.execute(
            text(
                "SELECT decision, reason_code FROM final_gate_reports WHERE task_id = :t"
            ),
            {"t": task_id},
        ).one()
        assert str(gate[0]) == "rejected"
        assert str(gate[1]) == "no_verified_claims"

        pa_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM published_answers WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert pa_count == 0


# ---------------------------------------------------------------------------
# Test 3 — double delivery does not duplicate any 8.3 or 8.4 row
# ---------------------------------------------------------------------------
def test_double_delivery_does_not_duplicate_anything():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()
    event = _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id)

    rc1 = handle_task_created(event, consumer_name=CONSUMER_NAME)
    assert rc1 == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        counts_before = _row_counts_full(conn, task_id=task_id)

    rc2 = handle_task_created(event, consumer_name=CONSUMER_NAME)
    assert rc2 == "skipped_already_succeeded"

    with engine.connect() as conn:
        counts_after = _row_counts_full(conn, task_id=task_id)

    assert counts_after == counts_before
