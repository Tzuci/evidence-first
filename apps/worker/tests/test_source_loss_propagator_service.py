"""Worker-level tests for apps/worker/app/services/source_loss_propagator.py
(Phase 8.5 — Block 2B-1).

Coverage:
  1. propagate_source_loss marks an impacted claim as 'unverifiable' v2,
     emits the supersedes lineage, records the propagation row and the
     task-scoped audit event. verify_task_audit_chain ok=True.
  2. propagate_source_loss is idempotent under redelivery on the same
     source_loss_event_id: ledger entries / lineage / propagation records /
     audit events are not duplicated. The second call must not raise.
  3. propagate_source_loss with a source_loss_events row that has no
     claim_evidence_links pointing at it returns status='no_claims_impacted'
     and records the dedicated propagation row.
  4. propagate_source_loss correctly records the impact on a published_answer
     (status='published') without ever modifying that published_answer:
     status stays 'published', withdrawn_at stays NULL. The
     'source_loss.propagated_to_published_answer' audit event is emitted.
  5. propagate_source_loss records 'no_active_published_answers_impacted'
     when at least one claim is impacted but no published_answer in
     status='published' depends on it.
  6. propagate_source_loss returns status='not_found' and does not write
     anything when the source_loss_event_id is unknown.

Design notes:
  - This file lives under apps/worker/tests/. The Python package `app`
    resolves to apps/worker/app, so `from app.db import get_engine` and
    `from app.services.source_loss_propagator import propagate_source_loss`
    work without ambiguity.
  - All helpers are LOCAL to this file (no imports from other test files
    nor from any test root). All identifiers / hashes / span texts are
    unique per invocation (rerun-safe).
  - The service requires an active SQLAlchemy Connection inside an explicit
    transaction. We always wrap calls in `with engine.begin() as conn:`.
  - The service performs no commit/rollback and never opens its own
    connection; the explicit transaction context manager is what flushes
    the writes.
  - verify_task_audit_chain expects a Connection, not an Engine. We wrap
    the call in a `with engine.connect() as conn:` block.
  - We never call apply_withdrawal. We never INSERT into
    published_answer_lifecycle_events. We never UPDATE published_answers.status.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.services.source_loss_propagator import propagate_source_loss
from evidencefirst_shared.db.audit import verify_task_audit_chain


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


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
                {"t": tenant_id, "n": f"source-loss-prop-test-{uuid.uuid4()}"},
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


# ---------------------------------------------------------------------------
# document + evidence_span seeding (aligned with 0002/0003 schema)
# ---------------------------------------------------------------------------
def _create_document_evidence_span(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    created_by: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create the full storage chain ending in an evidence_spans row.

    Order of inserts (to honor every FK and the storage_blobs unique partial
    index sb_global_uq):
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans

    Returns (document_id, document_version_id, document_chunk_id,
    evidence_span_id).
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source loss propagator test marker {marker}. "
        f"This sentence contains the digit 7 and a {quote}."
    )
    content_hash_payload = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
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
                    # Make the content_hash unique per invocation so the
                    # global UNIQUE (content_hash, hash_algorithm) WHERE
                    # tenant_namespace_id IS NULL never collides on a
                    # long-running dev DB.
                    "h": content_hash_payload + "-" + uuid.uuid4().hex,
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

    document_id = uuid.UUID(
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
                    "h": content_hash_payload,
                    "sz": size_bytes,
                    "u": created_by,
                },
            ).first()[0]
        )
    )

    document_version_id = uuid.UUID(
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
                    "did": document_id,
                    "so": storage_object_id,
                    "it": chunk_text,
                    "th": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    document_chunk_id = uuid.UUID(
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
                    "dv": document_version_id,
                    "ce": len(chunk_text),
                    "it": chunk_text,
                    "th": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    char_start = chunk_text.index(quote)
    char_end = char_start + len(quote)
    evidence_span_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO evidence_spans
                        (id, document_chunk_id, char_start, char_end,
                         quote, quote_hash)
                    VALUES
                        (:id, :cid, :cs, :ce, :q, :qh)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "cid": document_chunk_id,
                    "cs": char_start,
                    "ce": char_end,
                    "q": quote,
                    "qh": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )

    return document_id, document_version_id, document_chunk_id, evidence_span_id


# ---------------------------------------------------------------------------
# claim seeding
# ---------------------------------------------------------------------------
def _create_logical_claim_with_verified_entry(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one logical_claims row plus a v1 ledger entry in state
    'verified_fact'. This is the typical 'head' state of a published claim,
    which is what the propagator expects to supersede with a new
    'unverifiable' v2 entry.

    Returns (claim_logical_id, claim_ledger_entry_id).
    """
    claim_logical_id = uuid.UUID(
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
                    "ch": _unique_hex(),
                },
            ).first()[0]
        )
    )

    claim_ledger_entry_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO claim_ledger_entries
                        (id, claim_logical_id, version_no, state,
                         support_scope, user_provided_dependency,
                         transition_reason)
                    VALUES (:id, :lc, 1, 'verified_fact',
                            'supported_by_user_corpus_only',
                            'supported_by_user_corpus_only',
                            'seeded_for_test')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "lc": claim_logical_id},
            ).first()[0]
        )
    )
    return claim_logical_id, claim_ledger_entry_id


