"""API tests for the source_loss.detected producer endpoint
(Phase 8.5 — Block 4B-2).

Endpoint exercised:
  POST /api/v1/source-loss-events

Coverage map (11 scenarios required by the block prompt):

   1. test_source_loss_event_happy_path_queues_event_and_persists_row
   2. test_source_loss_event_defaults_are_applied
   3. test_source_loss_event_empty_strings_default_correctly
   4. test_source_loss_event_all_valid_loss_kinds
   5. test_source_loss_event_invalid_loss_kind_rejected_no_xadd
   6. test_source_loss_event_404_for_missing_evidence_span_no_xadd
   7. test_source_loss_event_idempotency_conflict_returns_409_and_no_second_xadd
   8. test_source_loss_event_redis_failure_rolls_back_source_loss_row
   9. test_source_loss_event_no_downstream_mutations
  10. test_source_loss_event_event_payload_json_compact_sorted
  11. test_source_loss_event_event_payload_absent_omits_redis_field

Design notes:
  - This file lives under apps/api/tests/. The Python package `app`
    resolves to apps/api/app, so we import the route module directly
    to monkeypatch its bound symbols.
  - The endpoint module imports get_redis via
    ``from ..redis import get_redis``. That binds the SYMBOL inside
    ``app.routes.source_loss`` at import time, so we must monkeypatch
    it there — patching ``app.redis.get_redis`` has no effect.
  - We do NOT use the session-scoped ``client`` fixture from
    conftest.py: each test instantiates its own TestClient(app) so a
    function-scoped monkeypatch on
    ``app.routes.source_loss.get_redis`` cannot leak across tests.
    This is the same pattern adopted by
    apps/api/tests/test_published_answer_withdrawal_request.py.
  - We do NOT spin up a real Redis. FakeRedis records every xadd call
    so we can assert against the exact stream + fields the producer
    emits.
  - The endpoint performs INSERT into source_loss_events and the
    Redis XADD inside the SAME transaction (option B documented in
    the route module). If the XADD raises, the transaction context
    manager rolls back the INSERT — no orphan row remains. We exercise
    that contract explicitly.
  - We do NOT call propagate_source_loss, handle_source_loss, or any
    worker code. The endpoint contract under test is strictly:
        DB write to source_loss_events + Redis XADD.
  - We do NOT touch claim_ledger_entries, claim_lineage,
    source_loss_propagation_records, published_answers.status, or
    published_answer_lifecycle_events. One of the tests proves the
    endpoint does not either.
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
from app.routes import source_loss as source_loss_route


# ---------------------------------------------------------------------------
# constants under test
# ---------------------------------------------------------------------------
EXPECTED_STREAM = "app.events.source_loss_detected"
EXPECTED_EVENT_TYPE = "source_loss.detected"
EXPECTED_DEFAULT_LOSS_KIND = "source_deleted"
EXPECTED_DEFAULT_LOSS_REASON = "source_loss_reported_via_api"
EXPECTED_DEFAULT_DETECTED_BY = "api"

ENDPOINT = "/api/v1/source-loss-events"

VALID_LOSS_KINDS = (
    "source_deleted",
    "source_access_lost",
    "quote_mismatch",
    "document_replaced",
    "policy_retraction",
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    Mirrors conftest.py's gating, but localized to the DB only: Redis
    is monkeypatched in every test of this module, so its availability
    is irrelevant. We still need the DB to seed the evidence_span chain
    that the endpoint resolves.
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
                {"t": tenant_id, "n": f"source-loss-api-test-{uuid.uuid4()}"},
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
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source loss API test marker {marker}. "
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


def _create_published_answer_for_task(
    conn: Connection,
    *,
    task_id: uuid.UUID,
) -> uuid.UUID:
    """Build the minimal 8.4 chain so a published_answer exists for the
    given task. Used by the "no downstream mutations" test to verify
    that the producer endpoint does NOT touch
    published_answers.status / withdrawn_at / superseded_at.
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
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_source_loss_event(
    conn: Connection, *, source_loss_event_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, project_id, task_id,
                   evidence_span_id, document_chunk_id,
                   document_version_id, document_id,
                   loss_kind, loss_reason, detected_by,
                   event_payload, idempotency_key
            FROM source_loss_events
            WHERE id = :id
            """
        ),
        {"id": source_loss_event_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
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
    }


def _count_source_loss_events(
    conn: Connection,
    *,
    evidence_span_id: uuid.UUID,
    loss_kind: str | None = None,
    idempotency_key: str | None = None,
) -> int:
    """Count source_loss_events rows matching the given filters.

    All parameters are positional via bound params; no string
    interpolation, so no SQL injection vector even though this is test
    code.
    """
    if loss_kind is None and idempotency_key is None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_events
                    WHERE evidence_span_id = :es
                    """
                ),
                {"es": evidence_span_id},
            ).scalar_one()
        )
    if loss_kind is not None and idempotency_key is None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_events
                    WHERE evidence_span_id = :es
                      AND loss_kind        = :lk
                    """
                ),
                {"es": evidence_span_id, "lk": loss_kind},
            ).scalar_one()
        )
    if loss_kind is None and idempotency_key is not None:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM source_loss_events
                    WHERE evidence_span_id = :es
                      AND idempotency_key  = :ik
                    """
                ),
                {"es": evidence_span_id, "ik": idempotency_key},
            ).scalar_one()
        )
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM source_loss_events
                WHERE evidence_span_id = :es
                  AND loss_kind        = :lk
                  AND idempotency_key  = :ik
                """
            ),
            {"es": evidence_span_id, "lk": loss_kind, "ik": idempotency_key},
        ).scalar_one()
    )


# Whitelist of tables we are willing to count via _count_table.
# Hardcoded to avoid any SQL injection vector even though this is test
# code: the table name is interpolated into the query, but only from
# this fixed set.
_COUNTABLE_TABLES = frozenset(
    {
        "claim_ledger_entries",
        "claim_lineage",
        "source_loss_propagation_records",
        "published_answer_lifecycle_events",
        "published_answers",
        "source_loss_events",
    }
)


def _count_table(conn: Connection, table_name: str) -> int:
    """Return a global count() of the named table.

    Only accepts a hardcoded whitelist of table names to keep the SQL
    construction safe.
    """
    if table_name not in _COUNTABLE_TABLES:
        raise ValueError(f"refusing to count unknown table: {table_name!r}")
    return int(
        conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
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


# ---------------------------------------------------------------------------
# FakeRedis
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub recording xadd calls.

    Only the surface area used by
    ``source_loss.create_source_loss_event`` is implemented. Other
    Redis methods (ping, xreadgroup, ...) are not exposed: this stub
    is meant to be installed exclusively as the return value of
    ``source_loss_route.get_redis``, never as a global Redis
    replacement.

    When ``fail=True``, ``xadd`` raises RuntimeError to exercise the
    endpoint's rollback path.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.xadd_calls: list[dict[str, Any]] = []

    def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        if self.fail:
            raise RuntimeError("redis down")
        # Defensive copy of fields: tests assert against the snapshot,
        # not against an alias the endpoint might still hold.
        self.xadd_calls.append(
            {
                "stream": stream,
                "fields": dict(fields),
                "maxlen": maxlen,
                "approximate": approximate,
            }
        )
        return "1700000000000-0"


# ---------------------------------------------------------------------------
# common patch helper
# ---------------------------------------------------------------------------
def _install_fake_redis(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
) -> FakeRedis:
    """Patch ``source_loss_route.get_redis`` with a fresh FakeRedis.

    Returns the FakeRedis so tests can assert on its xadd_calls. The
    patched name lives on ``app.routes.source_loss``, not on
    ``app.redis``: the route module captured ``get_redis`` at import
    time via ``from ..redis import get_redis``, so the bound name must
    be replaced on the route module itself.
    """
    fake = FakeRedis(fail=fail)
    monkeypatch.setattr(source_loss_route, "get_redis", lambda: fake)
    return fake


# ===========================================================================
# 1 — happy path: full body, single xadd, full DB row, all fields propagated
# ===========================================================================
def test_source_loss_event_happy_path_queues_event_and_persists_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    body = {
        "evidence_span_id": str(chain["evidence_span_id"]),
        "loss_kind": "quote_mismatch",
        "loss_reason": "quote no longer matches source",
        "idempotency_key": "source-loss-idem-1",
        # Intentionally non-sorted to verify sort_keys at emit time.
        "event_payload": {"z": 2, "a": 1},
    }
    resp = client.post(ENDPOINT, json=body)

    # ---- HTTP envelope ----
    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == EXPECTED_EVENT_TYPE
    assert rb["stream"] == EXPECTED_STREAM
    assert rb["evidence_span_id"] == str(chain["evidence_span_id"])
    assert rb["idempotency_key"] == "source-loss-idem-1"
    response_event_id = uuid.UUID(rb["event_id"])
    response_sle_id = uuid.UUID(rb["source_loss_event_id"])

    # ---- Redis side effect ----
    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    call = fake.xadd_calls[0]
    assert call["stream"] == EXPECTED_STREAM

    fields = call["fields"]
    # All values on the wire are strings (Redis Streams contract).
    for k, v in fields.items():
        assert isinstance(k, str), f"field key not str: {k!r}"
        assert isinstance(v, str), f"field value for {k!r} not str: {v!r}"

    assert fields["event_id"] == str(response_event_id)
    assert fields["event_type"] == EXPECTED_EVENT_TYPE
    assert fields["source_loss_event_id"] == str(response_sle_id)
    assert fields["evidence_span_id"] == str(chain["evidence_span_id"])
    assert fields["idempotency_key"] == "source-loss-idem-1"
    assert fields["tenant_id"] == str(tenant_id)
    assert fields["project_id"] == str(project_id)
    assert fields["document_chunk_id"] == str(chain["document_chunk_id"])
    assert fields["document_version_id"] == str(chain["document_version_id"])
    assert fields["document_id"] == str(chain["document_id"])
    assert fields["loss_kind"] == "quote_mismatch"
    assert fields["loss_reason"] == "quote no longer matches source"
    assert fields["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    # Compact AND sorted keys.
    assert fields["event_payload_json"] == '{"a":1,"z":2}'

    # ---- DB row ----
    with engine.connect() as conn:
        row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert row is not None
    assert row["id"] == response_sle_id
    assert row["tenant_id"] == tenant_id
    assert row["project_id"] == project_id
    # task_id MUST be NULL: the endpoint does not derive it from spans.
    assert row["task_id"] is None
    assert row["evidence_span_id"] == chain["evidence_span_id"]
    assert row["document_chunk_id"] == chain["document_chunk_id"]
    assert row["document_version_id"] == chain["document_version_id"]
    assert row["document_id"] == chain["document_id"]
    assert row["loss_kind"] == "quote_mismatch"
    assert row["loss_reason"] == "quote no longer matches source"
    assert row["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    assert row["idempotency_key"] == "source-loss-idem-1"
    assert row["event_payload"] == {"a": 1, "z": 2}


# ===========================================================================
# 2 — defaults: body with only evidence_span_id
# ===========================================================================
def test_source_loss_event_defaults_are_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        ENDPOINT,
        json={"evidence_span_id": str(chain["evidence_span_id"])},
    )

    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == EXPECTED_EVENT_TYPE
    assert rb["stream"] == EXPECTED_STREAM

    # Generated idempotency_key: non-empty.
    generated_idem = rb["idempotency_key"]
    assert isinstance(generated_idem, str) and generated_idem != ""

    response_sle_id = uuid.UUID(rb["source_loss_event_id"])

    # Exactly one xadd, with the correct defaults on the stream.
    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    assert fields["idempotency_key"] == generated_idem
    assert fields["loss_kind"] == EXPECTED_DEFAULT_LOSS_KIND
    assert fields["loss_reason"] == EXPECTED_DEFAULT_LOSS_REASON
    assert fields["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    # event_payload absent -> Redis field omitted entirely.
    assert "event_payload_json" not in fields

    # DB row reflects defaults.
    with engine.connect() as conn:
        row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert row is not None
    assert row["loss_kind"] == EXPECTED_DEFAULT_LOSS_KIND
    assert row["loss_reason"] == EXPECTED_DEFAULT_LOSS_REASON
    assert row["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    assert row["idempotency_key"] == generated_idem
    assert row["event_payload"] == {}
    assert row["task_id"] is None


# ===========================================================================
# 3 — empty strings on optional fields default correctly
# ===========================================================================
def test_source_loss_event_empty_strings_default_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "loss_reason": "",
            "idempotency_key": "",
        },
    )

    assert resp.status_code == 202, resp.text
    rb = resp.json()

    generated_idem = rb["idempotency_key"]
    assert isinstance(generated_idem, str) and generated_idem != ""

    response_sle_id = uuid.UUID(rb["source_loss_event_id"])

    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    assert fields["idempotency_key"] == generated_idem
    assert fields["loss_kind"] == EXPECTED_DEFAULT_LOSS_KIND
    assert fields["loss_reason"] == EXPECTED_DEFAULT_LOSS_REASON
    assert fields["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    assert "event_payload_json" not in fields

    with engine.connect() as conn:
        row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert row is not None
    assert row["loss_kind"] == EXPECTED_DEFAULT_LOSS_KIND
    assert row["loss_reason"] == EXPECTED_DEFAULT_LOSS_REASON
    assert row["idempotency_key"] == generated_idem


# ===========================================================================
# 4 — all valid loss_kinds accepted
# ===========================================================================
@pytest.mark.parametrize("loss_kind", VALID_LOSS_KINDS)
def test_source_loss_event_all_valid_loss_kinds(
    monkeypatch: pytest.MonkeyPatch,
    loss_kind: str,
) -> None:
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

    _install_fake_redis(monkeypatch)
    client = TestClient(app)

    # Unique idempotency_key per parametrized invocation to avoid the
    # (evidence_span_id, loss_kind, idempotency_key) UNIQUE constraint
    # colliding across parameter cases that happen to share an
    # evidence_span_id (here they don't, but the principle is good
    # hygiene).
    idem = f"loss-kind-{loss_kind}-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "loss_kind": loss_kind,
            "idempotency_key": idem,
        },
    )

    assert resp.status_code == 202, resp.text
    rb = resp.json()
    response_sle_id = uuid.UUID(rb["source_loss_event_id"])

    with engine.connect() as conn:
        row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert row is not None
    assert row["loss_kind"] == loss_kind
    assert row["idempotency_key"] == idem


# ===========================================================================
# 5 — invalid loss_kind rejected, no xadd, no DB row
# ===========================================================================
def test_source_loss_event_invalid_loss_kind_rejected_no_xadd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    idem = f"invalid-kind-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "loss_kind": "not_valid",
            "idempotency_key": idem,
        },
    )

    # The repo wires a normalized handler for RequestValidationError
    # that returns 400 VALIDATION_ERROR. We accept 422 as well in case
    # the handler wiring is bypassed in some future configuration; in
    # both cases the body must NOT have produced a Redis publish nor a
    # DB row.
    assert resp.status_code in (400, 422), resp.text
    if resp.status_code == 400:
        err = _err(resp.json())
        assert err["code"] == "VALIDATION_ERROR"

    # No xadd happened: Pydantic validation runs BEFORE the route body.
    assert fake.xadd_calls == []

    with engine.connect() as conn:
        # Filter by the unique idempotency_key we just submitted; no
        # row for it must exist on any evidence_span / loss_kind.
        n = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_loss_events WHERE idempotency_key = :ik"
                ),
                {"ik": idem},
            ).scalar_one()
        )
        assert n == 0


# ===========================================================================
# 6 — 404 on unknown evidence_span; no xadd
# ===========================================================================
def test_source_loss_event_404_for_missing_evidence_span_no_xadd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_if_db_unreachable()

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    bogus = uuid.uuid4()
    idem = f"missing-span-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(bogus),
            "idempotency_key": idem,
        },
    )

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "evidence_spans"
    assert details.get("id") == str(bogus)

    # The endpoint must short-circuit BEFORE any Redis interaction.
    assert fake.xadd_calls == []

    engine = get_engine()
    with engine.connect() as conn:
        n = int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM source_loss_events WHERE idempotency_key = :ik"
                ),
                {"ik": idem},
            ).scalar_one()
        )
        assert n == 0


# ===========================================================================
# 7 — idempotency conflict returns 409 and no second xadd
# ===========================================================================
def test_source_loss_event_idempotency_conflict_returns_409_and_no_second_xadd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    idem = f"conflict-{_unique_hex()}"
    body = {
        "evidence_span_id": str(chain["evidence_span_id"]),
        "loss_kind": "source_deleted",
        "idempotency_key": idem,
    }

    # First call: 202, one xadd, one DB row.
    r1 = client.post(ENDPOINT, json=body)
    assert r1.status_code == 202, r1.text
    assert len(fake.xadd_calls) == 1

    # Second call with same (evidence_span_id, loss_kind, idempotency_key).
    r2 = client.post(ENDPOINT, json=body)
    assert r2.status_code == 409, r2.text
    err = _err(r2.json())
    assert err["code"] == "RESOURCE_CONFLICT"
    details = err.get("details") or {}
    assert details.get("resource") == "source_loss_events"
    assert details.get("evidence_span_id") == str(chain["evidence_span_id"])
    assert details.get("loss_kind") == "source_deleted"
    assert details.get("idempotency_key") == idem

    # Crucial invariant: the IntegrityError short-circuits the XADD;
    # the second call must NOT have published.
    assert len(fake.xadd_calls) == 1

    with engine.connect() as conn:
        n = _count_source_loss_events(
            conn,
            evidence_span_id=chain["evidence_span_id"],
            loss_kind="source_deleted",
            idempotency_key=idem,
        )
        assert n == 1


# ===========================================================================
# 8 — Redis failure rolls back the source_loss_events row
# ===========================================================================
def test_source_loss_event_redis_failure_rolls_back_source_loss_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    # FakeRedis configured to raise on xadd. The endpoint inserts the
    # row INSIDE the transaction and XADDs BEFORE commit; the
    # transaction context manager rolls back on exception, so no row
    # must remain.
    _install_fake_redis(monkeypatch, fail=True)
    client = TestClient(app)

    idem = f"redis-fail-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "idempotency_key": idem,
        },
    )

    assert resp.status_code == 500, resp.text
    err = _err(resp.json())
    assert err["code"] == "INTERNAL_ERROR"
    details = err.get("details") or {}
    assert details.get("stream") == EXPECTED_STREAM

    with engine.connect() as conn:
        # Zero rows for the submitted idempotency_key — rollback worked.
        n = _count_source_loss_events(
            conn,
            evidence_span_id=chain["evidence_span_id"],
            idempotency_key=idem,
        )
        assert n == 0


# ===========================================================================
# 9 — endpoint does NOT mutate downstream tables
# ===========================================================================
def test_source_loss_event_no_downstream_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        pa_id = _create_published_answer_for_task(conn, task_id=task_id)

    # Snapshot BEFORE the POST.
    with engine.connect() as conn:
        before = {
            "claim_ledger_entries": _count_table(conn, "claim_ledger_entries"),
            "claim_lineage": _count_table(conn, "claim_lineage"),
            "source_loss_propagation_records": _count_table(
                conn, "source_loss_propagation_records"
            ),
            "published_answer_lifecycle_events": _count_table(
                conn, "published_answer_lifecycle_events"
            ),
            "published_answers": _count_table(conn, "published_answers"),
            "source_loss_events": _count_table(conn, "source_loss_events"),
        }
        pa_before = _fetch_published_answer(conn, published_answer_id=pa_id)
    assert str(pa_before["status"]) == "published"
    assert pa_before["withdrawn_at"] is None
    assert pa_before["superseded_at"] is None
    assert pa_before["superseded_by_id"] is None

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    idem = f"no-mutation-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "idempotency_key": idem,
        },
    )
    assert resp.status_code == 202, resp.text
    assert len(fake.xadd_calls) == 1  # XADD happened.

    # Snapshot AFTER the POST.
    with engine.connect() as conn:
        after = {
            "claim_ledger_entries": _count_table(conn, "claim_ledger_entries"),
            "claim_lineage": _count_table(conn, "claim_lineage"),
            "source_loss_propagation_records": _count_table(
                conn, "source_loss_propagation_records"
            ),
            "published_answer_lifecycle_events": _count_table(
                conn, "published_answer_lifecycle_events"
            ),
            "published_answers": _count_table(conn, "published_answers"),
            "source_loss_events": _count_table(conn, "source_loss_events"),
        }
        pa_after = _fetch_published_answer(conn, published_answer_id=pa_id)

    # Only source_loss_events grew, by exactly 1.
    assert after["source_loss_events"] == before["source_loss_events"] + 1
    assert after["claim_ledger_entries"] == before["claim_ledger_entries"]
    assert after["claim_lineage"] == before["claim_lineage"]
    assert (
        after["source_loss_propagation_records"]
        == before["source_loss_propagation_records"]
    )
    assert (
        after["published_answer_lifecycle_events"]
        == before["published_answer_lifecycle_events"]
    )
    assert after["published_answers"] == before["published_answers"]

    # The published_answer row is byte-for-byte the same.
    assert str(pa_after["status"]) == "published"
    assert pa_after["withdrawn_at"] is None
    assert pa_after["superseded_at"] is None
    assert pa_after["superseded_by_id"] is None


# ===========================================================================
# 10 — event_payload_json is compact + sort_keys (both on Redis and in DB)
# ===========================================================================
def test_source_loss_event_event_payload_json_compact_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    idem = f"payload-sort-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "idempotency_key": idem,
            # Intentionally inserted in non-sorted order so that a
            # producer that did NOT sort would emit '{"z":2,"a":1}'.
            "event_payload": {"z": 2, "a": 1},
        },
    )
    assert resp.status_code == 202, resp.text

    response_sle_id = uuid.UUID(resp.json()["source_loss_event_id"])

    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    # Compact (no spaces around separators) AND sorted keys.
    assert fields["event_payload_json"] == '{"a":1,"z":2}'

    # Persisted JSONB carries the same payload (as a dict, regardless
    # of in-memory key ordering).
    with engine.connect() as conn:
        row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert row is not None
    assert row["event_payload"] == {"a": 1, "z": 2}


# ===========================================================================
# 11 — event_payload absent omits the Redis event_payload_json field
# ===========================================================================
def test_source_loss_event_event_payload_absent_omits_redis_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fake = _install_fake_redis(monkeypatch)
    client = TestClient(app)

    idem = f"payload-absent-{_unique_hex()}"
    resp = client.post(
        ENDPOINT,
        json={
            "evidence_span_id": str(chain["evidence_span_id"]),
            "idempotency_key": idem,
        },
    )
    assert resp.status_code == 202, resp.text

    response_sle_id = uuid.UUID(resp.json()["source_loss_event_id"])

    assert len(fake.xadd_calls) == 1
    fields = fake.xadd_calls[0]["fields"]
    # event_payload absent -> the Redis field is omitted entirely
    # (not set to "" or "null").
    assert "event_payload_json" not in fields

    # DB row still has event_payload = {}: the schema column is NOT
    # NULL and defaults to an empty JSON object, and the endpoint
    # serializes {} when the body omits it.
    with engine.connect() as conn:
        row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert row is not None
    assert row["event_payload"] == {}
