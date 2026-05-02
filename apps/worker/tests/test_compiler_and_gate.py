"""Worker-level tests for Phase 8.4 compiler + final answer gate.

All tests are rerun-safe (UUID/hash/marker unique per invocation) and self-
contained (no pytest.skip; helpers create storage_blobs + storage_objects
themselves, aligned with the real schema in 0002_storage.sql / 0003_documents.sql
and the patterns used by existing 8.3 tests).

Scenarios:
  - Test 1 (approved): document with a chunk containing digits, valid quote
    with valid quote_hash -> CVE-lite PASS -> ledger v2 verified_fact ->
    compiler builds a draft with >=1 span all backed by latest verified entry
    -> gate approves -> published_answers v1 with status='published'.

  - Test 2 (rejected, zero verified): same setup but evidence_span has a
    corrupt quote_hash -> CVE-lite FAIL -> ledger v2 unverifiable -> compiler
    finds zero verified_fact -> draft v1 with summary_text='' and zero spans
    -> gate rejects with reason_code='no_verified_claims', emits at least one
    coverage_gap_statements (kind='missing_evidence', severity='block',
    gap_key='no_verified_claims'). No published_answers.

  - Test 3 (idempotency): running the consumer twice on the same event must
    not duplicate any row.

  - Test 4 (redelivery terminale): after the first run completes (approved
    or rejected), a redelivery with a fresh idempotency_key must return
    'skipped_terminal' without inserting new rows or audit events.

  - Test 5 (rejected unverified spans): hand-built corrupt state where a span
    link points to a non-latest claim_ledger_entries (link to v1 candidate
    while latest is v2 unverifiable). Direct run_final_answer_gate() invocation
    -> rejected/unverified_spans_present + coverage_gap_statements
    (kind='unverified_claim', gap_key='span:<id>'). No published_answers.

  - Test 6a (compiling + approved gate report): force task_masters.status
    ='compiling' with a pre-existing final_gate_reports decision='approved' ->
    handle_task_created drives the task to 'published' via
    _finalize_from_existing_gate_report.

  - Test 6b (compiling + rejected gate report): same but with
    decision='rejected' -> task is driven to 'analyzed_partial'.

  - Test 7 (compiling without gate report): force task_masters.status
    ='compiling' with NO draft and NO gate report, but ledger v2 verified_fact
    pre-existing -> handle_task_created resumes compiler+gate idempotently and
    produces the missing report; the task ends up in 'published'.

Notes:
  - The Phase 8.4 gate computes "latest" via
        SELECT ... FROM claim_ledger_entries
        WHERE claim_logical_id = :lc
        ORDER BY version_no DESC LIMIT 1
    No claim_lineage row is required for the gate to function, so these tests
    do not insert into claim_lineage.

  - In any test that calls handle_task_created with the task in status
    'compiling', a document MUST be attached to the task: otherwise the
    consumer's _has_documents() returns False and skips _run_pipeline_with_docs.
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.consumers.task_created import handle_task_created
from app.db import get_engine
from app.services.final_answer_gate import run_final_answer_gate
from evidencefirst_shared.db.audit import verify_task_audit_chain


CONSUMER_NAME = "test_compiler_and_gate"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seeded_dev(conn: Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
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
                {"t": tenant_id, "n": f"compiler-gate-test-{uuid.uuid4()}"},
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
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create one storage_blobs + storage_objects + uploaded_documents +
    document_versions + document_chunks + evidence_spans, aligned with the
    real schema in 0002_storage.sql / 0003_documents.sql and the patterns
    used by existing 8.3 tests.

    The chunk text contains a unique sentence with digits so the 8.3 extractor
    will pick it up as a factual claim. The evidence_span.quote is exactly
    that sentence; quote_hash is sha256(quote) by default, or a corrupt hex
    string if `quote_hash_override` is provided (zero-verified scenario).

    Returns (document_id, document_chunk_id, quote).
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"Sales grew by 37 percent in {marker}."
    chunk_text = (
        f"Q3 report summary {marker}. {quote} "
        f"There were 3412 new customers in {marker}."
    )
    content_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
    size_bytes = len(chunk_text.encode("utf-8"))

    # 1) storage_blobs (global dedup; tenant_namespace_id NULL).
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
                    # Make the content_hash unique per invocation so the
                    # global UNIQUE (content_hash, hash_algorithm) WHERE
                    # tenant_namespace_id IS NULL never collides on a
                    # long-running dev DB.
                    "h": content_hash + "-" + uuid.uuid4().hex,
                    "sz": size_bytes,
                    "lp": f"/dev/null/{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )

    # 2) storage_objects (tenant-scoped pointer to the blob).
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

    # 3) uploaded_documents (tier in {user_provided, system_generated}).
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

    # 4) document_versions (version_kind='parsed').
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

    # 5) document_chunks.
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

    # 6) evidence_spans.
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

    return doc_id, chunk_id, quote


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
    """Common setup for tests that drive the consumer end-to-end: seed
    tenant/user, create project + task + doc + chunk + span, attach doc to
    task. Returns (tenant_id, project_id, user_id, task_id).
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        doc_id, _chunk_id, _quote = _create_doc_with_chunk_and_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
            quote_hash_override=quote_hash_override,
        )
        _attach_doc_to_task(conn, task_id=task_id, document_id=doc_id)
    return tenant_id, project_id, user_id, task_id


