"""API tests for the source_loss propagation read endpoint
(Phase 8.6 — Block 8.6C).

Endpoint exercised:
  GET /api/v1/source-loss-events/{source_loss_event_id}/propagation

Coverage map (9 scenarios required by the block prompt):

  1. test_source_loss_propagation_happy_path_claim_marked_unverifiable
  2. test_source_loss_propagation_happy_path_published_answer_impacted
  3. test_source_loss_propagation_event_exists_no_rows_returns_empty_items
  4. test_source_loss_propagation_404_for_unknown_source_loss_event
  5. test_source_loss_propagation_filter_propagation_kind
  6. test_source_loss_propagation_filter_status_failed
  7. test_source_loss_propagation_limit_truncates_response
  8. test_source_loss_propagation_invalid_filters_return_validation_error
  9. test_source_loss_propagation_endpoint_is_read_only

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    resolves to apps/api/app, so ``from app.main import app`` and
    ``from app.db import get_engine`` are the canonical imports — same
    pattern used by all other 8.5 / 8.6 API test modules.
  - We do NOT touch Redis: the endpoint is strictly read-only and does
    not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code (no
    ``propagate_source_loss``, no dispatcher, no consumer). All
    propagation rows are seeded directly via SQL — exactly what the
    worker propagator does in production, minus the orchestration.
  - Helpers are LOCAL to this file (we copy the seed primitives from
    the 8.6B and propagator-service test modules); the file is
    autonomous.
  - All append-only tables involved (``source_loss_events``,
    ``source_loss_propagation_records``, ``claim_ledger_entries``,
    ``published_answer_lifecycle_events``) accept INSERT — the
    ``reject_modify_append_only`` trigger only blocks UPDATE / DELETE.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe).
  - The partial unique indexes on ``source_loss_propagation_records``
    cover ``status IN ('recorded', 'skipped')`` only; ``failed`` rows
    can therefore be inserted without colliding even for the same
    ``(source_loss_event_id, propagation_kind, claim_logical_id)``
    tuple, which is what makes the status=failed filter test possible.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.main import app


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    Mirrors the gating used by other 8.5 / 8.6 API test modules: the
    endpoint needs a real DB to seed the evidence_span chain, the
    ``source_loss_events`` row and the propagation rows. No Redis is
    required.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; bring up the stack first.")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("DB unreachable; run `make up` and `make migrate && make seed`.")


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hex() -> str:
    """Return a rerun-safe sha256 hex string unique per call."""
    return hashlib.sha256(uuid.uuid4().bytes + uuid.uuid4().bytes).hexdigest()


def _err(resp_json: dict[str, Any]) -> dict[str, Any]:
    """Extract the normalized error envelope from a NormalizedError response.

    Envelope shape (from packages/shared/evidencefirst_shared/errors.py):
        {"error": {"code": "...", "message": "...", "details": {...}, ...}}
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


def _endpoint(source_loss_event_id: uuid.UUID) -> str:
    return f"/api/v1/source-loss-events/{source_loss_event_id}/propagation"


def _normalize_jsonb(value: Any) -> Any:
    """Normalize a JSONB column read into a Python object.

    psycopg (3.x) returns JSONB columns as native Python values, but on
    some driver / pool combinations the value may surface as a JSON
    string. We accept both forms to keep the tests robust, mirroring
    the convention adopted by the 8.6B test module.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# DB seeding helpers — tenant / project / task
# ---------------------------------------------------------------------------
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
# DB seeding helpers — evidence_span chain
# ---------------------------------------------------------------------------
def _create_evidence_span_chain(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    """Create the full storage chain ending in an evidence_spans row.

    Order of inserts (to honor every FK and the storage_blobs unique
    partial index sb_global_uq):
      storage_blobs -> storage_objects -> uploaded_documents
        -> document_versions (kind='parsed') -> document_chunks
        -> evidence_spans

    Returns a dict with the chain ids; mirrors the helper in
    apps/api/tests/test_source_loss_endpoint.py and
    apps/api/tests/test_source_loss_events_read_endpoint.py.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source loss propagation API test marker {marker}. "
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
                    "u": user_id,
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

    return {
        "document_id": document_id,
        "document_version_id": document_version_id,
        "document_chunk_id": document_chunk_id,
        "evidence_span_id": evidence_span_id,
    }


