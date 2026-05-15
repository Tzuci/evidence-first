"""API tests for the source_quality read endpoints (Phase 8.7 — Block F).

Endpoints exercised:

  GET /api/v1/evidence-spans/{evidence_span_id}/source-quality
  GET /api/v1/tasks/{task_id}/source-quality

Coverage map (10 scenarios required by the block prompt):

  1.  test_get_evidence_span_source_quality_happy_path
  2.  test_get_evidence_span_source_quality_multiple_versions_ordered
  3.  test_get_evidence_span_source_quality_no_assessments_returns_empty
  4.  test_get_evidence_span_source_quality_404
  5.  test_get_task_source_quality_happy_path
  6.  test_get_task_source_quality_multiple_versions_latest_summary_counts_latest_only
  7.  test_get_task_source_quality_no_claim_evidence_links
  8.  test_get_task_source_quality_404
  9.  test_read_endpoints_are_read_only
  10. test_limit_bounds_or_limit_behavior

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    resolves to apps/api/app, so ``from app.main import app`` and
    ``from app.db import get_engine`` are the canonical imports — same
    pattern used by all other 8.5 / 8.6 / 8.7 API test modules.
  - We do NOT touch Redis: the endpoints are strictly read-only and
    do not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code (no evaluator, no orchestrator,
    no dispatcher, no consumer). All rows are seeded directly via
    SQL — exactly what the (mock) worker does in production, minus
    the orchestration.
  - Helpers are LOCAL to this file (per the block prompt: no imports
    from other test files). Seed primitives mirror those used in
    apps/api/tests/test_source_loss_propagation_endpoint.py and
    apps/api/tests/test_task_source_loss_events_endpoint.py.
  - All append-only tables involved
    (``source_quality_assessments``, ``claim_ledger_entries``)
    accept INSERT — the shared ``reject_modify_append_only`` trigger
    only blocks UPDATE / DELETE.
  - The partial UNIQUE indexes on
    ``source_quality_assessments`` are scoped per target column;
    inserting multiple versions for the SAME evidence_span_id with
    DIFFERENT (version_no, idempotency_key) is the normal write
    path. Seeds honor that.
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

    Mirrors the gating used by every other 8.5 / 8.6 / 8.7 API test
    module: the endpoints need a real DB to seed the evidence_span
    chain, the logical_claims/claim_evidence_links graph and the
    source_quality_assessments rows. No Redis is required.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; bring up the stack first.")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip(
            "DB unreachable; run `make up` and `make migrate && make seed`."
        )


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


def _span_endpoint(evidence_span_id: uuid.UUID) -> str:
    return f"/api/v1/evidence-spans/{evidence_span_id}/source-quality"


def _task_endpoint(task_id: uuid.UUID) -> str:
    return f"/api/v1/tasks/{task_id}/source-quality"


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
                "SELECT id FROM users WHERE tenant_id = :t "
                "AND email = 'dev@local'"
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
                {"t": tenant_id, "n": f"sqr-test-{uuid.uuid4()}"},
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

    Returns a dict with the chain ids. Mirrors the helper in
    apps/api/tests/test_task_source_loss_events_endpoint.py.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source quality read API test marker {marker}. "
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
# DB seeding helpers — logical_claims + claim_ledger_entries + link
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

    Mirrors the helper used by 8.6C / 8.6D test modules.
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


def _link_claim_to_span(
    conn: Connection,
    *,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    link_role: str = "primary_support",
) -> uuid.UUID:
    """Insert one claim_evidence_links row binding a claim's ledger
    entry to an evidence_span.

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
                 evidence_span_id, retrieved_source_span_id, link_role)
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
# DB seeding helpers — source_quality_assessments
# ---------------------------------------------------------------------------
# Default mock values mirroring what the mock evaluator writes today.
# Tests can override individual fields where needed.
_DEFAULT_SOURCE_TYPE = "user_document"
_DEFAULT_SOURCE_ROLE = "unclear"
_DEFAULT_AUTHORITY_LEVEL = "unknown"
_DEFAULT_INDEPENDENCE_LEVEL = "unknown"
_DEFAULT_FRESHNESS = "undated"
_DEFAULT_RELEVANCE = "direct_support"
_DEFAULT_EXTRACT_QUALITY = "exact_quote_match"
_DEFAULT_CONTRADICTION_STATUS = "unchecked"
_DEFAULT_OVERALL_QUALITY = "unknown"
_DEFAULT_CONFIDENCE = 0.5
_DEFAULT_EVALUATOR_NAME = "mock_source_quality_evaluator"
_DEFAULT_EVALUATOR_VERSION = "0.1.0"
_DEFAULT_POLICY_NAME = "mvp0_mock_source_quality"
_DEFAULT_POLICY_VERSION = "0.1.0"


