"""API tests for the task-level source_loss_events listing endpoint
(Phase 8.6 — Block 8.6D).

Endpoint exercised:
  GET /api/v1/tasks/{task_id}/source-loss-events

Coverage map (8 scenarios required by the block prompt):

  1. test_task_source_loss_events_happy_path_only_claim_evidence_link
  2. test_task_source_loss_events_happy_path_only_task_scope
  3. test_task_source_loss_events_dedup_precedence_task_scope_wins
  4. test_task_source_loss_events_mixed_s1_and_s2
  5. test_task_source_loss_events_task_exists_no_events_returns_empty_items
  6. test_task_source_loss_events_404_for_unknown_task
  7. test_task_source_loss_events_limit_truncates_response
  8. test_task_source_loss_events_endpoint_is_read_only

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    resolves to apps/api/app, so ``from app.main import app`` and
    ``from app.db import get_engine`` are the canonical imports —
    same pattern used by all other 8.5 / 8.6 API test modules.
  - We do NOT touch Redis: the endpoint is strictly read-only and
    does not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code (no ``propagate_source_loss``,
    no dispatcher, no consumer). All rows are seeded directly via
    SQL — exactly what the worker propagator and the API producer do
    in production, minus the orchestration.
  - Helpers are LOCAL to this file (we copy the seed primitives from
    the 8.6B / 8.6C / propagator test modules); the file is
    autonomous.
  - All append-only tables involved (``source_loss_events``,
    ``claim_ledger_entries``) accept INSERT — the
    ``reject_modify_append_only`` trigger only blocks UPDATE / DELETE.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe).
  - ``logical_claims`` has a UNIQUE on
    ``(task_id, canonical_claim_hash)`` (lc_task_canonical_uq); the
    seed uses ``_unique_hex()`` for every claim to avoid collisions.
  - ``claim_evidence_links`` has CHECK ``cel_origin_xor`` requiring
    ``evidence_span_id IS NOT NULL AND retrieved_source_span_id IS
    NULL``. We honor it.
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
    endpoint needs a real DB to seed the task, the evidence_span
    chain, and the source_loss_events / logical_claims /
    claim_evidence_links rows. No Redis is required.
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
    """Extract the normalized error envelope from a NormalizedError
    response.

    Envelope shape (from
    packages/shared/evidencefirst_shared/errors.py):
        {"error": {"code": "...", "message": "...", "details": {...}, ...}}
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


def _endpoint(task_id: uuid.UUID) -> str:
    return f"/api/v1/tasks/{task_id}/source-loss-events"