def _row_counts(conn: Connection, *, task_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
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


def _fetch_task_status(conn: Connection, task_id: uuid.UUID) -> str:
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Test 1 — approved scenario (full pipeline via consumer)
# ---------------------------------------------------------------------------
def test_approved_scenario_publishes_answer():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "published"

        draft = conn.execute(
            text(
                "SELECT id, summary_text FROM draft_final_answers "
                "WHERE task_id = :t AND version_no = 1"
            ),
            {"t": task_id},
        ).one()
        draft_id = uuid.UUID(str(draft[0]))
        summary_text = str(draft[1])
        assert summary_text != ""

        span_count = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_answer_spans "
                    "WHERE draft_final_answer_id = :d"
                ),
                {"d": draft_id},
            ).scalar_one()
        )
        assert span_count >= 1

        unlinked = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM final_answer_spans fas
                    LEFT JOIN final_answer_span_claim_links fascl
                      ON fascl.final_answer_span_id = fas.id
                     AND fascl.link_role = 'primary_support'
                    WHERE fas.draft_final_answer_id = :d
                      AND fascl.id IS NULL
                    """
                ),
                {"d": draft_id},
            ).scalar_one()
        )
        assert unlinked == 0

        report = conn.execute(
            text(
                "SELECT decision, reason_code FROM final_gate_reports WHERE task_id = :t"
            ),
            {"t": task_id},
        ).one()
        assert str(report[0]) == "approved"
        assert str(report[1]) == "all_spans_verified"

        pa = conn.execute(
            text(
                "SELECT version_no, content_hash, status "
                "FROM published_answers WHERE task_id = :t"
            ),
            {"t": task_id},
        ).one()
        assert int(pa[0]) == 1
        assert str(pa[1]) == hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
        assert str(pa[2]) == "published"

        gap_count = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM coverage_gap_statements "
                    "WHERE draft_final_answer_id = :d"
                ),
                {"d": draft_id},
            ).scalar_one()
        )
        assert gap_count == 0

    with get_engine().connect() as conn:
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
    assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 2 — rejected zero verified
# ---------------------------------------------------------------------------
def test_rejected_scenario_zero_verified_emits_missing_evidence_gap():
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

        draft = conn.execute(
            text(
                "SELECT id, summary_text FROM draft_final_answers "
                "WHERE task_id = :t AND version_no = 1"
            ),
            {"t": task_id},
        ).one()
        draft_id = uuid.UUID(str(draft[0]))
        assert str(draft[1]) == ""

        span_count = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_answer_spans "
                    "WHERE draft_final_answer_id = :d"
                ),
                {"d": draft_id},
            ).scalar_one()
        )
        assert span_count == 0

        report = conn.execute(
            text(
                "SELECT decision, reason_code FROM final_gate_reports WHERE task_id = :t"
            ),
            {"t": task_id},
        ).one()
        assert str(report[0]) == "rejected"
        assert str(report[1]) == "no_verified_claims"

        gap = conn.execute(
            text(
                """
                SELECT kind, severity, gap_key
                FROM coverage_gap_statements
                WHERE draft_final_answer_id = :d
                  AND kind = 'missing_evidence'
                  AND gap_key = 'no_verified_claims'
                """
            ),
            {"d": draft_id},
        ).one()
        assert str(gap[0]) == "missing_evidence"
        assert str(gap[1]) == "block"
        assert str(gap[2]) == "no_verified_claims"

        pa_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM published_answers WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert pa_count == 0

    with get_engine().connect() as conn:
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
    assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 3 — double delivery does not duplicate anything
# ---------------------------------------------------------------------------
def test_double_delivery_does_not_duplicate_anything():
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()
    event = _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id)

    rc1 = handle_task_created(event, consumer_name=CONSUMER_NAME)
    assert rc1 == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        counts_before = _row_counts(conn, task_id=task_id)

    rc2 = handle_task_created(event, consumer_name=CONSUMER_NAME)
    assert rc2 == "skipped_already_succeeded"

    with engine.connect() as conn:
        counts_after = _row_counts(conn, task_id=task_id)

    assert counts_after == counts_before


# ---------------------------------------------------------------------------
# Test 4 — redelivery with fresh idempotency_key after run is terminal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "quote_hash_override",
    [
        None,             # approved -> task ends 'published'
        "use_corrupt",    # rejected -> task ends 'analyzed_partial' + gate_report
    ],
)
def test_redelivery_with_existing_gate_report_is_terminal(quote_hash_override):
    qh_override = _unique_hash() if quote_hash_override == "use_corrupt" else None
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc(
        quote_hash_override=qh_override,
    )

    rc1 = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc1 == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        counts_before = _row_counts(conn, task_id=task_id)

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
        counts_after = _row_counts(conn, task_id=task_id)

    assert counts_after == counts_before


# ---------------------------------------------------------------------------
# Test 5 — rejected unverified spans (hand-built corrupt link state)
# ---------------------------------------------------------------------------
def test_rejected_unverified_spans_via_stale_link():
    """Build a draft whose only span links to a NON-LATEST claim_ledger_entries.

    Specifically:
      - logical_claim L
      - ledger v1 with state='candidate' (entry_v1)
      - ledger v2 with state='unverifiable' (entry_v2)
      - link from span -> entry_v1 (NOT latest)

    The Phase 8.4 gate computes "latest" via version_no DESC LIMIT 1, so it
    does not need a claim_lineage row. We deliberately omit claim_lineage.

    By the gate rule (linked_entry_id == latest_entry_id AND latest_entry_state
    == 'verified_fact'), the span is NOT verified-backed.

    Run the gate directly: expected rejected/unverified_spans_present with a
    coverage_gap_statements (kind='unverified_claim', gap_key='span:<id>').
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, _user_id, task_id = _seeded_dev(conn)

        lc_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO logical_claims
                            (id, tenant_id, project_id, task_id,
                             canonical_claim_text, canonical_claim_hash)
                        VALUES (:id, :t, :p, :tid, :ct, :ch)
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": tenant_id,
                        "p": project_id,
                        "tid": task_id,
                        "ct": f"canonical-{uuid.uuid4()}",
                        "ch": _unique_hash(),
                    },
                ).first()[0]
            )
        )

        entry_v1 = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO claim_ledger_entries
                            (id, claim_logical_id, version_no, state,
                             support_scope, user_provided_dependency,
                             transition_reason)
                        VALUES (:id, :lc, 1, 'candidate',
                                'supported_by_user_corpus_only',
                                'supported_by_user_corpus_only',
                                'extracted_by_mock_extractor')
                        RETURNING id
                        """
                    ),
                    {"id": uuid.uuid4(), "lc": lc_id},
                ).first()[0]
            )
        )

        conn.execute(
            text(
                """
                INSERT INTO claim_ledger_entries
                    (id, claim_logical_id, version_no, state,
                     support_scope, user_provided_dependency,
                     transition_reason)
                VALUES (:id, :lc, 2, 'unverifiable',
                        'supported_by_user_corpus_only',
                        'supported_by_user_corpus_only',
                        'cve_lite_quote_mismatch')
                """
            ),
            {"id": uuid.uuid4(), "lc": lc_id},
        )

        # Move the task into 'compiling' so the hand-built draft is consistent
        # with a "would-be-compiled" state. We do not need a document attached
        # because we invoke the gate directly, not handle_task_created.
        for s in ("analyzing", "analyzed_partial", "compiling"):
            conn.execute(
                text("UPDATE task_masters SET status = :s WHERE id = :id"),
                {"s": s, "id": task_id},
            )

        draft_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO draft_final_answers
                            (id, task_id, version_no,
                             compiler_name, compiler_version, summary_text)
                        VALUES (:id, :t, 1,
                                'mvp0_compiler_v1', '0.1.0', :st)
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "st": f"hand-built-{uuid.uuid4()}\n",
                    },
                ).first()[0]
            )
        )

        span_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO final_answer_spans
                            (id, draft_final_answer_id, span_index,
                             char_start, char_end, span_text, span_hash)
                        VALUES (:id, :d, 0,
                                0, 10, :st, :sh)
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "d": draft_id,
                        "st": "hand-built",
                        "sh": _unique_hash(),
                    },
                ).first()[0]
            )
        )

        conn.execute(
            text(
                """
                INSERT INTO final_answer_span_claim_links
                    (id, final_answer_span_id, claim_ledger_entry_id,
                     claim_logical_id, link_role)
                VALUES (:id, :sp, :ent, :lc, 'primary_support')
                """
            ),
            {
                "id": uuid.uuid4(),
                "sp": span_id,
                "ent": entry_v1,  # NOT LATEST
                "lc": lc_id,
            },
        )

    # Now invoke the gate directly.
    with engine.begin() as conn:
        outcome = run_final_answer_gate(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    assert outcome["decision"] == "rejected"
    assert outcome["reason_code"] == "unverified_spans_present"
    assert outcome["spans_total"] == 1
    assert outcome["spans_verified"] == 0
    assert outcome["spans_unverified"] == 1
    assert outcome["coverage_gaps_emitted"] >= 1

    with engine.connect() as conn:
        gap = conn.execute(
            text(
                """
                SELECT kind, severity, gap_key
                FROM coverage_gap_statements
                WHERE draft_final_answer_id = (
                    SELECT id FROM draft_final_answers
                    WHERE task_id = :t AND version_no = 1
                )
                  AND kind = 'unverified_claim'
                """
            ),
            {"t": task_id},
        ).one()
        assert str(gap[0]) == "unverified_claim"
        assert str(gap[1]) == "block"
        assert str(gap[2]).startswith("span:")

        pa_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM published_answers WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert pa_count == 0


# ---------------------------------------------------------------------------
# Test 6a / 6b — compiling + existing gate report -> finalize
# ---------------------------------------------------------------------------
def _seed_compiling_with_gate_report(decision: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Build the minimal coherent state to drive _finalize_from_existing_gate_report:
      - tenant + user + project + task
      - ONE document attached to the task (required by _has_documents in the
        consumer; otherwise the consumer skips _run_pipeline_with_docs)
      - task in status 'compiling' (no need to run the 8.3 stage; we only need
        the consumer to enter the 8.4 stage and find the gate report already
        present)
      - draft_final_answers v1
      - final_gate_reports with the requested decision (approved/rejected)
      - if approved: also a published_answers v1 (which the consumer will
        fetch to populate the audit payload)

    Returns (tenant_id, project_id, task_id).
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)

        # Attach a document so _has_documents(task) is True. Quote_hash
        # validity does not matter here: we are not running the 8.3 stage.
        doc_id, _chunk_id, _quote = _create_doc_with_chunk_and_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        _attach_doc_to_task(conn, task_id=task_id, document_id=doc_id)

        # Move the task into 'compiling' (no audit emitted; this is test setup,
        # not normal pipeline flow).
        for s in ("analyzing", "analyzed_partial", "compiling"):
            conn.execute(
                text("UPDATE task_masters SET status = :s WHERE id = :id"),
                {"s": s, "id": task_id},
            )

        summary_text = "preexisting-summary\n" if decision == "approved" else ""
        draft_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO draft_final_answers
                            (id, task_id, version_no,
                             compiler_name, compiler_version, summary_text)
                        VALUES (:id, :t, 1,
                                'mvp0_compiler_v1', '0.1.0', :st)
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "st": summary_text,
                    },
                ).first()[0]
            )
        )

        reason_code = (
            "all_spans_verified" if decision == "approved" else "no_verified_claims"
        )
        gate_report_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO final_gate_reports
                            (id, task_id, draft_final_answer_id,
                             decision, reason_code)
                        VALUES (:id, :t, :d, :dec, :rc)
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "d": draft_id,
                        "dec": decision,
                        "rc": reason_code,
                    },
                ).first()[0]
            )
        )

        if decision == "approved":
            content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
            conn.execute(
                text(
                    """
                    INSERT INTO published_answers
                        (id, task_id, draft_final_answer_id, final_gate_report_id,
                         version_no, content_hash, status)
                    VALUES (:id, :t, :d, :g, 1, :h, 'published')
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "d": draft_id,
                    "g": gate_report_id,
                    "h": content_hash,
                },
            )

    return tenant_id, project_id, task_id