# ---------------------------------------------------------------------------
# DB seeding helpers — source_loss_events
# ---------------------------------------------------------------------------
def _insert_source_loss_event_row(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    document_chunk_id: uuid.UUID,
    document_version_id: uuid.UUID,
    document_id: uuid.UUID,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    """Insert one ``source_loss_events`` row directly via SQL.

    Mirrors what the API producer endpoint persists: ``task_id`` is
    NULL by design (a span may back claims of multiple tasks), all
    document_* columns are populated, ``event_payload`` is ``{}``,
    ``detected_by='api'``.
    """
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO source_loss_events (
                id, tenant_id, project_id, task_id,
                evidence_span_id, document_chunk_id,
                document_version_id, document_id,
                loss_kind, loss_reason, detected_by,
                event_payload, idempotency_key
            ) VALUES (
                :id, :tenant_id, :project_id, NULL,
                :evidence_span_id, :document_chunk_id,
                :document_version_id, :document_id,
                'source_deleted', 'unit-test loss reason', 'api',
                '{}'::jsonb, :idempotency_key
            )
            """
        ),
        {
            "id": new_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "evidence_span_id": evidence_span_id,
            "document_chunk_id": document_chunk_id,
            "document_version_id": document_version_id,
            "document_id": document_id,
            "idempotency_key": idempotency_key or _unique_hex(),
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB seeding helpers — logical_claims + claim_ledger_entries
# ---------------------------------------------------------------------------
def _create_logical_claim_with_verified_entry(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create one logical_claims row plus a v1 ledger entry in state
    'verified_fact'. Returns (claim_logical_id, claim_ledger_entry_id).

    Mirrors the helper in apps/worker/tests/test_source_loss_propagator_service.py.
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


def _append_unverifiable_ledger_entry(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    version_no: int,
) -> uuid.UUID:
    """Append an 'unverifiable' / 'source_lost' v(N+1) ledger entry.

    Mirrors what the worker propagator writes in production. The
    schema enforces append-only via trigger; the INSERT itself is the
    normal write path.
    """
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO claim_ledger_entries
                (id, claim_logical_id, version_no, state,
                 support_scope, user_provided_dependency,
                 transition_reason)
            VALUES (:id, :lc, :vno, 'unverifiable',
                    'unsupported', 'unsupported',
                    'source_lost')
            """
        ),
        {"id": new_id, "lc": claim_logical_id, "vno": version_no},
    )
    return new_id


# ---------------------------------------------------------------------------
# DB seeding helpers — published_answer chain
# ---------------------------------------------------------------------------
def _create_published_answer(
    conn: Connection,
    *,
    task_id: uuid.UUID,
) -> uuid.UUID:
    """Build the minimal 8.4 chain so a published_answer exists for the
    given task: draft v1 -> approved gate -> published v1.

    The chain is sufficient to satisfy FK requirements for
    ``source_loss_propagation_records.published_answer_id``. Mirrors
    the helper in
    apps/api/tests/test_published_answer_lifecycle_events_endpoint.py.
    """
    summary_text = f"summary-{uuid.uuid4()}\n"

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

    gate_id = uuid.UUID(
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
    pa_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO published_answers
                        (id, task_id, draft_final_answer_id, final_gate_report_id,
                         version_no, content_hash, status)
                    VALUES (:id, :t, :d, :g, 1, :h, 'published')
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "d": draft_id,
                    "g": gate_id,
                    "h": content_hash,
                },
            ).first()[0]
        )
    )
    return pa_id