# ---------------------------------------------------------------------------
# DB seeding helpers — tenant / project / task
# ---------------------------------------------------------------------------
def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per
    invocation.

    Returns (tenant_id, project_id, user_id, task_id).
    """
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
        row = conn.execute(
            text("SELECT id FROM tenants WHERE slug = 'dev'")
        ).one()
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
                {"t": tenant_id, "n": f"task-source-loss-test-{uuid.uuid4()}"},
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

    Returns a dict with the chain ids; mirrors the helpers in
    apps/api/tests/test_source_loss_endpoint.py /
    apps/api/tests/test_source_loss_events_read_endpoint.py /
    apps/api/tests/test_source_loss_propagation_endpoint.py.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Task source loss API test marker {marker}. "
        f"This sentence contains the digit 7 and a {quote}."
    )
    content_hash_payload = hashlib.sha256(
        chunk_text.encode("utf-8")
    ).hexdigest()
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
                    # Make the content_hash unique per invocation so
                    # the global UNIQUE (content_hash, hash_algorithm)
                    # WHERE tenant_namespace_id IS NULL never collides
                    # on a long-running dev DB.
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
                    "th": hashlib.sha256(
                        chunk_text.encode("utf-8")
                    ).hexdigest(),
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
                    "th": hashlib.sha256(
                        chunk_text.encode("utf-8")
                    ).hexdigest(),
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
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID,
    document_chunk_id: uuid.UUID | None,
    document_version_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    loss_kind: str = "source_deleted",
    loss_reason: str = "unit-test loss reason",
    detected_by: str = "api",
    event_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    """Insert one ``source_loss_events`` row directly via SQL.

    The table is APPEND-ONLY via trigger; INSERT is the normal write
    path. ``task_id`` is a parameter (not forced to NULL) so the same
    helper covers both S1 (task-scoped) and S2 (NULL by design) seeds.
    """
    if event_payload is None:
        event_payload = {}
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
            "idempotency_key": idempotency_key or _unique_hex(),
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB seeding helpers — logical_claims + claim_ledger_entries +
# claim_evidence_links
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

    The ledger entry's support_scope and user_provided_dependency are
    set to ``supported_by_user_corpus_only`` (one of the valid values
    of the CHECK declared in 0004_claim_ledger.sql).
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
                    # Unique per call to avoid lc_task_canonical_uq
                    # collisions on long-running dev DBs.
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


def _insert_claim_evidence_link(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    link_role: str = "primary_support",
) -> uuid.UUID:
    """Insert one claim_evidence_links row binding a claim's verified
    ledger entry to an evidence_span.

    Honors the CHECK ``cel_origin_xor`` (evidence_span_id NOT NULL,
    retrieved_source_span_id NULL) and the composite FK
    ``cel_entry_logical_consistency`` by passing both
    ``claim_ledger_entry_id`` and ``claim_logical_id`` from the same
    ledger row.
    """
    new_id = uuid.uuid4()
    conn.execute(
        text(
            """
            INSERT INTO claim_evidence_links
                (id, claim_logical_id, claim_ledger_entry_id,
                 evidence_span_id, retrieved_source_span_id,
                 link_role)
            VALUES
                (:id, :lc, :cle, :es, NULL, :role)
            """
        ),
        {
            "id": new_id,
            "lc": claim_logical_id,
            "cle": claim_ledger_entry_id,
            "es": evidence_span_id,
            "role": link_role,
        },
    )
    return new_id


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
# Whitelist of tables we are willing to count via _count_table.
# Hardcoded to avoid any SQL injection vector even though this is test
# code: the table name is interpolated into the query, but only from
# this fixed set. Mirrors the pattern adopted by the 8.6A / 8.6B /
# 8.6C test modules.
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


# ===========================================================================
# 1 — happy path: only S2 (claim_evidence_link)
# ===========================================================================
def test_task_source_loss_events_happy_path_only_claim_evidence_link() -> None:
    """Seed:
      - one task,
      - one evidence_span chain,
      - one source_loss_events row with task_id=NULL,
      - one logical_claim on the task with a v1 verified_fact entry,
      - one claim_evidence_links row binding the claim to the span.

    The endpoint must return one item whose ``impacted_via`` is
    ``"claim_evidence_link"`` and whose ``source_loss_event.task_id``
    is JSON ``null`` (by-design behavior of the Phase 8.5 producer:
    NULL is never camouflaged).
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
            task_id=None,  # by design — NULL must surface as JSON null
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        claim_logical_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _insert_claim_evidence_link(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["task_id"] == str(task_id)
    items = body["items"]
    assert isinstance(items, list)
    assert len(items) == 1

    item = items[0]
    assert "source_loss_event" in item
    assert "impacted_via" in item
    assert item["impacted_via"] == "claim_evidence_link"

    sle = item["source_loss_event"]
    assert sle["id"] == str(sle_id)
    # CRITICAL: the SLE row has task_id=NULL; the endpoint must NOT
    # synthesize the queried task_id into this field even when the
    # event is reached via S2. This is the by-design contract.
    assert sle["task_id"] is None
    assert sle["evidence_span_id"] == str(chain["evidence_span_id"])
    assert sle["tenant_id"] == str(tenant_id)


# ===========================================================================
# 2 — happy path: only S1 (task_scope)
# ===========================================================================
def test_task_source_loss_events_happy_path_only_task_scope() -> None:
    """Seed:
      - one task,
      - one evidence_span chain,
      - one source_loss_events row WITH task_id explicitly set to
        the task,
      - no claim_evidence_links (no logical_claim, no ledger entry).

    The endpoint must return one item whose ``impacted_via`` is
    ``"task_scope"`` and whose ``source_loss_event.task_id`` matches
    the queried task.
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
            task_id=task_id,  # S1: task-scoped row
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["task_id"] == str(task_id)
    items = body["items"]
    assert len(items) == 1

    item = items[0]
    assert item["impacted_via"] == "task_scope"
    sle = item["source_loss_event"]
    assert sle["id"] == str(sle_id)
    assert sle["task_id"] == str(task_id)


# ===========================================================================
# 3 — dedup precedence: same SLE in S1 AND S2 -> impacted_via=task_scope
# ===========================================================================
def test_task_source_loss_events_dedup_precedence_task_scope_wins() -> None:
    """Seed:
      - one task,
      - one evidence_span chain,
      - one source_loss_events row WITH task_id set to the task
        (i.e. S1 hits),
      - one logical_claim on the SAME task with a v1 verified_fact
        entry,
      - one claim_evidence_links binding the claim to the SAME span
        (i.e. S2 hits too).

    The same source_loss_event satisfies both S1 and S2. The endpoint
    must return ONE item only, with ``impacted_via='task_scope'``.
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
            task_id=task_id,
            evidence_span_id=chain["evidence_span_id"],
            document_chunk_id=chain["document_chunk_id"],
            document_version_id=chain["document_version_id"],
            document_id=chain["document_id"],
        )
        claim_logical_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _insert_claim_evidence_link(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    items = body["items"]
    # Dedup invariant: exactly one item even though the row appears in
    # both S1 and S2.
    assert len(items) == 1
    item = items[0]
    assert item["source_loss_event"]["id"] == str(sle_id)
    # Precedence: task_scope wins over claim_evidence_link.
    assert item["impacted_via"] == "task_scope"


# ===========================================================================
# 4 — mixed S1 + S2: two distinct events, one per set
# ===========================================================================
def test_task_source_loss_events_mixed_s1_and_s2() -> None:
    """Seed two distinct source_loss_events for the same task:
      - SLE-A: reaches the task via S1 (task_id set, no
        claim_evidence_links seeded for it);
      - SLE-B: reaches the task via S2 only (task_id=NULL,
        evidence_span linked to a logical_claim on the task).

    Both events must appear in the response, with the correct
    ``impacted_via`` value for each.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        # First evidence_span — used by SLE-A (task_scope).
        chain_a = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        # Second evidence_span — used by SLE-B (claim_evidence_link).
        chain_b = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        sle_a_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=chain_a["evidence_span_id"],
            document_chunk_id=chain_a["document_chunk_id"],
            document_version_id=chain_a["document_version_id"],
            document_id=chain_a["document_id"],
        )
        sle_b_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=None,
            evidence_span_id=chain_b["evidence_span_id"],
            document_chunk_id=chain_b["document_chunk_id"],
            document_version_id=chain_b["document_version_id"],
            document_id=chain_b["document_id"],
        )
        claim_logical_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _insert_claim_evidence_link(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain_b["evidence_span_id"],
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    items = body["items"]
    assert len(items) == 2

    # Build a map id -> impacted_via for stable assertions independent
    # of the (created_at, id) ordering.
    by_id: dict[str, str] = {
        item["source_loss_event"]["id"]: item["impacted_via"] for item in items
    }
    assert by_id[str(sle_a_id)] == "task_scope"
    assert by_id[str(sle_b_id)] == "claim_evidence_link"


# ===========================================================================
# 5 — task exists, no events: 200 with items=[]
# ===========================================================================
def test_task_source_loss_events_task_exists_no_events_returns_empty_items() -> None:
    """A freshly created task with no source_loss_events (neither
    task-scoped nor reachable via claim_evidence_links) must return
    200 with ``items=[]`` rather than 404.
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _, _, _, task_id = _seeded_dev(conn)

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["items"] == []


# ===========================================================================
# 6 — unknown task -> 404 RESOURCE_NOT_FOUND with full details
# ===========================================================================
def test_task_source_loss_events_404_for_unknown_task() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)
    bogus = uuid.uuid4()
    resp = client.get(_endpoint(bogus))

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "task_masters"
    assert details.get("id") == str(bogus)


