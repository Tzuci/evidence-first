"""Worker-level tests for apps/worker/app/consumers/source_loss.py
(Phase 8.5 — Block 3B-2).

Coverage map (10 scenarios required by the block prompt):

  1. test_source_loss_consumer_happy_path_marks_claim_unverifiable
  2. test_source_loss_consumer_redelivery_same_consumer_key_skips
  3. test_source_loss_consumer_different_consumer_key_same_source_loss_event_is_service_idempotent
  4. test_source_loss_consumer_no_claims_impacted
  5. test_source_loss_consumer_marks_published_answer_impacted_without_withdrawing
  6. test_source_loss_consumer_no_active_published_answers_impacted
  7. test_source_loss_consumer_missing_source_loss_event_with_tenant_records_failed_epr
  8. test_source_loss_consumer_missing_source_loss_event_without_tenant_writes_no_epr
  9. test_source_loss_consumer_malformed_required_field_writes_no_epr
 10. test_source_loss_consumer_bad_event_type_writes_no_epr

Design notes:
  - This file lives under apps/worker/tests/. The Python package `app`
    resolves to apps/worker/app, so we can import the consumer entry
    point and the worker DB helper directly without any sys.path tweaking.
  - We DO NOT call propagate_source_loss directly. The contract under
    test is the consumer handler (handle_source_loss) end-to-end: its
    event parsing rules, its EPR bookkeeping, and its delegation to the
    propagator service.
  - We DO NOT spin up Redis or the worker loop. The handler is invoked
    with a plain Python dict (the same shape produced by the Redis
    decoder in apps/worker/app/main.py, but with native types — the
    consumer accepts both forms).
  - We DO NOT seed source_loss_propagation_records, claim_ledger_entries
    v(N+1), claim_lineage, or audit rows manually for the impact path.
    Every such row observed by these tests is produced by the service
    via the consumer call.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe) so the suite stays clean on a long-running
    dev DB.
  - verify_task_audit_chain expects a Connection, not an Engine; we
    always wrap the call in `with engine.connect() as conn:`.
  - The consumer_name used in tests is a stable, logical identifier
    ("test_source_loss"), not a per-instance worker identity.
  - We never mutate task_masters.status.
  - We never mutate published_answers.status.
  - We never invoke apply_withdrawal nor write to
    published_answer_lifecycle_events.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.consumers.source_loss import handle_source_loss
from app.db import get_engine
from evidencefirst_shared.db.audit import verify_task_audit_chain


CONSUMER_NAME = "test_source_loss"

EVENT_TYPE = "source_loss.detected"

AUDIT_EVENT_PROPAGATED_TO_CLAIM = "source_loss.propagated_to_claim"
AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER = (
    "source_loss.propagated_to_published_answer"
)


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
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
                {"t": tenant_id, "n": f"source-loss-consumer-test-{uuid.uuid4()}"},
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

    Order of inserts (to honor every FK and the storage_blobs unique
    partial index sb_global_uq):
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans

    Returns (document_id, document_version_id, document_chunk_id,
    evidence_span_id).
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source loss consumer test marker {marker}. "
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
    'verified_fact'. This is the typical 'head' state of a published
    claim, which is what the propagator expects to supersede with a new
    'unverifiable' v2 entry on a source loss.

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
    """Insert a claim_evidence_links row connecting the (logical, entry)
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
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID,
    document_chunk_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    loss_kind: str = "source_deleted",
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
                         :lk, 'unit-test loss reason',
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
                    "lk": loss_kind,
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
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Build the minimal 8.4 chain so that the propagator's
    impacted-published-answers query can find this published_answer:

      draft_final_answers v1
        -> final_answer_spans (1)
          -> final_answer_span_claim_links (1) referencing the given
             (claim_logical_id, claim_ledger_entry_id)
      final_gate_reports (decision='approved')
      published_answers v1 (status as requested; default 'published')

    Honors the composite FKs in 0005 (draft + gate consistency on
    task_id). Status defaults to 'published'; pass 'withdrawn' or
    'superseded' only in test setup. The propagator MUST NOT change this
    status afterwards.

    Returns (draft_id, span_id, gate_id, published_answer_id).
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

    return draft_id, span_id, gate_report_id, pa_id