def _link_claim_to_evidence_span(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> uuid.UUID:
    """Insert a claim_evidence_links row connecting the given (logical, entry)
    pair to the given evidence_span. Honors the cel_origin_xor CHECK by
    setting evidence_span_id and leaving retrieved_source_span_id NULL.
    """
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO claim_evidence_links
                        (id, claim_logical_id, claim_ledger_entry_id,
                         evidence_span_id, retrieved_source_span_id, link_role)
                    VALUES (:id, :lc, :le, :es, NULL, 'primary_support')
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "lc": claim_logical_id,
                    "le": claim_ledger_entry_id,
                    "es": evidence_span_id,
                },
            ).first()[0]
        )
    )


def _create_source_loss_event(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    document_chunk_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one source_loss_events row keyed on the given evidence_span_id.

    The document_* columns are reporting context only. We pass them when
    available so the row is well-formed end-to-end.
    """
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO source_loss_events
                        (id, tenant_id, project_id, task_id,
                         evidence_span_id, document_chunk_id,
                         document_version_id, document_id,
                         loss_kind, loss_reason, detected_by,
                         event_payload, idempotency_key)
                    VALUES
                        (:id, :t, :p, :tid,
                         :es, :dc, :dv, :di,
                         'source_deleted', 'unit-test loss reason',
                         'pytest', '{}'::jsonb, :ik)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "p": project_id,
                    "tid": task_id,
                    "es": evidence_span_id,
                    "dc": document_chunk_id,
                    "dv": document_version_id,
                    "di": document_id,
                    "ik": _unique_hex(),
                },
            ).first()[0]
        )
    )


# ---------------------------------------------------------------------------
# published_answer seeding
# ---------------------------------------------------------------------------
def _create_published_answer_for_claim(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    status: str = "published",
) -> uuid.UUID:
    """Build the minimal 8.4 chain so that
    _select_impacted_published_answers can find this published_answer:

      draft_final_answers v1
        -> final_answer_spans (1)
          -> final_answer_span_claim_links (1) referencing the given
             (claim_logical_id, claim_ledger_entry_id)
      final_gate_reports (decision='approved')
      published_answers v1 (status as requested; default 'published')

    Honors the composite FKs in 0005 (draft + gate consistency on task_id).
    Status defaults to 'published'; pass 'withdrawn' or 'superseded' only in
    test setup. The propagator MUST NOT change this status afterwards.

    Returns the published_answer_id.
    """
    summary_text = f"published-{uuid.uuid4()}\n"

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
                {"id": uuid.uuid4(), "t": task_id, "st": summary_text},
            ).first()[0]
        )
    )

    span_text = f"span-{uuid.uuid4()}"
    span_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_answer_spans
                        (id, draft_final_answer_id, span_index,
                         char_start, char_end, span_text, span_hash)
                    VALUES (:id, :d, 0,
                            0, :ce, :st, :sh)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "d": draft_id,
                    "ce": len(span_text),
                    "st": span_text,
                    "sh": hashlib.sha256(span_text.encode("utf-8")).hexdigest(),
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
            "ent": claim_ledger_entry_id,
            "lc": claim_logical_id,
        },
    )

    gate_report_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_gate_reports
                        (id, task_id, draft_final_answer_id,
                         decision, reason_code)
                    VALUES (:id, :t, :d, 'approved', 'all_spans_verified')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "t": task_id, "d": draft_id},
            ).first()[0]
        )
    )

    content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()

    if status == "published":
        pa_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO published_answers
                            (id, task_id, draft_final_answer_id,
                             final_gate_report_id, version_no, content_hash, status)
                        VALUES (:id, :t, :d, :g, 1, :h, 'published')
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "d": draft_id,
                        "g": gate_report_id,
                        "h": content_hash,
                    },
                ).first()[0]
            )
        )
    elif status == "withdrawn":
        pa_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO published_answers
                            (id, task_id, draft_final_answer_id,
                             final_gate_report_id, version_no, content_hash,
                             status, withdrawn_at)
                        VALUES (:id, :t, :d, :g, 1, :h, 'withdrawn', NOW())
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "d": draft_id,
                        "g": gate_report_id,
                        "h": content_hash,
                    },
                ).first()[0]
            )
        )
    elif status == "superseded":
        pa_id = uuid.UUID(
            str(
                conn.execute(
                    text(
                        """
                        INSERT INTO published_answers
                            (id, task_id, draft_final_answer_id,
                             final_gate_report_id, version_no, content_hash,
                             status, superseded_at)
                        VALUES (:id, :t, :d, :g, 1, :h, 'superseded', NOW())
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "t": task_id,
                        "d": draft_id,
                        "g": gate_report_id,
                        "h": content_hash,
                    },
                ).first()[0]
            )
        )
    else:
        raise ValueError(f"unsupported status {status!r}")

    return pa_id