# ===========================================================================
# 7 — limit=1 truncates the response to one item
# ===========================================================================
def test_task_source_loss_events_limit_truncates_response() -> None:
    """Seed two events visible from the task (both via S1 for
    simplicity), in separate transactions so ``NOW()`` yields distinct
    ``created_at`` timestamps. Ask for ``limit=1`` and assert exactly
    one item is returned (the first by created_at ASC).
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain_a = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        chain_b = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )

    # Insert the two events in separate transactions so that NOW()
    # advances and the ASC ordering by created_at is well-defined.
    with engine.begin() as conn:
        first_id = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=chain_a["evidence_span_id"],
            document_chunk_id=chain_a["document_chunk_id"],
            document_version_id=chain_a["document_version_id"],
            document_id=chain_a["document_id"],
        )
    with engine.begin() as conn:
        _ = _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=chain_b["evidence_span_id"],
            document_chunk_id=chain_b["document_chunk_id"],
            document_version_id=chain_b["document_version_id"],
            document_id=chain_b["document_id"],
        )

    client = TestClient(app)

    # Sanity: without limit we get both.
    full = client.get(_endpoint(task_id))
    assert full.status_code == 200, full.text
    assert len(full.json()["items"]) == 2

    # With limit=1 only the first (ASC by created_at) is returned.
    limited = client.get(_endpoint(task_id), params={"limit": 1})
    assert limited.status_code == 200, limited.text
    items = limited.json()["items"]
    assert len(items) == 1
    assert items[0]["source_loss_event"]["id"] == str(first_id)


# ===========================================================================
# 8 — read-only invariant: no count drift across multiple GETs (200 + 404)
# ===========================================================================
def test_task_source_loss_events_endpoint_is_read_only() -> None:
    """The GET endpoint MUST NOT mutate any DB row. We snapshot row
    counts on every relevant table AFTER seeding the test data, hit
    the endpoint multiple times (including a 404 path), and assert
    the snapshot is identical afterward.

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
        chain_a = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        chain_b = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        # SLE-A: task_scope
        _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=chain_a["evidence_span_id"],
            document_chunk_id=chain_a["document_chunk_id"],
            document_version_id=chain_a["document_version_id"],
            document_id=chain_a["document_id"],
        )
        # SLE-B: claim_evidence_link
        _insert_source_loss_event_row(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=None,
            evidence_span_id=chain_b["evidence_span_id"],
            document_chunk_id=chain_b["document_chunk_id"],
            document_version_id=chain_b["document_version_id"],
            document_id=chain_b["document_id"],
        )
        claim_logical_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _insert_claim_evidence_link(
            conn,
            claim_logical_id=claim_logical_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain_b["evidence_span_id"],
        )

    # Snapshot BEFORE the GETs (i.e. after all seed transactions).
    with engine.connect() as conn:
        before_counts = _snapshot_all_counts(conn)

    client = TestClient(app)

    # Hit the endpoint multiple times across the happy path, the
    # limit path, and a 404 path. None of them must mutate the DB.
    r1 = client.get(_endpoint(task_id))
    assert r1.status_code == 200, r1.text
    r2 = client.get(_endpoint(task_id), params={"limit": 1})
    assert r2.status_code == 200, r2.text
    r3 = client.get(_endpoint(task_id), params={"limit": 2000})
    assert r3.status_code == 200, r3.text
    r4 = client.get(_endpoint(uuid.uuid4()))
    assert r4.status_code == 404, r4.text

    # Snapshot AFTER the GETs.
    with engine.connect() as conn:
        after_counts = _snapshot_all_counts(conn)

    assert after_counts == before_counts, (
        "row counts drifted after read-only GETs; "
        f"before={before_counts!r}, after={after_counts!r}"
    )