# ---------------------------------------------------------------------------
# DB seeding helpers — source_loss_propagation_records
# ---------------------------------------------------------------------------
def _insert_propagation_record(
    conn: Connection,
    *,
    source_loss_event_id: uuid.UUID,
    propagation_kind: str,
    status: str,
    claim_logical_id: uuid.UUID | None = None,
    old_claim_ledger_entry_id: uuid.UUID | None = None,
    new_claim_ledger_entry_id: uuid.UUID | None = None,
    published_answer_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert one ``source_loss_propagation_records`` row directly via SQL.

    The table is APPEND-ONLY via trigger; INSERT is the normal write
    path. The partial unique indexes cover only ``recorded`` and
    ``skipped`` rows — ``failed`` rows can be freely repeated for the
    same (source_loss_event_id, propagation_kind, target) tuple, which
    is what makes the status=failed filter test feasible.
    """
    payload = details if details is not None else {}
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO source_loss_propagation_records (
                id, source_loss_event_id,
                claim_logical_id,
                old_claim_ledger_entry_id, new_claim_ledger_entry_id,
                published_answer_id,
                propagation_kind, status, details
            ) VALUES (
                :id, :sle,
                :clid,
                :old_id, :new_id,
                :pa_id,
                :kind, :status, CAST(:details AS JSONB)
            )
            """
        ),
        {
            "id": new_id,
            "sle": source_loss_event_id,
            "clid": claim_logical_id,
            "old_id": old_claim_ledger_entry_id,
            "new_id": new_claim_ledger_entry_id,
            "pa_id": published_answer_id,
            "kind": propagation_kind,
            "status": status,
            "details": json.dumps(payload, sort_keys=True),
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
# Whitelist of tables we are willing to count via _count_table.
# Hardcoded to avoid any SQL injection vector even though this is test
# code: the table name is interpolated into the query, but only from
# this fixed set. Mirrors the pattern adopted by the 8.6A / 8.6B test
# modules.
_COUNTABLE_TABLES = frozenset(
    {
        "published_answer_lifecycle_events",
        "source_loss_events",
        "source_loss_propagation_records",
        "published_answers",
        "claim_ledger_entries",
        "claim_lineage",
        "audit_records",
    }
)


def _count_table(conn: Connection, table_name: str) -> int:
    """Return a global COUNT(*) of the named table.

    Only accepts a hardcoded whitelist of table names to keep the SQL
    construction safe.
    """
    if table_name not in _COUNTABLE_TABLES:
        raise ValueError(f"refusing to count unknown table: {table_name!r}")
    return int(
        conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
    )


def _snapshot_all_counts(conn: Connection) -> dict[str, int]:
    return {t: _count_table(conn, t) for t in _COUNTABLE_TABLES}


def _fetch_propagation_row(
    conn: Connection, *, prop_id: uuid.UUID
) -> dict[str, Any]:
    """Read back one propagation row for byte-level comparison.

    Used by the read-only invariant test: we snapshot the row before
    and after the GETs and assert they are byte-for-byte identical.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id, source_loss_event_id,
              claim_logical_id,
              old_claim_ledger_entry_id, new_claim_ledger_entry_id,
              published_answer_id,
              propagation_kind, status, details, created_at
            FROM source_loss_propagation_records
            WHERE id = :id
            """
        ),
        {"id": prop_id},
    ).one()
    m = row._mapping

    def _opt_uuid(value: Any) -> uuid.UUID | None:
        return uuid.UUID(str(value)) if value is not None else None

    return {
        "id": uuid.UUID(str(m["id"])),
        "source_loss_event_id": uuid.UUID(str(m["source_loss_event_id"])),
        "claim_logical_id": _opt_uuid(m["claim_logical_id"]),
        "old_claim_ledger_entry_id": _opt_uuid(m["old_claim_ledger_entry_id"]),
        "new_claim_ledger_entry_id": _opt_uuid(m["new_claim_ledger_entry_id"]),
        "published_answer_id": _opt_uuid(m["published_answer_id"]),
        "propagation_kind": str(m["propagation_kind"]),
        "status": str(m["status"]),
        "details": _normalize_jsonb(m["details"]),
        "created_at": m["created_at"],
    }