def test_compiling_with_approved_gate_report_finalizes_to_published():
    tenant_id, project_id, task_id = _seed_compiling_with_gate_report("approved")

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "published"


def test_compiling_with_rejected_gate_report_finalizes_to_analyzed_partial():
    tenant_id, project_id, task_id = _seed_compiling_with_gate_report("rejected")

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id) == "analyzed_partial"


# ---------------------------------------------------------------------------
# Test 7 — compiling without gate report -> resume compiler+gate
# ---------------------------------------------------------------------------
def test_compiling_without_gate_report_resumes_compiler_and_gate():
    """Synthesize a minimal coherent state that the consumer must resume:

      - tenant + user + project + task
      - ONE document attached to the task (required by _has_documents)
      - logical_claim with ledger v1='candidate' and v2='verified_fact'
      - task in status='compiling'
      - NO draft_final_answers
      - NO final_gate_reports
      - NO published_answers

    handle_task_created must run compiler + gate idempotently, produce a
    final_gate_reports row, and drive the task to 'published' (because the
    only ledger latest is v2='verified_fact', so the single span built by the
    compiler will be verified-backed).
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)

        # Attach a document so _has_documents(task) is True.
        doc_id, _chunk_id, _quote = _create_doc_with_chunk_and_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        _attach_doc_to_task(conn, task_id=task_id, document_id=doc_id)

        # Pre-populate the ledger as if 8.3 had completed: v1 candidate then
        # v2 verified_fact for the same logical_claim. claim_lineage is not
        # required by the 8.4 gate (which uses version_no DESC).
        lc_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO logical_claims
                            (id, tenant_id, project_id, task_id,
                             canonical_claim_text, canonical_claim_hash)
                        VALUES (:id, :t, :p, :tid, :ct, :ch)
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": tenant_id,
                        "p": project_id,
                        "tid": task_id,
                        "ct": f"canonical-{uuid.uuid4()}",
                        "ch": _unique_hash(),
                    },
                ).first()[0]
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO claim_ledger_entries
                    (id, claim_logical_id, version_no, state,
                     support_scope, user_provided_dependency,
                     transition_reason)
                VALUES (:id, :lc, 1, 'candidate',
                        'supported_by_user_corpus_only',
                        'supported_by_user_corpus_only',
                        'extracted_by_mock_extractor')
                """
            ),
            {"id": uuid.uuid4(), "lc": lc_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO claim_ledger_entries
                    (id, claim_logical_id, version_no, state,
                     support_scope, user_provided_dependency,
                     transition_reason)
                VALUES (:id, :lc, 2, 'verified_fact',
                        'supported_by_user_corpus_only',
                        'supported_by_user_corpus_only',
                        'cve_lite_pass')
                """
            ),
            {"id": uuid.uuid4(), "lc": lc_id},
        )

        # Move the task to 'compiling' (no audit emitted; this is test setup).
        for s in ("analyzing", "analyzed_partial", "compiling"):
            conn.execute(
                text("UPDATE task_masters SET status = :s WHERE id = :id"),
                {"s": s, "id": task_id},
            )

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    with engine.connect() as conn:
        # The compiler must have produced exactly one draft v1, and the gate
        # exactly one report. Because the only verified_fact claim leads to a
        # verified-backed span, the gate approves and the task ends published.
        assert _fetch_task_status(conn, task_id) == "published"

        draft_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM draft_final_answers WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert draft_count == 1

        gate_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM final_gate_reports WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert gate_count == 1

        pa_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM published_answers WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert pa_count == 1