def _insert_source_quality_assessment(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    evidence_span_id: uuid.UUID,
    version_no: int = 1,
    overall_quality: str = _DEFAULT_OVERALL_QUALITY,
    confidence: float | None = _DEFAULT_CONFIDENCE,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert ONE source_quality_assessments row directly via SQL.

    We do NOT pass an explicit ``id`` so the table's DEFAULT
    ``app_new_uuid()`` is exercised — coherent with the 8.7B
    micro-fix that confirmed app_new_uuid as the canonical id
    generator.

    The seed targets ``evidence_span_id`` only (the other two
    target columns are NULL, honoring ``sqa_target_xor``). The
    block prompt makes this an explicit constraint: 8.7E only ever
    writes evidence_span-targeted rows.

    Caller-controllable fields (the rest stay at the mock defaults):
      - version_no
      - overall_quality
      - confidence
      - idempotency_key (default: fresh hex per call)
      - payload (default: ``{}``)
    """
    eff_payload = payload if payload is not None else {}
    eff_key = idempotency_key if idempotency_key is not None else _unique_hex()
    row = conn.execute(
        text(
            """
            INSERT INTO source_quality_assessments (
                tenant_id, project_id,
                evidence_span_id, document_chunk_id, document_id,
                version_no,
                source_type, source_role, authority_level, independence_level,
                freshness, relevance, extract_quality, contradiction_status,
                overall_quality, confidence,
                evaluator_name, evaluator_version,
                policy_name, policy_version,
                idempotency_key, payload
            ) VALUES (
                :tenant_id, :project_id,
                :evidence_span_id, NULL, NULL,
                :version_no,
                :source_type, :source_role, :authority_level, :independence_level,
                :freshness, :relevance, :extract_quality, :contradiction_status,
                :overall_quality, :confidence,
                :evaluator_name, :evaluator_version,
                :policy_name, :policy_version,
                :idempotency_key, CAST(:payload AS JSONB)
            )
            RETURNING id
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "evidence_span_id": evidence_span_id,
            "version_no": version_no,
            "source_type": _DEFAULT_SOURCE_TYPE,
            "source_role": _DEFAULT_SOURCE_ROLE,
            "authority_level": _DEFAULT_AUTHORITY_LEVEL,
            "independence_level": _DEFAULT_INDEPENDENCE_LEVEL,
            "freshness": _DEFAULT_FRESHNESS,
            "relevance": _DEFAULT_RELEVANCE,
            "extract_quality": _DEFAULT_EXTRACT_QUALITY,
            "contradiction_status": _DEFAULT_CONTRADICTION_STATUS,
            "overall_quality": overall_quality,
            "confidence": confidence,
            "evaluator_name": _DEFAULT_EVALUATOR_NAME,
            "evaluator_version": _DEFAULT_EVALUATOR_VERSION,
            "policy_name": _DEFAULT_POLICY_NAME,
            "policy_version": _DEFAULT_POLICY_VERSION,
            "idempotency_key": eff_key,
            "payload": json.dumps(eff_payload, sort_keys=True),
        },
    ).first()
    return uuid.UUID(str(row[0]))


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
# Whitelist of tables we are willing to count via _count_table.
# Hardcoded to avoid any SQL injection vector even though this is test
# code: the table name is interpolated into the query, but only from
# this fixed set. Mirrors the pattern adopted by all 8.6 / 8.7 test
# modules.
_COUNTABLE_TABLES = frozenset(
    {
        "source_quality_assessments",
        "audit_records",
        "claim_ledger_entries",
        "claim_evidence_links",
        "logical_claims",
        "final_gate_reports",
        "published_answers",
        "source_loss_events",
        "source_loss_propagation_records",
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


# Required fields that SourceQualityAssessmentRead surfaces. Used by
# the happy-path test to make sure no field is silently dropped.
_REQUIRED_ASSESSMENT_FIELDS = (
    "id",
    "tenant_id",
    "project_id",
    "evidence_span_id",
    "document_chunk_id",
    "document_id",
    "version_no",
    "source_type",
    "source_role",
    "authority_level",
    "independence_level",
    "freshness",
    "relevance",
    "extract_quality",
    "contradiction_status",
    "overall_quality",
    "confidence",
    "evaluator_name",
    "evaluator_version",
    "policy_name",
    "policy_version",
    "idempotency_key",
    "payload",
    "created_at",
)


# ===========================================================================
# 1 — evidence_span endpoint, happy path: full row, payload roundtrip,
#     latest_assessment == items[0]
# ===========================================================================
def test_get_evidence_span_source_quality_happy_path() -> None:
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

    seeded_payload = {"k": "v", "n": 42, "nested": {"deep": True}}
    seeded_idem = f"happy-{_unique_hex()}"
    with engine.begin() as conn:
        assessment_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            overall_quality="unknown",
            confidence=0.5,
            idempotency_key=seeded_idem,
            payload=seeded_payload,
        )

    client = TestClient(app)
    resp = client.get(_span_endpoint(chain["evidence_span_id"]))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["evidence_span_id"] == str(chain["evidence_span_id"])

    items = body["items"]
    assert isinstance(items, list)
    assert len(items) == 1
    item = items[0]

    # Every field on SourceQualityAssessmentRead must surface.
    for f in _REQUIRED_ASSESSMENT_FIELDS:
        assert f in item, f"missing field {f!r} in item: {item!r}"

    assert item["id"] == str(assessment_id)
    assert item["tenant_id"] == str(tenant_id)
    assert item["project_id"] == str(project_id)
    assert item["evidence_span_id"] == str(chain["evidence_span_id"])
    # Target XOR: only evidence_span_id is set on the row.
    assert item["document_chunk_id"] is None
    assert item["document_id"] is None
    assert item["version_no"] == 1
    assert item["source_type"] == _DEFAULT_SOURCE_TYPE
    assert item["overall_quality"] == "unknown"
    # confidence is a float in [0, 1].
    assert isinstance(item["confidence"], float)
    assert item["confidence"] == pytest.approx(0.5)
    assert 0.0 <= item["confidence"] <= 1.0
    assert item["idempotency_key"] == seeded_idem
    # JSONB roundtrip.
    assert item["payload"] == seeded_payload

    # latest_assessment is structurally equal to items[0].
    assert body["latest_assessment"] == item


# ===========================================================================
# 2 — evidence_span endpoint, multiple versions: ordered ASC and
#     latest_assessment.version_no is the highest in the returned slice
# ===========================================================================
def test_get_evidence_span_source_quality_multiple_versions_ordered() -> None:
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

    # Insert two versions in separate transactions so that NOW()
    # advances between them. The ordering tested is by version_no
    # primarily, but distinct created_at values make the secondary
    # key meaningful.
    idem_v1 = f"v1-{_unique_hex()}"
    idem_v2 = f"v2-{_unique_hex()}"
    with engine.begin() as conn:
        v1_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            idempotency_key=idem_v1,
        )
    with engine.begin() as conn:
        v2_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=2,
            idempotency_key=idem_v2,
        )

    client = TestClient(app)
    resp = client.get(_span_endpoint(chain["evidence_span_id"]))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    items = body["items"]
    assert len(items) == 2

    # ASC ordering: v1 first, v2 second.
    assert items[0]["id"] == str(v1_id)
    assert items[0]["version_no"] == 1
    assert items[1]["id"] == str(v2_id)
    assert items[1]["version_no"] == 2

    # latest_assessment is the highest version_no in the returned slice.
    assert body["latest_assessment"] is not None
    assert body["latest_assessment"]["version_no"] == 2
    assert body["latest_assessment"]["id"] == str(v2_id)