# ===========================================================================
# 1 — happy path: claim_marked_unverifiable / recorded
# ===========================================================================
def test_source_loss_propagation_happy_path_claim_marked_unverifiable() -> None:
    """Seed one source_loss_events row, one logical_claim with a v1
    verified_fact + a v2 unverifiable ledger entry, and one
    propagation row in ``claim_marked_unverifiable / recorded`` form.
    GET propagation and assert the full envelope plus every field on
    the response item.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        claim_logical_id, old_entry_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        new_entry_id = _append_unverifiable_ledger_entry(
            conn,
            claim_logical_id=claim_logical_id,
            version_no=2,
        )

    seeded_details = {
        "service_name": "mvp0_source_loss_propagator_v1",
        "previous_state": "verified_fact",
        "new_state": "unverifiable",
        "nested": {"k": "v"},
    }
    with engine.begin() as conn:
        prop_id = _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
            claim_logical_id=claim_logical_id,
            old_claim_ledger_entry_id=old_entry_id,
            new_claim_ledger_entry_id=new_entry_id,
            published_answer_id=None,
            details=seeded_details,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(sle_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["source_loss_event_id"] == str(sle_id)
    items = body["items"]
    assert isinstance(items, list)
    assert len(items) == 1

    item = items[0]
    for f in (
        "id",
        "source_loss_event_id",
        "claim_logical_id",
        "old_claim_ledger_entry_id",
        "new_claim_ledger_entry_id",
        "published_answer_id",
        "propagation_kind",
        "status",
        "details",
        "created_at",
    ):
        assert f in item, f"missing field {f!r} in item: {item!r}"

    assert item["id"] == str(prop_id)
    assert item["source_loss_event_id"] == str(sle_id)
    assert item["claim_logical_id"] == str(claim_logical_id)
    assert item["old_claim_ledger_entry_id"] == str(old_entry_id)
    assert item["new_claim_ledger_entry_id"] == str(new_entry_id)
    assert item["published_answer_id"] is None
    assert item["propagation_kind"] == "claim_marked_unverifiable"
    assert item["status"] == "recorded"
    assert isinstance(item["details"], dict)
    # JSONB roundtrip: dict equality regardless of key ordering.
    assert item["details"] == seeded_details
    assert isinstance(item["created_at"], str) and item["created_at"] != ""


# ===========================================================================
# 2 — happy path: published_answer_impacted / recorded
# ===========================================================================
def test_source_loss_propagation_happy_path_published_answer_impacted() -> None:
    """Seed a source_loss_events row, a published_answer chain for the
    task, and a propagation row in ``published_answer_impacted /
    recorded`` form pointing at that published_answer.

    The ``claim_logical_id`` is intentionally NULL for this propagation
    kind — the schema admits it and the propagator writes it that way
    in production (``_insert_published_answer_impacted_record``).
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        pa_id = _create_published_answer(conn, task_id=task_id)

    seeded_details = {
        "service_name": "mvp0_source_loss_propagator_v1",
        "published_answer_id": str(pa_id),
        "loss_kind": "source_deleted",
    }
    with engine.begin() as conn:
        prop_id = _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="published_answer_impacted",
            status="recorded",
            claim_logical_id=None,
            old_claim_ledger_entry_id=None,
            new_claim_ledger_entry_id=None,
            published_answer_id=pa_id,
            details=seeded_details,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(sle_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["source_loss_event_id"] == str(sle_id)
    items = body["items"]
    assert len(items) == 1

    item = items[0]
    assert item["id"] == str(prop_id)
    assert item["source_loss_event_id"] == str(sle_id)
    assert item["claim_logical_id"] is None
    assert item["old_claim_ledger_entry_id"] is None
    assert item["new_claim_ledger_entry_id"] is None
    assert item["published_answer_id"] == str(pa_id)
    assert item["propagation_kind"] == "published_answer_impacted"
    assert item["status"] == "recorded"
    assert item["details"] == seeded_details


# ===========================================================================
# 3 — source_loss_event exists, no propagation rows -> 200 items=[]
# ===========================================================================
def test_source_loss_propagation_event_exists_no_rows_returns_empty_items() -> None:
    """Race window between POST /api/v1/source-loss-events and the
    worker's propagator: the SLE row exists, but the propagator has
    not yet written any propagation rows. The endpoint MUST return
    200 with ``items=[]`` rather than 404.

    PHASE_8_6_PLAN.md §9 calls out this race explicitly.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(sle_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source_loss_event_id"] == str(sle_id)
    assert body["items"] == []


# ===========================================================================
# 4 — unknown source_loss_event -> 404 RESOURCE_NOT_FOUND with full details
# ===========================================================================
def test_source_loss_propagation_404_for_unknown_source_loss_event() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)
    bogus = uuid.uuid4()
    resp = client.get(_endpoint(bogus))

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "source_loss_events"
    assert details.get("id") == str(bogus)


# ===========================================================================
# 5 — filter propagation_kind returns only matching rows
# ===========================================================================
def test_source_loss_propagation_filter_propagation_kind() -> None:
    """Seed several propagation rows with different kinds for the same
    source_loss_event and verify that the propagation_kind filter
    isolates exactly the requested kind.

    Inserted seed:
      - 1 claim_marked_unverifiable / recorded   (claim_logical_id set)
      - 1 published_answer_impacted / recorded   (published_answer_id set)
      - 1 no_active_published_answers_impacted / recorded
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        claim_logical_id, old_entry_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        new_entry_id = _append_unverifiable_ledger_entry(
            conn,
            claim_logical_id=claim_logical_id,
            version_no=2,
        )
        pa_id = _create_published_answer(conn, task_id=task_id)

    with engine.begin() as conn:
        claim_prop_id = _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
            claim_logical_id=claim_logical_id,
            old_claim_ledger_entry_id=old_entry_id,
            new_claim_ledger_entry_id=new_entry_id,
            details={"k": "claim"},
        )
        _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="published_answer_impacted",
            status="recorded",
            published_answer_id=pa_id,
            details={"k": "pa"},
        )
        _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="no_active_published_answers_impacted",
            status="recorded",
            details={"k": "no_active"},
        )

    client = TestClient(app)

    # Without filter: 3 rows.
    full = client.get(_endpoint(sle_id))
    assert full.status_code == 200, full.text
    assert len(full.json()["items"]) == 3

    # With propagation_kind=claim_marked_unverifiable: 1 row.
    filtered = client.get(
        _endpoint(sle_id), params={"propagation_kind": "claim_marked_unverifiable"}
    )
    assert filtered.status_code == 200, filtered.text
    body = filtered.json()
    assert body["source_loss_event_id"] == str(sle_id)
    items = body["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(claim_prop_id)
    assert items[0]["propagation_kind"] == "claim_marked_unverifiable"
    assert items[0]["status"] == "recorded"


# ===========================================================================
# 6 — filter status=failed returns only failed rows
# ===========================================================================
def test_source_loss_propagation_filter_status_failed() -> None:
    """Seed three propagation rows: one recorded, one skipped, one
    failed. The partial unique indexes cover only recorded/skipped, so
    the failed row can co-exist with the recorded one even on the
    same (source_loss_event_id, propagation_kind, claim_logical_id)
    tuple in principle — but to keep the seed unambiguous we use
    distinct claim_logical_id values for the three rows.

    The status filter must isolate exactly the failed row.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        # Three distinct claims, each with a v1 verified_fact head, so
        # we can attach a propagation row per claim without violating
        # any partial unique index.
        claim_a, entry_a = _create_logical_claim_with_verified_entry(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        claim_b, entry_b = _create_logical_claim_with_verified_entry(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        claim_c, _entry_c = _create_logical_claim_with_verified_entry(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )
        new_entry_a = _append_unverifiable_ledger_entry(
            conn, claim_logical_id=claim_a, version_no=2
        )
        new_entry_b = _append_unverifiable_ledger_entry(
            conn, claim_logical_id=claim_b, version_no=2
        )

    with engine.begin() as conn:
        _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
            claim_logical_id=claim_a,
            old_claim_ledger_entry_id=entry_a,
            new_claim_ledger_entry_id=new_entry_a,
            details={"st": "recorded"},
        )
        _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="skipped",
            claim_logical_id=claim_b,
            old_claim_ledger_entry_id=entry_b,
            new_claim_ledger_entry_id=None,
            details={"st": "skipped"},
        )
        failed_prop_id = _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="failed",
            claim_logical_id=claim_c,
            old_claim_ledger_entry_id=None,
            new_claim_ledger_entry_id=None,
            details={"st": "failed", "reason": "missing_claim_ledger_entry"},
        )

    client = TestClient(app)

    # Without filter: all three rows.
    full = client.get(_endpoint(sle_id))
    assert full.status_code == 200, full.text
    assert len(full.json()["items"]) == 3

    # With status=failed: only the failed row.
    filtered = client.get(_endpoint(sle_id), params={"status": "failed"})
    assert filtered.status_code == 200, filtered.text
    body = filtered.json()
    items = body["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(failed_prop_id)
    assert items[0]["status"] == "failed"
    assert items[0]["claim_logical_id"] == str(claim_c)


# ===========================================================================
# 7 — limit=1 truncates the response to one item
# ===========================================================================
def test_source_loss_propagation_limit_truncates_response() -> None:
    """Seed at least 2 propagation rows in separate transactions so
    that ``NOW()`` yields distinct ``created_at`` timestamps, then ask
    for ``limit=1`` and assert the response carries exactly the first
    (ASC) row.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )

    # First propagation row in its own transaction.
    with engine.begin() as conn:
        first_id = _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="no_claims_impacted",
            status="recorded",
            details={"i": 1},
        )
    # Second propagation row in a separate transaction so its
    # created_at is strictly greater than the first.
    with engine.begin() as conn:
        _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="no_active_published_answers_impacted",
            status="recorded",
            details={"i": 2},
        )

    client = TestClient(app)

    # Sanity: without limit we get both.
    full = client.get(_endpoint(sle_id))
    assert full.status_code == 200, full.text
    assert len(full.json()["items"]) == 2

    # With limit=1 only the first (ASC by created_at) is returned.
    limited = client.get(_endpoint(sle_id), params={"limit": 1})
    assert limited.status_code == 200, limited.text
    items = limited.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(first_id)