# ---------------------------------------------------------------------------
# count / fetch helpers
# ---------------------------------------------------------------------------
def _count_ledger_entries_for_claim(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM claim_ledger_entries WHERE claim_logical_id = :lc"
            ),
            {"lc": claim_logical_id},
        ).scalar_one()
    )


def _fetch_latest_ledger_entry(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> dict[str, Any]:
    row = conn.execute(
        text(
            """
            SELECT id, version_no, state, support_scope,
                   user_provided_dependency, transition_reason
            FROM claim_ledger_entries
            WHERE claim_logical_id = :lc
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"lc": claim_logical_id},
    ).one()
    return dict(row._mapping)


def _count_lineage_for_pair(
    conn: Connection,
    *,
    parent_entry_id: uuid.UUID,
    child_entry_id: uuid.UUID,
    relation_kind: str = "supersedes",
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM claim_lineage
                WHERE parent_entry_id = :p
                  AND child_entry_id  = :c
                  AND relation_kind   = :rk
                """
            ),
            {"p": parent_entry_id, "c": child_entry_id, "rk": relation_kind},
        ).scalar_one()
    )


def _count_lineage_for_child(
    conn: Connection,
    *,
    child_entry_id: uuid.UUID,
    relation_kind: str = "supersedes",
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM claim_lineage
                WHERE child_entry_id = :c AND relation_kind = :rk
                """
            ),
            {"c": child_entry_id, "rk": relation_kind},
        ).scalar_one()
    )


def _count_propagation_records(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    propagation_kind: str,
    status: str | None = None,
) -> int:
    if status is None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_propagation_records
                    WHERE source_loss_event_id = :sle
                      AND propagation_kind = :pk
                    """
                ),
                {"sle": source_loss_event_id, "pk": propagation_kind},
            ).scalar_one()
        )
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM source_loss_propagation_records
                WHERE source_loss_event_id = :sle
                  AND propagation_kind     = :pk
                  AND status               = :st
                """
            ),
            {"sle": source_loss_event_id, "pk": propagation_kind, "st": status},
        ).scalar_one()
    )


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


def _fetch_published_answer_status(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> dict[str, Any]:
    row = conn.execute(
        text(
            """
            SELECT status, withdrawn_at, superseded_at, superseded_by_id
            FROM published_answers
            WHERE id = :pid
            """
        ),
        {"pid": published_answer_id},
    ).one()
    return dict(row._mapping)


# ---------------------------------------------------------------------------
# Test 1 — propagate_source_loss marks an impacted claim as unverifiable
# ---------------------------------------------------------------------------
def test_propagate_source_loss_marks_claim_unverifiable():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        document_id, document_version_id, document_chunk_id, evidence_span_id = (
            _create_document_evidence_span(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                created_by=user_id,
            )
        )
        claim_logical_id, claim_ledger_entry_id = (
            _create_logical_claim_with_verified_entry(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
            )
        )
        _link_claim_to_evidence_span(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=claim_ledger_entry_id,
            evidence_span_id=evidence_span_id,
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
            document_chunk_id=document_chunk_id,
            document_version_id=document_version_id,
            document_id=document_id,
        )

    with engine.begin() as conn:
        result = propagate_source_loss(conn, source_loss_event_id=sle_id)

    assert result["status"] == "propagated"
    assert result["impacted_claim_count"] == 1
    assert result["created_unverifiable_count"] == 1
    assert result["skipped_claim_count"] == 0
    assert result["failed_claim_count"] == 0

    with engine.connect() as conn:
        # Two ledger entries now exist for the claim: v1 verified_fact and
        # v2 unverifiable.
        assert _count_ledger_entries_for_claim(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        latest = _fetch_latest_ledger_entry(
            conn, claim_logical_id=claim_logical_id
        )
        assert int(latest["version_no"]) == 2
        assert str(latest["state"]) == "unverifiable"
        assert str(latest["support_scope"]) == "unsupported"
        assert str(latest["user_provided_dependency"]) == "unsupported"
        assert str(latest["transition_reason"]) == "source_lost"

        new_entry_id = uuid.UUID(str(latest["id"]))

        # Lineage edge v1 -> v2 ('supersedes') exists exactly once.
        assert _count_lineage_for_pair(
            conn,
            parent_entry_id=claim_ledger_entry_id,
            child_entry_id=new_entry_id,
            relation_kind="supersedes",
        ) == 1

        # Propagation record for claim_marked_unverifiable / recorded.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
        ) == 1

        # Task-scoped audit event for the claim transition.
        assert _count_audit_event(
            conn, task_id=task_id, event_type="source_loss.propagated_to_claim"
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 2 — propagate_source_loss is idempotent under redelivery
# ---------------------------------------------------------------------------
def test_propagate_source_loss_is_idempotent():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        document_id, document_version_id, document_chunk_id, evidence_span_id = (
            _create_document_evidence_span(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                created_by=user_id,
            )
        )
        claim_logical_id, claim_ledger_entry_id = (
            _create_logical_claim_with_verified_entry(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
            )
        )
        _link_claim_to_evidence_span(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=claim_ledger_entry_id,
            evidence_span_id=evidence_span_id,
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
            document_chunk_id=document_chunk_id,
            document_version_id=document_version_id,
            document_id=document_id,
        )

    # First call: real propagation.
    with engine.begin() as conn:
        result_1 = propagate_source_loss(conn, source_loss_event_id=sle_id)
    assert result_1["status"] == "propagated"
    assert result_1["created_unverifiable_count"] == 1

    # Second call: same source_loss_event_id. The propagator must not raise,
    # and must NOT duplicate any ledger entry, lineage edge, propagation
    # record (recorded + skipped combined remain at 1, because the partial
    # unique index covers both 'recorded' and 'skipped'), or audit event.
    with engine.begin() as conn:
        result_2 = propagate_source_loss(conn, source_loss_event_id=sle_id)
    assert result_2["status"] == "propagated"

    with engine.connect() as conn:
        # Ledger entries: still 2 (v1 verified_fact + v2 unverifiable).
        assert _count_ledger_entries_for_claim(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        latest = _fetch_latest_ledger_entry(
            conn, claim_logical_id=claim_logical_id
        )
        new_entry_id = uuid.UUID(str(latest["id"]))

        # Lineage: still 1 (parent=v1, child=v2, supersedes).
        assert _count_lineage_for_child(
            conn, child_entry_id=new_entry_id, relation_kind="supersedes"
        ) == 1

        # Propagation record: total recorded + skipped for
        # claim_marked_unverifiable on this source_loss_event remains 1.
        recorded = _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
        )
        skipped = _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="skipped",
        )
        assert recorded + skipped == 1

        # Audit event: emitted only on the first call.
        assert _count_audit_event(
            conn, task_id=task_id, event_type="source_loss.propagated_to_claim"
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 3 — no claims impacted
# ---------------------------------------------------------------------------
def test_propagate_source_loss_no_claims_impacted():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        document_id, document_version_id, document_chunk_id, evidence_span_id = (
            _create_document_evidence_span(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                created_by=user_id,
            )
        )
        # No claim_evidence_links for this evidence_span.
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
            document_chunk_id=document_chunk_id,
            document_version_id=document_version_id,
            document_id=document_id,
        )

    with engine.begin() as conn:
        result = propagate_source_loss(conn, source_loss_event_id=sle_id)

    assert result["status"] == "no_claims_impacted"
    assert result["impacted_claim_count"] == 0

    with engine.connect() as conn:
        # Dedicated propagation row exists.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="no_claims_impacted",
            status="recorded",
        ) == 1

        # No claim_marked_unverifiable propagation row was created.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
        ) == 0

        # No claim ledger entries were created via this path: there are no
        # logical_claims for this task, so the count is 0.
        n_entries = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM claim_ledger_entries cle
                    JOIN logical_claims lc ON lc.id = cle.claim_logical_id
                    WHERE lc.task_id = :tid
                    """
                ),
                {"tid": task_id},
            ).scalar_one()
        )
        assert n_entries == 0


