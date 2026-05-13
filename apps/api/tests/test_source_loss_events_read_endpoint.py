"""API tests for the source_loss_events read endpoint
(Phase 8.6 — Block 8.6B).

Endpoint exercised:
  GET /api/v1/source-loss-events/{source_loss_event_id}

Coverage map (5 scenarios required by the block prompt):

  1. test_source_loss_event_read_happy_path
  2. test_source_loss_event_read_404_for_unknown_id
  3. test_source_loss_event_read_event_payload_default_empty_dict
  4. test_source_loss_event_read_is_read_only
  5. test_source_loss_event_read_nullable_fields_serialized_as_null

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    resolves to apps/api/app, so ``from app.main import app`` and
    ``from app.db import get_engine`` are the canonical imports — same
    pattern used by other 8.5 / 8.6A API test modules.
  - We do NOT touch Redis: the endpoint is strictly read-only and does
    not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code: this is a pure API test module.
  - We seed directly into the DB the minimal rows needed for each
    scenario (no producer endpoint, no consumer, no propagator).
  - The ``source_loss_events`` table is APPEND-ONLY via trigger, but
    the trigger rejects only UPDATE / DELETE — plain INSERTs are the
    normal write path (it is exactly how the API producer writes rows
    in production). Seeding via INSERT is therefore legitimate.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe).
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
    endpoint needs a real DB to seed the evidence_span chain and the
    ``source_loss_events`` row. No Redis is required.
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
    return f"/api/v1/source-loss-events/{source_loss_event_id}"


def _normalize_jsonb(value: Any) -> Any:
    """Normalize a JSONB column read into a Python object.

    psycopg (3.x) returns JSONB columns as native Python values, but on
    some driver / pool combinations the value may surface as a JSON
    string. We accept both forms to keep the tests robust.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# DB seeding helpers (no consumer, no Redis, no worker modules)
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
                {"t": tenant_id, "n": f"source-loss-read-test-{uuid.uuid4()}"},
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

    Returns a dict with:
      document_id, document_version_id, document_chunk_id,
      evidence_span_id, quote, chunk_text.

    Pattern mirrors apps/api/tests/test_source_loss_endpoint.py.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source loss read API test marker {marker}. "
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
        "quote": quote,
        "chunk_text": chunk_text,
    }