# ---------------------------------------------------------------------------
# event builder
# ---------------------------------------------------------------------------
def _event_for(
    source_loss_event_id: uuid.UUID | str,
    *,
    event_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    event_type: str = EVENT_TYPE,
) -> dict[str, Any]:
    """Build a well-shaped source_loss event for the consumer.

    The handler accepts both native (uuid.UUID) and string-encoded UUID
    fields, mirroring the Redis-decoded event shape produced by main.py.
    Tests use native types for clarity and switch to strings only when
    explicitly validating malformed-event branches.

    Optional fields default to None, which the consumer correctly treats
    as "absent" rather than "empty string".
    """
    payload: dict[str, Any] = {
        "event_id": event_id if event_id is not None else uuid.uuid4(),
        "event_type": event_type,
        "source_loss_event_id": source_loss_event_id,
        "idempotency_key": (
            idempotency_key if idempotency_key is not None else _unique_hex()
        ),
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    return payload


# ---------------------------------------------------------------------------
# count / fetch helpers
# ---------------------------------------------------------------------------
def _count_epr(
    conn: Connection, *, consumer_name: str, idempotency_key: str
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM event_processing_records
                WHERE consumer_name = :c AND idempotency_key = :k
                """
            ),
            {"c": consumer_name, "k": idempotency_key},
        ).scalar_one()
    )


def _fetch_epr(
    conn: Connection, *, consumer_name: str, idempotency_key: str
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, processing_status, error_code, error_message,
                   tenant_id, project_id, task_id
            FROM event_processing_records
            WHERE consumer_name = :c AND idempotency_key = :k
            LIMIT 1
            """
        ),
        {"c": consumer_name, "k": idempotency_key},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "id": uuid.UUID(str(m["id"])),
        "processing_status": str(m["processing_status"]),
        "error_code": m["error_code"],
        "error_message": m["error_message"],
        "tenant_id": uuid.UUID(str(m["tenant_id"])) if m["tenant_id"] is not None else None,
        "project_id": uuid.UUID(str(m["project_id"])) if m["project_id"] is not None else None,
        "task_id": uuid.UUID(str(m["task_id"])) if m["task_id"] is not None else None,
    }


def _count_claim_ledger_entries(
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


def _latest_claim_ledger_entry(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> dict[str, Any]:
    row = conn.execute(
        text(
            """
            SELECT id, version_no, state, support_scope,
                   user_provided_dependency, transition_reason, payload
            FROM claim_ledger_entries
            WHERE claim_logical_id = :lc
            ORDER BY version_no DESC
            LIMIT 1
            """
        ),
        {"lc": claim_logical_id},
    ).one()
    return dict(row._mapping)


def _count_claim_lineage(
    conn: Connection,
    *,
    parent_entry_id: uuid.UUID | None = None,
    child_entry_id: uuid.UUID | None = None,
    relation_kind: str = "supersedes",
) -> int:
    if parent_entry_id is not None and child_entry_id is not None:
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
    if child_entry_id is not None:
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
    if parent_entry_id is not None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM claim_lineage
                    WHERE parent_entry_id = :p AND relation_kind = :rk
                    """
                ),
                {"p": parent_entry_id, "rk": relation_kind},
            ).scalar_one()
        )
    raise ValueError(
        "_count_claim_lineage: at least one of parent_entry_id/child_entry_id required"
    )


def _count_propagation_records(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    propagation_kind: str | None = None,
    status: str | None = None,
) -> int:
    if propagation_kind is None and status is None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_propagation_records
                    WHERE source_loss_event_id = :sle
                    """
                ),
                {"sle": source_loss_event_id},
            ).scalar_one()
        )
    if propagation_kind is not None and status is None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_propagation_records
                    WHERE source_loss_event_id = :sle
                      AND propagation_kind     = :pk
                    """
                ),
                {"sle": source_loss_event_id, "pk": propagation_kind},
            ).scalar_one()
        )
    if propagation_kind is None and status is not None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_propagation_records
                    WHERE source_loss_event_id = :sle
                      AND status               = :st
                    """
                ),
                {"sle": source_loss_event_id, "st": status},
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


