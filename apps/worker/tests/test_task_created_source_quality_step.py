"""Integration tests for the Phase 8.7E source quality step inside
apps/worker/app/consumers/task_created.py.

Coverage map (4 scenarios required by the block prompt):

  1. test_pipeline_emits_task_source_quality_assessed_audit_after_analyzed_partial
  2. test_pipeline_populates_source_quality_for_claim_evidence_spans
  3. test_pipeline_resume_from_compiling_does_not_reemit_source_quality_audit
  4. test_pipeline_source_quality_failure_is_audited_but_does_not_block_8_4

Design notes:

  - These are DB-real tests. They drive the full task.created
    pipeline against the real Postgres so the 8.7E step is exercised
    in the context of all the other steps. No Redis, no API, no
    dispatcher: we call ``handle_task_created`` directly with a
    decoded event dict, mirroring the pattern used by
    apps/worker/tests/test_extractor_and_cve_lite.py.

  - Helpers in this file are LOCAL (per the block prompt: no imports
    from other test files).

  - Test 3 (Correction 3, Option A of the 8.7E micro-fix) constructs
    a real "resume from compiling" state: we force the task into
    status='compiling' with a pre-existing
    task.source_quality_assessed audit already on the chain (from a
    notional previous fresh run) and then call handle_task_created
    with a FRESH event_id + idempotency_key. The redelivery does NOT
    go through ``_run_8_3_extract_and_verify`` (the task is in
    'compiling', not 'analyzing'), so the 8.7E step is NOT re-entered
    and the audit count must stay at 1. The pre-existing audit row
    is constructed via ``audit_append`` so the chain remains valid.

  - Test 4 monkeypatches the symbol
    ``app.consumers.task_created.run_source_quality_assessment`` to
    a stub that raises a RuntimeError. The patched binding lives on
    the consumer module because task_created imports the function at
    module load time (``from ..services.source_quality_orchestrator
    import run_source_quality_assessment``), so the consumer's local
    name is what gets called.

  - Per the block prompt, tests must be DB-real and must skip cleanly
    when DATABASE_URL is missing or the DB is unreachable.
"""
from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.consumers import task_created as task_created_module
from app.consumers.task_created import handle_task_created
from app.db import get_engine
from evidencefirst_shared.db.audit import audit_append, verify_task_audit_chain


CONSUMER_NAME = "test_task_created_source_quality_step"


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; bring up the stack first.")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("DB unreachable; run `make up` and `make migrate`.")


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    row = conn.execute(
        text(
            """
            INSERT INTO tenants (name, slug, status)
            VALUES ('Dev','dev','active')
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
            text(
                "SELECT id FROM users WHERE tenant_id = :t AND email = 'dev@local'"
            ),
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
                {"t": tenant_id, "n": f"sq-step-test-{uuid.uuid4()}"},
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
) -> uuid.UUID:
    """Create a fully-formed document the extractor will recognize as
    factual (sentences with digits) so the 8.3 pipeline produces at
    least one raw_claim, claim_ledger v1 candidate, evidence_span,
    claim_evidence_link, and CVE-lite v2 verified_fact.

    Returns document_id.
    """
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
            "qh": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
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
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    event_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    return {
        "event_id": str(event_id) if event_id is not None else str(uuid.uuid4()),
        "event_type": "task.created",
        "tenant_id": str(tenant_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "idempotency_key": (
            idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        ),
    }


def _setup_task_with_doc() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        doc_id = _create_doc_with_chunk_and_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        _attach_doc_to_task(conn, task_id=task_id, document_id=doc_id)
    return tenant_id, project_id, user_id, task_id


# ---------------------------------------------------------------------------
# audit / DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_audit_event_types_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> list[tuple[int, str]]:
    """Return [(chain_seq, event_type), ...] ordered by chain_seq ASC."""
    rows = conn.execute(
        text(
            """
            SELECT chain_seq, event_type
            FROM audit_records
            WHERE chain_scope = 'task' AND scope_id = :t
            ORDER BY chain_seq ASC
            """
        ),
        {"t": task_id},
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _count_audit_event(
    conn: Connection, *, task_id: uuid.UUID, event_type: str
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM audit_records
                WHERE chain_scope = 'task'
                  AND scope_id    = :t
                  AND event_type  = :etype
                """
            ),
            {"t": task_id, "etype": event_type},
        ).scalar_one()
    )