def _insert_source_loss_event_row(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID,
    document_chunk_id: uuid.UUID | None,
    document_version_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    loss_kind: str,
    loss_reason: str,
    detected_by: str,
    event_payload: dict[str, Any],
    idempotency_key: str,
) -> uuid.UUID:
    """Insert one ``source_loss_events`` row directly via SQL.

    The table is APPEND-ONLY via the shared ``reject_modify_append_only``
    trigger; the trigger only blocks UPDATE / DELETE, so plain INSERTs
    are the normal write path. This is the same idiom the API producer
    uses (``_insert_source_loss_event`` in
    ``apps/api/app/routes/source_loss.py``) and that the worker
    propagator does NOT use (it only reads source_loss_events).
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
                :id, :tenant_id, :project_id, :task_id,
                :evidence_span_id, :document_chunk_id,
                :document_version_id, :document_id,
                :loss_kind, :loss_reason, :detected_by,
                CAST(:event_payload AS JSONB), :idempotency_key
            )
            """
        ),
        {
            "id": new_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "task_id": task_id,
            "evidence_span_id": evidence_span_id,
            "document_chunk_id": document_chunk_id,
            "document_version_id": document_version_id,
            "document_id": document_id,
            "loss_kind": loss_kind,
            "loss_reason": loss_reason,
            "detected_by": detected_by,
            "event_payload": json.dumps(event_payload, sort_keys=True),
            "idempotency_key": idempotency_key,
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
# Whitelist of tables we are willing to count via _count_table.
# Hardcoded to avoid any SQL injection vector even though this is test
# code: the table name is interpolated into the query, but only from
# this fixed set. Mirrors the pattern adopted by
# test_source_loss_endpoint.py::_count_table and
# test_published_answer_lifecycle_events_endpoint.py.
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


def _fetch_source_loss_event_row(
    conn: Connection, *, source_loss_event_id: uuid.UUID
) -> dict[str, Any]:
    """Read back the persisted row for byte-level comparison.

    Used by the read-only invariant test: we snapshot the row before
    and after the GETs and assert they are byte-for-byte identical.
    """
    row = conn.execute(
        text(
            """
            SELECT
              id, tenant_id, project_id, task_id,
              evidence_span_id, document_chunk_id,
              document_version_id, document_id,
              loss_kind, loss_reason, detected_by,
              event_payload, idempotency_key, created_at
            FROM source_loss_events
            WHERE id = :id
            """
        ),
        {"id": source_loss_event_id},
    ).one()
    m = row._mapping
    # Normalize event_payload to a dict so equality comparison is
    # stable across driver variants that may return either dict or
    # JSON string for JSONB columns.
    return {
        "id": uuid.UUID(str(m["id"])),
        "tenant_id": uuid.UUID(str(m["tenant_id"])),
        "project_id": uuid.UUID(str(m["project_id"])) if m["project_id"] is not None else None,
        "task_id": uuid.UUID(str(m["task_id"])) if m["task_id"] is not None else None,
        "evidence_span_id": uuid.UUID(str(m["evidence_span_id"])),
        "document_chunk_id": uuid.UUID(str(m["document_chunk_id"])) if m["document_chunk_id"] is not None else None,
        "document_version_id": uuid.UUID(str(m["document_version_id"])) if m["document_version_id"] is not None else None,
        "document_id": uuid.UUID(str(m["document_id"])) if m["document_id"] is not None else None,
        "loss_kind": str(m["loss_kind"]),
        "loss_reason": str(m["loss_reason"]),
        "detected_by": str(m["detected_by"]),
        "event_payload": _normalize_jsonb(m["event_payload"]),
        "idempotency_key": str(m["idempotency_key"]),
        "created_at": m["created_at"],
    }


# ===========================================================================
# 1 — happy path: full row, task_id NULL, all fields surfaced, event_payload
#     roundtrip
# ===========================================================================
def test_source_loss_event_read_happy_path() -> None:
    """Seed one source_loss_events row mirroring what the API producer
    would persist (task_id NULL, all document_* populated, non-trivial
    event_payload). GET it and assert every field on the response.
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

    idem = f"happy-{_unique_hex()}"
    payload = {"a": 1, "z": 2, "nested": {"k": "v"}}
    with engine.begin() as conn:
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=None,  # by design: producer leaves task_id NULL
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
            loss_kind="quote_mismatch",
            loss_reason="quote no longer matches source",
            detected_by="api",
            event_payload=payload,
            idempotency_key=idem,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(sle_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()

    # Every field declared on SourceLossEventRead must be present in
    # the serialized payload.
    for f in (
        "id",
        "tenant_id",
        "project_id",
        "task_id",
        "evidence_span_id",
        "document_chunk_id",
        "document_version_id",
        "document_id",
        "loss_kind",
        "loss_reason",
        "detected_by",
        "event_payload",
        "idempotency_key",
        "created_at",
    ):
        assert f in body, f"missing field {f!r} in response: {body!r}"

    assert body["id"] == str(sle_id)
    assert body["tenant_id"] == str(tenant_id)
    assert body["project_id"] == str(project_id)
    # CRITICAL: task_id MUST be JSON null (Python None after parsing).
    assert body["task_id"] is None
    assert body["evidence_span_id"] == str(chain["evidence_span_id"])
    assert body["document_chunk_id"] == str(chain["document_chunk_id"])
    assert body["document_version_id"] == str(chain["document_version_id"])
    assert body["document_id"] == str(chain["document_id"])
    assert body["loss_kind"] == "quote_mismatch"
    assert body["loss_reason"] == "quote no longer matches source"
    assert body["detected_by"] == "api"
    assert body["idempotency_key"] == idem

    # event_payload roundtrip: equal to the seeded dict.
    assert isinstance(body["event_payload"], dict)
    assert body["event_payload"] == payload

    # created_at is serialized as a string (Pydantic mode='json' default
    # for datetime). We do not parse it — its presence is enough; the
    # DB row is the authoritative timestamp source.
    assert isinstance(body["created_at"], str)
    assert body["created_at"] != ""


# ===========================================================================
# 2 — unknown id -> 404 RESOURCE_NOT_FOUND with full details
# ===========================================================================
def test_source_loss_event_read_404_for_unknown_id() -> None:
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
# 3 — event_payload default ({}) surfaces as an empty dict
# ===========================================================================
def test_source_loss_event_read_event_payload_default_empty_dict() -> None:
    """The schema column event_payload is NOT NULL DEFAULT '{}'::jsonb.
    Verify that a row seeded with an empty payload surfaces as
    ``{}`` (a dict, not ``null``) in the response.
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

    idem = f"empty-payload-{_unique_hex()}"
    with engine.begin() as conn:
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=None,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
            loss_kind="source_deleted",
            loss_reason="reason-x",
            detected_by="api",
            event_payload={},
            idempotency_key=idem,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(sle_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert "event_payload" in body
    assert body["event_payload"] == {}
    assert isinstance(body["event_payload"], dict)


# ===========================================================================
# 4 — read-only invariant: no count drift on any 8.5 / 8.4 / audit table,
#     and the seeded source_loss_events row is byte-for-byte unchanged
# ===========================================================================
def test_source_loss_event_read_is_read_only() -> None:
    """The GET endpoint MUST NOT mutate any DB row. We snapshot row
    counts on every relevant table AFTER seeding the test row, hit the
    endpoint several times (including a 404 path), and assert the
    snapshot is identical afterward. We also assert the seeded row is
    byte-for-byte unchanged.

    Tables in the snapshot, per the block prompt:
      - published_answer_lifecycle_events
      - source_loss_events
      - source_loss_propagation_records
      - published_answers
      - claim_ledger_entries
      - claim_lineage
      - audit_records

    The seed itself is NOT counted as a mutation: we snapshot AFTER
    the seed transaction commits.
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

    idem = f"readonly-{_unique_hex()}"
    payload = {"k": "v", "n": 42}
    with engine.begin() as conn:
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=None,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
            loss_kind="source_deleted",
            loss_reason="readonly test",
            detected_by="api",
            event_payload=payload,
            idempotency_key=idem,
        )

    # Snapshot BEFORE the GETs (i.e. after the seed).
    with engine.connect() as conn:
        before_counts = _snapshot_all_counts(conn)
        before_row = _fetch_source_loss_event_row(conn, source_loss_event_id=sle_id)

    client = TestClient(app)
    # Hit the endpoint multiple times, including a 404 path. None of
    # them must produce any DB mutation.
    r1 = client.get(_endpoint(sle_id))
    assert r1.status_code == 200, r1.text
    r2 = client.get(_endpoint(sle_id))
    assert r2.status_code == 200, r2.text
    r3 = client.get(_endpoint(uuid.uuid4()))
    assert r3.status_code == 404, r3.text

    # Snapshot AFTER the GETs.
    with engine.connect() as conn:
        after_counts = _snapshot_all_counts(conn)
        after_row = _fetch_source_loss_event_row(conn, source_loss_event_id=sle_id)

    assert after_counts == before_counts, (
        "row counts drifted after read-only GETs; "
        f"before={before_counts!r}, after={after_counts!r}"
    )
    assert after_row == before_row, (
        "source_loss_events row mutated after read-only GETs; "
        f"before={before_row!r}, after={after_row!r}"
    )


# ===========================================================================
# 5 — nullable fields: project_id, task_id, document_chunk_id,
#     document_version_id, document_id all NULL in the row are serialized
#     as JSON null on the response.
# ===========================================================================
def test_source_loss_event_read_nullable_fields_serialized_as_null() -> None:
    """Per migrations/0006_lifecycle.sql, the only NOT NULL columns on
    source_loss_events (besides id, evidence_span_id, loss_kind,
    loss_reason, detected_by, idempotency_key, event_payload,
    created_at) are ``tenant_id``. Every document_* column and
    project_id / task_id is nullable.

    We seed a minimal-valid row where all the nullable columns are
    explicitly NULL and assert each surfaces as JSON ``null`` (Python
    ``None`` after parsing).

    Note on real-world producers:
      - The API producer endpoint (Phase 8.5) DOES populate the
        document_* columns and project_id by deriving them from the
        evidence_span chain; it intentionally leaves only task_id
        NULL. This test exercises the more relaxed schema-level
        contract: the response shape MUST handle NULLs gracefully on
        every nullable column, because (a) the schema admits them, and
        (b) the propagator pseudocode and future worker-originated
        producers may write rows where these columns are NULL.
      - tenant_id is NOT NULL and we never seed it as NULL.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, _project_id, user_id, _task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=_project_id,
            user_id=user_id,
        )

    idem = f"nullable-{_unique_hex()}"
    with engine.begin() as conn:
        sle_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=None,
            task_id=None,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=None,
            document_version_id=None,
            document_id=None,
            loss_kind="source_deleted",
            loss_reason="nullable test",
            detected_by="api",
            event_payload={},
            idempotency_key=idem,
        )

    client = TestClient(app)
    resp = client.get(_endpoint(sle_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["id"] == str(sle_id)
    # tenant_id is NOT NULL on the schema and must always be populated.
    assert body["tenant_id"] == str(tenant_id)
    # The five nullable columns must surface as JSON null.
    assert body["project_id"] is None
    assert body["task_id"] is None
    assert body["document_chunk_id"] is None
    assert body["document_version_id"] is None
    assert body["document_id"] is None
    # evidence_span_id is NOT NULL on the schema; sanity-check.
    assert body["evidence_span_id"] == str(chain["evidence_span_id"])
    # The mandatory text columns are surfaced verbatim.
    assert body["loss_kind"] == "source_deleted"
    assert body["loss_reason"] == "nullable test"
    assert body["detected_by"] == "api"
    assert body["idempotency_key"] == idem
    # event_payload defaulted to {} on the seed.
    assert body["event_payload"] == {}