# ===========================================================================
# 3 — evidence_span endpoint, span exists but no assessments:
#     200 with items=[] and latest_assessment=null
# ===========================================================================
def test_get_evidence_span_source_quality_no_assessments_returns_empty() -> None:
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

    client = TestClient(app)
    resp = client.get(_span_endpoint(chain["evidence_span_id"]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["evidence_span_id"] == str(chain["evidence_span_id"])
    assert body["items"] == []
    assert body["latest_assessment"] is None


# ===========================================================================
# 4 — evidence_span endpoint: unknown id -> 404 RESOURCE_NOT_FOUND
# ===========================================================================
def test_get_evidence_span_source_quality_404() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)
    bogus = uuid.uuid4()
    resp = client.get(_span_endpoint(bogus))

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "evidence_spans"
    assert details.get("id") == str(bogus)


# ===========================================================================
# 5 — task endpoint, happy path: two spans linked, one assessed, summary
#     reflects the asymmetry
# ===========================================================================
def test_get_task_source_quality_happy_path() -> None:
    """Two evidence_spans linked to the task via two distinct
    logical_claims. Only one of the two spans has an assessment.

    The endpoint must:
      - return both spans (none hidden);
      - mark the unassessed span with latest_assessment=null and
        items=[];
      - mark the assessed span with latest_assessment != null;
      - report summary totals 2 / 1 / 1;
      - count the single latest overall_quality value into the
        ``unknown`` bucket (the seed default).
    """
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain_assessed = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        chain_unassessed = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        lc_a, le_a = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        lc_u, le_u = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=le_a,
            evidence_span_id=chain_assessed["evidence_span_id"],
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_u,
            claim_ledger_entry_id=le_u,
            evidence_span_id=chain_unassessed["evidence_span_id"],
        )

    with engine.begin() as conn:
        _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain_assessed["evidence_span_id"],
            version_no=1,
            overall_quality="unknown",
        )

    client = TestClient(app)
    resp = client.get(_task_endpoint(task_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["task_id"] == str(task_id)

    items = body["items"]
    assert isinstance(items, list)
    assert len(items) == 2

    by_span_id = {item["evidence_span_id"]: item for item in items}
    assert str(chain_assessed["evidence_span_id"]) in by_span_id
    assert str(chain_unassessed["evidence_span_id"]) in by_span_id

    assessed_item = by_span_id[str(chain_assessed["evidence_span_id"])]
    unassessed_item = by_span_id[str(chain_unassessed["evidence_span_id"])]

    # Assessed span: items[0] exists, latest_assessment non-null.
    assert len(assessed_item["items"]) == 1
    assert assessed_item["latest_assessment"] is not None
    assert (
        assessed_item["latest_assessment"]["overall_quality"] == "unknown"
    )

    # Unassessed span: items empty, latest_assessment null.
    assert unassessed_item["items"] == []
    assert unassessed_item["latest_assessment"] is None

    # Summary aggregation.
    summary = body["summary"]
    assert summary["evidence_spans_total"] == 2
    assert summary["spans_with_assessment"] == 1
    assert summary["spans_without_assessment"] == 1

    counts = summary["latest_overall_quality_counts"]
    # All overall_quality codomain keys are present, initialized to 0.
    for k in ("strong", "adequate", "weak", "unsuitable", "unknown"):
        assert k in counts, f"missing overall_quality key {k!r}: {counts!r}"
    assert counts["unknown"] == 1
    assert counts["strong"] == 0
    assert counts["adequate"] == 0
    assert counts["weak"] == 0
    assert counts["unsuitable"] == 0


# ===========================================================================
# 6 — task endpoint, multiple versions per span: summary counts only the
#     latest assessment per span
# ===========================================================================
def test_get_task_source_quality_multiple_versions_latest_summary_counts_latest_only() -> None:
    """One span with two assessments: v1 overall_quality='weak',
    v2 overall_quality='adequate'.

    Summary must count the LATEST per span only, i.e. ``adequate=1``,
    NOT ``weak=1, adequate=1``. The block prompt makes this an
    explicit invariant.

    We use distinct ``overall_quality`` values across the two
    versions (rather than ``low`` / ``medium`` as in the prompt
    example) because ``low`` / ``medium`` belong to the
    ``authority_level`` codomain, not to ``overall_quality``. The
    ``overall_quality`` codomain is ``{strong, adequate, weak,
    unsuitable, unknown}`` — see migrations/0007_source_quality.sql.
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
        lc, le = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=chain["evidence_span_id"],
        )

    with engine.begin() as conn:
        _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            overall_quality="weak",
            idempotency_key=f"v1-{_unique_hex()}",
        )
    with engine.begin() as conn:
        _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=2,
            overall_quality="adequate",
            idempotency_key=f"v2-{_unique_hex()}",
        )

    client = TestClient(app)
    resp = client.get(_task_endpoint(task_id))
    assert resp.status_code == 200, resp.text

    body = resp.json()
    items = body["items"]
    assert len(items) == 1
    span_item = items[0]
    # latest within the span is v2 (overall_quality='adequate').
    assert span_item["latest_assessment"]["version_no"] == 2
    assert span_item["latest_assessment"]["overall_quality"] == "adequate"

    summary = body["summary"]
    assert summary["evidence_spans_total"] == 1
    assert summary["spans_with_assessment"] == 1
    assert summary["spans_without_assessment"] == 0

    counts = summary["latest_overall_quality_counts"]
    # latest_overall_quality_counts counts only the LATEST per span.
    assert counts["adequate"] == 1
    assert counts["weak"] == 0
    assert counts["strong"] == 0
    assert counts["unsuitable"] == 0
    assert counts["unknown"] == 0


# ===========================================================================
# 7 — task endpoint, task without claim_evidence_links:
#     200 with items=[] and summary totals zero
# ===========================================================================
def test_get_task_source_quality_no_claim_evidence_links() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)

    client = TestClient(app)
    resp = client.get(_task_endpoint(task_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["items"] == []

    summary = body["summary"]
    assert summary["evidence_spans_total"] == 0
    assert summary["spans_with_assessment"] == 0
    assert summary["spans_without_assessment"] == 0

    counts = summary["latest_overall_quality_counts"]
    for k in ("strong", "adequate", "weak", "unsuitable", "unknown"):
        assert counts[k] == 0


# ===========================================================================
# 8 — task endpoint: unknown id -> 404 RESOURCE_NOT_FOUND
# ===========================================================================
def test_get_task_source_quality_404() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)
    bogus = uuid.uuid4()
    resp = client.get(_task_endpoint(bogus))

    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "task_masters"
    assert details.get("id") == str(bogus)


# ===========================================================================
# 9 — read-only invariant: no count drift on any 8.4 / 8.5 / 8.6 / 8.7
#     / audit table
# ===========================================================================
def test_read_endpoints_are_read_only() -> None:
    """Both endpoints MUST NOT mutate any DB row. We snapshot row
    counts on every relevant table AFTER seeding the test data, hit
    both endpoints several times (including a 404 path), and assert
    the snapshot is identical afterward.

    Tables in the snapshot (block prompt 8.7F):
      - source_quality_assessments
      - audit_records
      - claim_ledger_entries
      - claim_evidence_links
      - logical_claims
      - final_gate_reports
      - published_answers
      - source_loss_events
      - source_loss_propagation_records

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
        lc, le = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc,
            claim_ledger_entry_id=le,
            evidence_span_id=chain["evidence_span_id"],
        )

    with engine.begin() as conn:
        _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            overall_quality="unknown",
        )

    # Snapshot BEFORE the GETs (i.e. after all seed transactions).
    with engine.connect() as conn:
        before = _snapshot_all_counts(conn)

    client = TestClient(app)

    # Both 200 paths.
    r_span = client.get(_span_endpoint(chain["evidence_span_id"]))
    assert r_span.status_code == 200, r_span.text
    r_task = client.get(_task_endpoint(task_id))
    assert r_task.status_code == 200, r_task.text

    # Both 404 paths.
    r_span_404 = client.get(_span_endpoint(uuid.uuid4()))
    assert r_span_404.status_code == 404, r_span_404.text
    r_task_404 = client.get(_task_endpoint(uuid.uuid4()))
    assert r_task_404.status_code == 404, r_task_404.text

    # Limit variants on both endpoints.
    r_span_lim = client.get(
        _span_endpoint(chain["evidence_span_id"]), params={"limit": 10}
    )
    assert r_span_lim.status_code == 200, r_span_lim.text
    r_task_lim = client.get(
        _task_endpoint(task_id), params={"limit_per_span": 10}
    )
    assert r_task_lim.status_code == 200, r_task_lim.text

    # Snapshot AFTER all the GETs.
    with engine.connect() as conn:
        after = _snapshot_all_counts(conn)

    assert after == before, (
        "row counts drifted after read-only GETs; "
        f"before={before!r}, after={after!r}"
    )