def _fetch_published_answer(
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


def _count_lifecycle_events_for_pa(
    conn: Connection, *, published_answer_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE published_answer_id = :pid
                """
            ),
            {"pid": published_answer_id},
        ).scalar_one()
    )


def _fetch_task_status(conn: Connection, *, task_id: uuid.UUID) -> str:
    """task_masters.status MUST stay invariant across any source_loss
    propagation: the lifecycle never lives on the task. We assert this
    invariant where it matters.
    """
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# 1 — happy path: claim is marked unverifiable
# ---------------------------------------------------------------------------
def test_source_loss_consumer_happy_path_marks_claim_unverifiable():
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
        claim_logical_id, original_entry_id = (
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
            claim_ledger_entry_id=original_entry_id,
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
        task_status_before = _fetch_task_status(conn, task_id=task_id)

    event = _event_for(sle_id, tenant_id=tenant_id)
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        # EPR: succeeded with full scope.
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"
        assert epr["tenant_id"] == tenant_id
        assert epr["project_id"] == project_id
        assert epr["task_id"] == task_id

        # Two ledger entries now exist for the claim: v1 verified_fact and
        # v2 unverifiable.
        assert _count_claim_ledger_entries(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        latest = _latest_claim_ledger_entry(
            conn, claim_logical_id=claim_logical_id
        )
        assert int(latest["version_no"]) == 2
        assert str(latest["state"]) == "unverifiable"
        assert str(latest["support_scope"]) == "unsupported"
        assert str(latest["user_provided_dependency"]) == "unsupported"
        assert str(latest["transition_reason"]) == "source_lost"

        # The propagator stamps the source loss context onto the new
        # ledger entry's payload. We assert the keys explicitly so a
        # future contract change is caught.
        payload = latest["payload"]
        assert isinstance(payload, dict)
        assert str(payload.get("source_loss_event_id")) == str(sle_id)
        assert str(payload.get("evidence_span_id")) == str(evidence_span_id)
        assert str(payload.get("previous_entry_id")) == str(original_entry_id)
        assert str(payload.get("previous_state")) == "verified_fact"
        assert str(payload.get("loss_kind")) == "source_deleted"

        new_entry_id = uuid.UUID(str(latest["id"]))

        # Lineage edge v1 -> v2 ('supersedes') exists exactly once.
        assert _count_claim_lineage(
            conn,
            parent_entry_id=original_entry_id,
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
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 1

        # task_masters.status MUST NOT have been changed.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        # Audit chain integrity verifies end-to-end.
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# 2 — redelivery with same consumer key short-circuits
# ---------------------------------------------------------------------------
def test_source_loss_consumer_redelivery_same_consumer_key_skips():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _doc, _dv, _dc, evidence_span_id = _create_document_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        claim_logical_id, original_entry_id = (
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
            claim_ledger_entry_id=original_entry_id,
            evidence_span_id=evidence_span_id,
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
        )

    idem = _unique_hex()
    # Reuse the SAME idempotency_key on both deliveries. event_id can
    # change (Redis assigns a new entry id on every retry), but the
    # consumer keys off idempotency_key for EPR uniqueness.
    event_1 = _event_for(sle_id, idempotency_key=idem, tenant_id=tenant_id)
    event_2 = _event_for(sle_id, idempotency_key=idem, tenant_id=tenant_id)

    rc_1 = handle_source_loss(event_1, consumer_name=CONSUMER_NAME)
    assert rc_1 == "processed"

    rc_2 = handle_source_loss(event_2, consumer_name=CONSUMER_NAME)
    assert rc_2 == "skipped_already_succeeded"

    with engine.connect() as conn:
        # Exactly one EPR row keyed on (consumer, idempotency_key).
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 1
        epr = _fetch_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        )
        assert epr is not None
        assert epr["processing_status"] == "succeeded"

        # Ledger entries: still 2 (v1 verified + v2 unverifiable).
        assert _count_claim_ledger_entries(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        # Audit emitted exactly once.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 1

        # Propagation record: total recorded + skipped for
        # claim_marked_unverifiable on this source_loss_event remains
        # exactly 1 (the partial unique index covers both statuses).
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


# ---------------------------------------------------------------------------
# 3 — different consumer keys, same source_loss_event_id
#      -> service-level idempotency
# ---------------------------------------------------------------------------
def test_source_loss_consumer_different_consumer_key_same_source_loss_event_is_service_idempotent():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _doc, _dv, _dc, evidence_span_id = _create_document_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        claim_logical_id, original_entry_id = (
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
            claim_ledger_entry_id=original_entry_id,
            evidence_span_id=evidence_span_id,
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
        )

    consumer_key_a = f"consumer-a-{_unique_hex()}"
    consumer_key_b = f"consumer-b-{_unique_hex()}"

    event_a = _event_for(
        sle_id, idempotency_key=consumer_key_a, tenant_id=tenant_id
    )
    event_b = _event_for(
        sle_id, idempotency_key=consumer_key_b, tenant_id=tenant_id
    )

    rc_a = handle_source_loss(event_a, consumer_name=CONSUMER_NAME)
    assert rc_a == "processed"

    # Second call passes through a fresh consumer-level idempotency slot
    # (so begin_processing returns 'started', not 'succeeded'), but the
    # propagator's per-claim handler detects that the head is already in
    # 'unverifiable' / 'source_lost' state and short-circuits via the
    # 'skipped' branch. The propagation record partial unique index
    # covers status IN ('recorded', 'skipped'), so no duplicate row is
    # inserted. The consumer maps the propagator's "propagated" outcome
    # to "processed" regardless.
    rc_b = handle_source_loss(event_b, consumer_name=CONSUMER_NAME)
    assert rc_b == "processed"

    with engine.connect() as conn:
        # Two EPR rows, one per consumer key, both succeeded.
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_a
        ) == 1
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_b
        ) == 1
        epr_a = _fetch_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_a
        )
        epr_b = _fetch_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=consumer_key_b
        )
        assert epr_a is not None and epr_a["processing_status"] == "succeeded"
        assert epr_b is not None and epr_b["processing_status"] == "succeeded"

        # Ledger entries: still 2. The propagator does NOT append a
        # second unverifiable entry when the head is already source_lost.
        assert _count_claim_ledger_entries(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        # Audit emitted exactly once: only the first call actually
        # transitioned the head and hit the audit emission gate.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 1

        # Total recorded + skipped propagation rows for
        # claim_marked_unverifiable on this source_loss_event remains 1
        # (partial unique index covers both statuses).
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


# ---------------------------------------------------------------------------
# 4 — no claims impacted
# ---------------------------------------------------------------------------
def test_source_loss_consumer_no_claims_impacted():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _doc, _dv, _dc, evidence_span_id = _create_document_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        # NO claim_evidence_links pointing at this evidence_span.
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
        )

    event = _event_for(sle_id, tenant_id=tenant_id)
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"

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

        # No claim ledger entries were created via this path: there are
        # no logical_claims for this task, so the count is 0.
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

        # No audit for claim propagation.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 0


# ---------------------------------------------------------------------------
# 5 — published_answer impacted, but NOT withdrawn
# ---------------------------------------------------------------------------
def test_source_loss_consumer_marks_published_answer_impacted_without_withdrawing():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _doc, _dv, _dc, evidence_span_id = _create_document_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        claim_logical_id, original_entry_id = (
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
            claim_ledger_entry_id=original_entry_id,
            evidence_span_id=evidence_span_id,
        )
        _draft_id, _span_id, _gate_id, pa_id = _create_published_answer_for_claim(
            conn,
            task_id=task_id,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=original_entry_id,
            status="published",
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
        )
        task_status_before = _fetch_task_status(conn, task_id=task_id)

    event = _event_for(sle_id, tenant_id=tenant_id)
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"

        # Propagation record for published_answer_impacted / recorded.
        assert _count_propagation_records(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="published_answer_impacted",
            status="recorded",
        ) == 1

        # The published_answer must NOT have been mutated.
        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert str(pa["status"]) == "published"
        assert pa["withdrawn_at"] is None
        assert pa["superseded_at"] is None
        assert pa["superseded_by_id"] is None

        # No published_answer_lifecycle_events were inserted by this
        # pipeline. The source_loss consumer must NEVER write to that
        # table; only the lifecycle service does.
        assert _count_lifecycle_events_for_pa(
            conn, published_answer_id=pa_id
        ) == 0

        # Audit event for the published_answer impact emitted exactly once.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER,
        ) == 1

        # task_masters.status invariant.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True


# ---------------------------------------------------------------------------
# 6 — claims impacted but no active published_answer
# ---------------------------------------------------------------------------
def test_source_loss_consumer_no_active_published_answers_impacted():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _doc, _dv, _dc, evidence_span_id = _create_document_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        claim_logical_id, original_entry_id = (
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
            claim_ledger_entry_id=original_entry_id,
            evidence_span_id=evidence_span_id,
        )
        # Build a published_answer that depends on this claim, but in
        # status='withdrawn': we set the column directly in setup
        # (NEVER via apply_withdrawal). This exercises the
        # 'no_active_published_answers_impacted' branch because the
        # propagator's query filters on pa.status = 'published'.
        _draft_id, _span_id, _gate_id, pa_id = _create_published_answer_for_claim(
            conn,
            task_id=task_id,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=original_entry_id,
            status="withdrawn",
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
        )

    event = _event_for(sle_id, tenant_id=tenant_id)
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "processed"

    with engine.connect() as conn:
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "succeeded"

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
            event_type=AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER,
        ) == 0

        # The withdrawn published_answer row remains untouched. Its
        # status was set by test setup (status='withdrawn') and the
        # source_loss pipeline must NOT alter it back to 'published',
        # NOT add lifecycle events, NOT mutate withdrawn_at.
        pa = _fetch_published_answer(conn, published_answer_id=pa_id)
        assert str(pa["status"]) == "withdrawn"
        assert _count_lifecycle_events_for_pa(
            conn, published_answer_id=pa_id
        ) == 0


# ---------------------------------------------------------------------------
# 7 — missing source_loss_event + tenant_id provided -> failed EPR
# ---------------------------------------------------------------------------
def test_source_loss_consumer_missing_source_loss_event_with_tenant_records_failed_epr():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, _user_id, _task_id = _seeded_dev(conn)

    bogus_sle_id = uuid.uuid4()
    event = _event_for(bogus_sle_id, tenant_id=tenant_id)
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        epr = _fetch_epr(conn, consumer_name=CONSUMER_NAME, idempotency_key=idem)
        assert epr is not None
        assert epr["processing_status"] == "failed"
        assert epr["error_code"] == "WORKER_SOURCE_LOSS_EVENT_NOT_VISIBLE"
        # tenant_id is the one supplied by the producer; project/task
        # remain NULL because the source_loss_event could not be resolved.
        assert epr["tenant_id"] == tenant_id
        assert epr["project_id"] is None
        assert epr["task_id"] is None


# ---------------------------------------------------------------------------
# 8 — missing source_loss_event + no tenant_id -> no EPR row
# ---------------------------------------------------------------------------
def test_source_loss_consumer_missing_source_loss_event_without_tenant_writes_no_epr():
    engine = get_engine()

    bogus_sle_id = uuid.uuid4()
    # Deliberately omit tenant_id: the consumer cannot persist an EPR
    # row without it (event_processing_records.tenant_id is NOT NULL).
    event = _event_for(bogus_sle_id)
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 0


# ---------------------------------------------------------------------------
# 9 — malformed required field -> failed pre-transaction, no EPR row
# ---------------------------------------------------------------------------
def test_source_loss_consumer_malformed_required_field_writes_no_epr():
    """Two malformed shapes covered by the same pre-transaction branch
    in the consumer:

      a) missing event_id (KeyError on the required-field block);
      b) syntactically invalid source_loss_event_id (ValueError on UUID
         parse).

    Both must short-circuit BEFORE any DB write: no
    event_processing_records row, no transaction opened.
    """
    engine = get_engine()

    # Sub-case (a): missing event_id.
    event_a: dict[str, Any] = {
        # 'event_id' deliberately absent
        "event_type": EVENT_TYPE,
        "source_loss_event_id": uuid.uuid4(),
        "idempotency_key": _unique_hex(),
    }
    rc_a = handle_source_loss(event_a, consumer_name=CONSUMER_NAME)
    assert rc_a == "failed"
    with engine.connect() as conn:
        assert _count_epr(
            conn,
            consumer_name=CONSUMER_NAME,
            idempotency_key=str(event_a["idempotency_key"]),
        ) == 0

    # Sub-case (b): source_loss_event_id is not a valid UUID.
    event_b: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "event_type": EVENT_TYPE,
        "source_loss_event_id": "not-a-uuid",
        "idempotency_key": _unique_hex(),
    }
    rc_b = handle_source_loss(event_b, consumer_name=CONSUMER_NAME)
    assert rc_b == "failed"
    with engine.connect() as conn:
        assert _count_epr(
            conn,
            consumer_name=CONSUMER_NAME,
            idempotency_key=str(event_b["idempotency_key"]),
        ) == 0


# ---------------------------------------------------------------------------
# 10 — wrong event_type -> failed pre-transaction, no EPR row
# ---------------------------------------------------------------------------
def test_source_loss_consumer_bad_event_type_writes_no_epr():
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _doc, _dv, _dc, evidence_span_id = _create_document_evidence_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            created_by=user_id,
        )
        claim_logical_id, original_entry_id = (
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
            claim_ledger_entry_id=original_entry_id,
            evidence_span_id=evidence_span_id,
        )
        sle_id = _create_source_loss_event(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=evidence_span_id,
        )

    event = _event_for(
        sle_id,
        tenant_id=tenant_id,
        event_type="source_loss.something_else",
    )
    idem = str(event["idempotency_key"])

    rc = handle_source_loss(event, consumer_name=CONSUMER_NAME)
    assert rc == "failed"

    with engine.connect() as conn:
        assert _count_epr(
            conn, consumer_name=CONSUMER_NAME, idempotency_key=idem
        ) == 0

        # No propagation row was created for this source_loss_event,
        # since the consumer rejected the event before opening any
        # transaction.
        assert _count_propagation_records(
            conn, source_loss_event_id=sle_id
        ) == 0

        # The claim ledger remains untouched (only the seeded v1 entry).
        assert _count_claim_ledger_entries(
            conn, claim_logical_id=claim_logical_id
        ) == 1

        # No audit events for source_loss propagation on this task.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 0
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER,
        ) == 0