# ---------------------------------------------------------------------------
# Test 4 — published_answer impacted, but NOT withdrawn
# ---------------------------------------------------------------------------
def test_propagate_source_loss_marks_published_answer_impacted_without_withdrawing():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        document_id, document_version_id, document_chunk_id, evidence_span_id = (
            _create_document_evidence_span(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                created_by=user_id,
            )
        )
        claim_logical_id, claim_ledger_entry_id = (
            _create_logical_claim_with_verified_entry(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
            )
        )
        _link_claim_to_evidence_span(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=claim_ledger_entry_id,
            evidence_span_id=evidence_span_id,
        )
        pa_id = _create_published_answer_for_claim(
            conn,
            task_id=task_id,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=claim_ledger_entry_id,
            status="published",
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
            document_chunk_id=document_chunk_id,
            document_version_id=document_version_id,
            document_id=document_id,
        )

    with engine.begin() as conn:
        result = propagate_source_loss(conn, source_loss_event_id=sle_id)

    assert result["status"] == "propagated"
    assert result["impacted_published_answer_count"] == 1
    assert result["newly_recorded_published_answer_count"] == 1

    with engine.connect() as conn:
        # Propagation record for published_answer_impacted / recorded.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="published_answer_impacted",
            status="recorded",
        ) == 1

        # The published_answer must NOT have been mutated.
        pa = _fetch_published_answer_status(conn, published_answer_id=pa_id)
        assert str(pa["status"]) == "published"
        assert pa["withdrawn_at"] is None
        assert pa["superseded_at"] is None
        assert pa["superseded_by_id"] is None

        # Audit event for the published_answer impact.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="source_loss.propagated_to_published_answer",
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# Test 5 — claims impacted but no active published_answer
# ---------------------------------------------------------------------------
def test_propagate_source_loss_no_active_published_answers_impacted():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        document_id, document_version_id, document_chunk_id, evidence_span_id = (
            _create_document_evidence_span(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                created_by=user_id,
            )
        )
        claim_logical_id, claim_ledger_entry_id = (
            _create_logical_claim_with_verified_entry(
                conn,
                tenant_id=tenant_id,
                project_id=project_id,
                task_id=task_id,
            )
        )
        _link_claim_to_evidence_span(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=claim_ledger_entry_id,
            evidence_span_id=evidence_span_id,
        )
        # Build a published_answer that depends on this claim, but in
        # status='withdrawn': this exercises the
        # "no active published answers" branch because the propagator's
        # query filters on pa.status = 'published'.
        _create_published_answer_for_claim(
            conn,
            task_id=task_id,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=claim_ledger_entry_id,
            status="withdrawn",
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
            document_chunk_id=document_chunk_id,
            document_version_id=document_version_id,
            document_id=document_id,
        )

    with engine.begin() as conn:
        result = propagate_source_loss(conn, source_loss_event_id=sle_id)

    assert result["status"] == "propagated"
    assert result["impacted_published_answer_count"] == 0

    with engine.connect() as conn:
        # No 'published_answer_impacted' propagation row.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="published_answer_impacted",
        ) == 0

        # The dedicated 'no_active_published_answers_impacted' row is present.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="no_active_published_answers_impacted",
            status="recorded",
        ) == 1

        # No audit for published_answer impact.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="source_loss.propagated_to_published_answer",
        ) == 0


# ---------------------------------------------------------------------------
# Test 6 — not_found on unknown source_loss_event_id
# ---------------------------------------------------------------------------
def test_propagate_source_loss_not_found():
    engine = get_engine()
    bogus_id = uuid.uuid4()

    with engine.begin() as conn:
        result = propagate_source_loss(conn, source_loss_event_id=bogus_id)

    assert result["status"] == "not_found"
    assert result["source_loss_event_id"] == str(bogus_id)