# ===========================================================================
# 10 — evidence_span endpoint: limit=1 truncates the response and
#      latest_assessment reflects the SINGLE returned item
# ===========================================================================
def test_limit_bounds_or_limit_behavior() -> None:
    """Seed three versions for the same evidence_span, ask for
    ``limit=1``, and assert:

      - items length is 1;
      - the returned item is the first by ASC ordering
        (version_no=1);
      - latest_assessment is the SAME item as items[0] (latest
        within the returned slice; see the route docstring for the
        rationale).

    Also exercises the invalid-limit path: ``limit=0`` is rejected
    by FastAPI's RequestValidationError handler (the route
    declares ``Query(default=100, ge=1, le=5000)``). The repo's
    normalized error handler returns 400 VALIDATION_ERROR; we
    accept 422 as well in case the handler wiring is bypassed in
    some future configuration. The assertion does NOT depend on
    body inspection, only on the status code, so this remains
    robust across both branches.
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

    # Insert three versions in separate transactions so created_at
    # advances; version_no is what drives the ASC ordering anyway.
    with engine.begin() as conn:
        v1_id = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            idempotency_key=f"v1-{_unique_hex()}",
        )
    with engine.begin() as conn:
        _ = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=2,
            idempotency_key=f"v2-{_unique_hex()}",
        )
    with engine.begin() as conn:
        _ = _insert_source_quality_assessment(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=3,
            idempotency_key=f"v3-{_unique_hex()}",
        )

    client = TestClient(app)

    # Sanity: without limit we get all three.
    resp_full = client.get(_span_endpoint(chain["evidence_span_id"]))
    assert resp_full.status_code == 200, resp_full.text
    assert len(resp_full.json()["items"]) == 3

    # With limit=1 only the first (ASC by version_no) is returned, and
    # latest_assessment reflects the SINGLE item returned (latest
    # within the returned slice — documented semantics).
    resp = client.get(
        _span_endpoint(chain["evidence_span_id"]), params={"limit": 1}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(v1_id)
    assert items[0]["version_no"] == 1
    assert body["latest_assessment"] == items[0]

    # Invalid limit (0 violates ge=1): expect 400 or 422 depending on
    # which handler runs first. The body shape is enforced by
    # NormalizedError when 400; we don't assert on the body to keep
    # the test robust against handler-wiring changes.
    resp_invalid = client.get(
        _span_endpoint(chain["evidence_span_id"]), params={"limit": 0}
    )
    assert resp_invalid.status_code in (400, 422), resp_invalid.text