def _fetch_audit_payload(
    conn: Connection, *, task_id: uuid.UUID, event_type: str
) -> dict:
    """Return the redacted_payload of the LAST audit row matching the
    given event_type for the task (highest chain_seq).
    """
    row = conn.execute(
        text(
            """
            SELECT redacted_payload
            FROM audit_records
            WHERE chain_scope = 'task'
              AND scope_id    = :t
              AND event_type  = :etype
            ORDER BY chain_seq DESC
            LIMIT 1
            """
        ),
        {"t": task_id, "etype": event_type},
    ).first()
    assert row is not None, f"no audit row with event_type={event_type!r}"
    payload = row[0]
    if isinstance(payload, str):
        import json as _json
        payload = _json.loads(payload)
    return dict(payload)


def _distinct_evidence_span_ids_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT cel.evidence_span_id
            FROM claim_evidence_links cel
            JOIN logical_claims lc ON lc.id = cel.claim_logical_id
            WHERE lc.task_id = :tid
              AND cel.evidence_span_id IS NOT NULL
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return {uuid.UUID(str(r[0])) for r in rows}


def _evidence_span_ids_with_sqa(
    conn: Connection, *, task_id: uuid.UUID
) -> set[uuid.UUID]:
    """Return the set of evidence_span_ids (linked to this task) for
    which a source_quality_assessments row exists.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT sqa.evidence_span_id
            FROM source_quality_assessments sqa
            WHERE sqa.evidence_span_id IN (
                SELECT DISTINCT cel.evidence_span_id
                FROM claim_evidence_links cel
                JOIN logical_claims lc ON lc.id = cel.claim_logical_id
                WHERE lc.task_id = :tid
                  AND cel.evidence_span_id IS NOT NULL
            )
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return {uuid.UUID(str(r[0])) for r in rows}


# ===========================================================================
# 1) pipeline emits task.source_quality_assessed after analyzed_partial,
#    before compiling; audit chain verifies
# ===========================================================================
def test_pipeline_emits_task_source_quality_assessed_audit_after_analyzed_partial():
    _skip_if_db_unreachable()
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)

    assert chain_ok["ok"] is True, chain_ok

    # Build the position map. Each event must be present exactly once on
    # this fresh-run path; if any of them is missing we get a KeyError
    # which fails the test clearly.
    positions = {etype: seq for seq, etype in chain}
    assert "task.analyzed_partial" in positions, chain
    assert "task.source_quality_assessed" in positions, chain
    assert "task.compiling" in positions, chain

    # Order invariant: source_quality_assessed sits strictly between
    # analyzed_partial and compiling on the task chain.
    assert (
        positions["task.analyzed_partial"]
        < positions["task.source_quality_assessed"]
        < positions["task.compiling"]
    ), chain

    # Exactly one source_quality_assessed audit (no duplicate, no resume
    # re-emission).
    with engine.connect() as conn:
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.source_quality_assessed",
        ) == 1

        # Payload shape: status='completed', counts is a dict, every
        # required counter is present and non-negative.
        payload = _fetch_audit_payload(
            conn, task_id=task_id, event_type="task.source_quality_assessed"
        )
        assert payload["status"] == "completed"
        assert payload["evaluated_target_kind"] == "evidence_span"
        counts = payload["counts"]
        assert isinstance(counts, dict)
        assert counts["status"] == "completed"
        assert counts["spans_total"] >= 1
        # The completion arithmetic must balance: every span ends up in
        # exactly one of the per-status buckets.
        bucket_total = (
            counts["assessed_count"]
            + counts["already_assessed_count"]
            + counts["not_found_count"]
            + counts["invalid_target_count"]
            + counts["error_count"]
        )
        assert bucket_total == counts["spans_total"]


# ===========================================================================
# 2) pipeline populates source_quality_assessments for every claim
#    evidence_span linked to the task
# ===========================================================================
def test_pipeline_populates_source_quality_for_claim_evidence_spans():
    _skip_if_db_unreachable()
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        expected_spans = _distinct_evidence_span_ids_for_task(
            conn, task_id=task_id
        )
        actual_spans = _evidence_span_ids_with_sqa(conn, task_id=task_id)

    assert len(expected_spans) >= 1, (
        "test corpus must produce at least one claim_evidence_link "
        "with a non-null evidence_span_id"
    )
    # Every linked span has a corresponding source_quality_assessments
    # row. (We compare as sets so an extra unrelated span is not a
    # failure — but every linked span MUST be present.)
    assert expected_spans.issubset(actual_spans), (
        f"spans missing from source_quality_assessments: "
        f"{expected_spans - actual_spans}"
    )


# ===========================================================================
# 3) resume from 'compiling' does NOT re-emit task.source_quality_assessed
# ===========================================================================
def test_pipeline_resume_from_compiling_does_not_reemit_source_quality_audit():
    """Genuine resume from 'compiling' (Correction 3, Option A of the
    8.7E micro-fix).

    Setup: the task is in status='compiling' WITHOUT a
    final_gate_report yet, simulating a previous fresh run that
    crashed between the 'task.compiling' audit and the compiler's
    INSERT of the draft. The audit chain for the task already
    contains the 8.3 events (task.analyzing through
    task.analyzed_partial), the 8.7E event task.source_quality_assessed,
    and task.compiling — exactly as the fresh-run path would have
    emitted them.

    Action: deliver a NEW event for this task — fresh event_id, fresh
    idempotency_key, so the consumer enters
    ``handle_task_created`` past the EPR short-circuit and into the
    pipeline orchestrator.

    Expected: the orchestrator's ``_run_pipeline_with_docs`` sees the
    task in status='compiling' (not 'analyzing'), skips
    ``_run_8_3_extract_and_verify`` entirely, and falls through to
    ``_run_8_4_compile_and_gate`` to resume the 8.4 stage. The 8.7E
    step lives inside ``_run_8_3_extract_and_verify``, so the
    source_quality audit count MUST stay at 1. The pipeline must
    complete successfully and the audit chain must verify.
    """
    _skip_if_db_unreachable()
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    engine = get_engine()

    # Build a coherent "previous fresh run crashed in 'compiling'"
    # state: emit the audit chain up to 'task.compiling' manually,
    # set task_masters.status to 'compiling', and leave NO
    # final_gate_report row.
    with engine.begin() as conn:
        for event_type, payload in (
            ("task.analyzing", {"transition": "created->analyzing"}),
            ("task.docs_loaded", {"counts": {}}),
            ("task.claims_extracted", {}),
            ("task.claims_classified", {}),
            ("task.claims_ledger_initialized", {}),
            ("task.cve_lite_started", {"checker": "mvp0_cve_lite_v1"}),
            ("task.cve_lite_completed", {}),
            (
                "task.analyzed_partial",
                {"transition": "analyzing->analyzed_partial"},
            ),
            (
                "task.source_quality_assessed",
                {
                    "evaluated_target_kind": "evidence_span",
                    "status": "completed",
                    "counts": {
                        "status": "completed",
                        "spans_total": 0,
                        "assessed_count": 0,
                        "already_assessed_count": 0,
                        "not_found_count": 0,
                        "invalid_target_count": 0,
                        "error_count": 0,
                    },
                },
            ),
            ("task.compiling", {"transition": "analyzed_partial->compiling"}),
        ):
            audit_append(
                conn,
                chain_scope="task",
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
                session_id=None,
                event_type=event_type,
                actor_type="job",
                actor_id=CONSUMER_NAME,
                redacted_payload=payload,
                related_entity_type="task_masters",
                related_entity_id=task_id,
            )
        conn.execute(
            text(
                """
                UPDATE task_masters SET status = 'compiling'
                WHERE id = :t
                """
            ),
            {"t": task_id},
        )

    # Sanity: pre-condition state is as expected.
    with engine.connect() as conn:
        status_before = str(
            conn.execute(
                text("SELECT status FROM task_masters WHERE id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        sqa_audit_before = _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.source_quality_assessed",
        )
        gate_report_count_before = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_gate_reports WHERE task_id = :t"
                ),
                {"t": task_id},
            ).scalar_one()
        )
    assert status_before == "compiling"
    assert sqa_audit_before == 1
    assert gate_report_count_before == 0

    # Resume delivery: fresh event_id + fresh idempotency_key so the
    # consumer does NOT short-circuit at the EPR layer.
    rc = handle_task_created(
        _make_event(
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            event_id=uuid.uuid4(),
            idempotency_key=str(uuid.uuid4()),
        ),
        consumer_name=CONSUMER_NAME,
    )
    # The resume path runs the 8.4 stage end-to-end; the consumer
    # classifies it as 'processed'.
    assert rc == "processed"

    with engine.connect() as conn:
        # Critical invariant: source_quality audit was NOT re-emitted.
        sqa_audit_after = _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.source_quality_assessed",
        )
        # 8.4 actually resumed: a final_gate_report now exists.
        gate_report_count_after = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM final_gate_reports WHERE task_id = :t"
                ),
                {"t": task_id},
            ).scalar_one()
        )
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)

    assert sqa_audit_after == 1, (
        "task.source_quality_assessed must NOT be re-emitted on resume "
        "from 'compiling'"
    )
    assert gate_report_count_after == 1, (
        "8.4 must have resumed and produced a final_gate_report"
    )
    assert chain_ok["ok"] is True


# ===========================================================================
# 4) source quality failure is audited but does NOT block 8.4
# ===========================================================================
def test_pipeline_source_quality_failure_is_audited_but_does_not_block_8_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()
    tenant_id, project_id, _user_id, task_id = _setup_task_with_doc()

    # Patch the symbol on the consumer module (NOT on the orchestrator
    # module), because task_created.py imports
    # run_source_quality_assessment at module load and binds the name
    # locally.
    def _raise_runtime(*_args, **_kwargs):
        raise RuntimeError("simulated source quality failure")

    monkeypatch.setattr(
        task_created_module,
        "run_source_quality_assessment",
        _raise_runtime,
    )

    rc = handle_task_created(
        _make_event(tenant_id=tenant_id, project_id=project_id, task_id=task_id),
        consumer_name=CONSUMER_NAME,
    )
    # The pipeline MUST succeed end-to-end despite the 8.7E failure.
    assert rc == "processed"

    engine = get_engine()
    with engine.connect() as conn:
        # The aggregated audit was still emitted, with status='failed'
        # and the exception class name surfaced in the payload (no
        # stack trace).
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.source_quality_assessed",
        ) == 1
        payload = _fetch_audit_payload(
            conn, task_id=task_id, event_type="task.source_quality_assessed"
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "RuntimeError"
        assert payload["evaluated_target_kind"] == "evidence_span"
        # The failure counts dict mirrors the canonical shape with
        # error_count=1 and zero on every other per-status bucket.
        counts = payload["counts"]
        assert counts["status"] == "failed"
        assert counts["error_count"] == 1
        assert counts["assessed_count"] == 0
        assert counts["already_assessed_count"] == 0
        assert counts["not_found_count"] == 0
        assert counts["invalid_target_count"] == 0

        # CRITICAL: the SAVEPOINT must have rolled back any partial
        # source_quality_assessments INSERT from this call. With the
        # orchestrator stubbed to raise BEFORE any insert, the table
        # remains empty for this task's spans.
        spans_with_sqa = _evidence_span_ids_with_sqa(conn, task_id=task_id)
        assert spans_with_sqa == set(), (
            f"savepoint rollback failed: source_quality_assessments "
            f"leaked rows for spans {spans_with_sqa}"
        )

        # The 8.4 pipeline ran to completion and the audit chain
        # verifies end-to-end.
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True

        # Sanity: a final_gate_report row exists (the weaker
        # invariant required by the prompt — "pipeline at least does
        # not fail because of the 8.7E error").
        gate_count = int(
            conn.execute(
                text("SELECT COUNT(*) FROM final_gate_reports WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
        assert gate_count == 1, (
            "8.4 pipeline must have produced a final_gate_report even "
            "when 8.7E fails"
        )