# ===========================================================================
# 8 — invalid filters rejected by validation (400 or 422)
# ===========================================================================
def test_source_loss_propagation_invalid_filters_return_validation_error() -> None:
    """Both ``propagation_kind`` and ``status`` are declared as
    pydantic Literals at the route signature. A value outside the
    declared sets must be rejected by FastAPI's RequestValidationError
    handler before any DB call.

    The repo wires a normalized handler for RequestValidationError that
    returns 400 VALIDATION_ERROR. We accept 422 as well in case the
    handler wiring is bypassed in some future configuration; in either
    case the endpoint body must not have executed and no row may have
    been mutated (the read-only invariant test below covers that
    independently).
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )

    client = TestClient(app)

    # Invalid propagation_kind.
    r1 = client.get(
        _endpoint(sle_id), params={"propagation_kind": "not_a_real_kind"}
    )
    assert r1.status_code in (400, 422), r1.text
    if r1.status_code == 400:
        err = _err(r1.json())
        assert err["code"] == "VALIDATION_ERROR"

    # Invalid status.
    r2 = client.get(_endpoint(sle_id), params={"status": "not_a_real_status"})
    assert r2.status_code in (400, 422), r2.text
    if r2.status_code == 400:
        err = _err(r2.json())
        assert err["code"] == "VALIDATION_ERROR"


# ===========================================================================
# 9 — read-only invariant: no count drift, seeded rows unchanged
# ===========================================================================
def test_source_loss_propagation_endpoint_is_read_only() -> None:
    """The GET endpoint MUST NOT mutate any DB row. We snapshot row
    counts on every relevant table AFTER seeding the test rows, hit
    the endpoint several times (including a 404 path and filter
    variants), and assert the snapshot is identical afterward. We
    also assert the seeded propagation row is byte-for-byte unchanged.

    Tables in the snapshot, per the block prompt:
      - published_answer_lifecycle_events
      - source_loss_events
      - source_loss_propagation_records
      - published_answers
      - claim_ledger_entries
      - claim_lineage
      - audit_records

    The seed itself is NOT counted as a mutation: we snapshot AFTER
    the seed transactions commit.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        claim_logical_id, old_entry_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        new_entry_id = _append_unverifiable_ledger_entry(
            conn,
            claim_logical_id=claim_logical_id,
            version_no=2,
        )

    seeded_details = {"k": "v", "n": 42}
    with engine.begin() as conn:
        prop_id = _insert_propagation_record(
            conn,
            source_loss_event_id=sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
            claim_logical_id=claim_logical_id,
            old_claim_ledger_entry_id=old_entry_id,
            new_claim_ledger_entry_id=new_entry_id,
            details=seeded_details,
        )

    # Snapshot BEFORE the GETs (i.e. after all seed transactions).
    with engine.connect() as conn:
        before_counts = _snapshot_all_counts(conn)
        before_row = _fetch_propagation_row(conn, prop_id=prop_id)

    client = TestClient(app)

    # Hit the endpoint multiple times across happy paths and a 404 path.
    r1 = client.get(_endpoint(sle_id))
    assert r1.status_code == 200, r1.text
    r2 = client.get(
        _endpoint(sle_id),
        params={"propagation_kind": "claim_marked_unverifiable"},
    )
    assert r2.status_code == 200, r2.text
    r3 = client.get(_endpoint(sle_id), params={"status": "recorded"})
    assert r3.status_code == 200, r3.text
    r4 = client.get(_endpoint(sle_id), params={"limit": 1})
    assert r4.status_code == 200, r4.text
    r5 = client.get(_endpoint(uuid.uuid4()))
    assert r5.status_code == 404, r5.text

    # Snapshot AFTER the GETs.
    with engine.connect() as conn:
        after_counts = _snapshot_all_counts(conn)
        after_row = _fetch_propagation_row(conn, prop_id=prop_id)

    assert after_counts == before_counts, (
        "row counts drifted after read-only GETs; "
        f"before={before_counts!r}, after={after_counts!r}"
    )
    assert after_row == before_row, (
        "source_loss_propagation_records row mutated after read-only GETs; "
        f"before={before_row!r}, after={after_row!r}"
    )
