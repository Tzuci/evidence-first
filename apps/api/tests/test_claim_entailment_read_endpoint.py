"""API tests for the claim_entailment read endpoint (Phase 8.8A-READ-A).

Endpoint exercised:

  GET /api/v1/tasks/{task_id}/claim-entailment

Coverage map (5 scenarios required by the block prompt):

  1. test_get_task_claim_entailment_returns_404_for_missing_task
  2. test_get_task_claim_entailment_returns_empty_items_for_task_without_checks
  3. test_get_task_claim_entailment_returns_checks_for_task
  4. test_get_task_claim_entailment_respects_limit_and_ordering
  5. test_get_task_claim_entailment_is_read_only

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    resolves to apps/api/app, so ``from app.main import app`` and
    ``from app.db import get_engine`` are the canonical imports — same
    pattern used by all other 8.5 / 8.6 / 8.7F API test modules.
  - We do NOT touch Redis: the endpoint is strictly read-only and
    does not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code (no checker, no orchestrator,
    no dispatcher, no consumer). All rows are seeded directly via
    SQL — exactly what the (mock) worker does in production, minus
    the orchestration.
  - Helpers are LOCAL to this file (per the block prompt: no imports
    from other test files). Seed primitives mirror those used in
    apps/api/tests/test_source_quality_read_endpoint.py, but they
    are NOT imported from there.
  - The append-only table ``claim_entailment_checks`` accepts INSERT —
    the shared ``reject_modify_append_only`` trigger only blocks
    UPDATE / DELETE.
  - The composite FK ``cec_entry_logical_consistency`` requires
    ``(claim_ledger_entry_id, claim_logical_id)`` to match a single
    ledger row; helpers below honor that invariant by always passing
    the pair drawn from the same ledger insert.
  - The partial UNIQUE indexes on ``claim_entailment_checks``
    (``cec_entry_span_version_uq`` on
    ``(claim_ledger_entry_id, evidence_span_id, version_no)`` and
    ``cec_entry_span_idem_uq`` on
    ``(claim_ledger_entry_id, evidence_span_id, idempotency_key)``)
    require uniqueness per pair. Tests that insert multiple rows for
    the same task use DISTINCT (entry, span) pairs to avoid spurious
    IntegrityErrors that would obscure the actual assertion.
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

    Mirrors the gating used by every other 8.5 / 8.6 / 8.7F API test
    module: the endpoint needs a real DB to seed the
    evidence_span / logical_claim / claim_ledger_entry chain and the
    claim_entailment_checks rows. No Redis is required.
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

    Envelope shape (from ``packages/shared/evidencefirst_shared/errors.py``):

        {"error": {"code": "...", "message": "...", "details": {...}, ...}}
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


def _endpoint(task_id: uuid.UUID) -> str:
    return f"/api/v1/tasks/{task_id}/claim-entailment"


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
                {"t": tenant_id, "n": f"cer-test-{uuid.uuid4()}"},
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

    Returns a dict with the chain ids. Mirrors the helper used by
    apps/api/tests/test_source_quality_read_endpoint.py but is kept
    LOCAL here (no cross-file imports, per the block prompt).
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Claim entailment read API test marker {marker}. "
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
    'verified_fact'.

    Returns ``(claim_logical_id, claim_ledger_entry_id)``.

    Note on the ledger entry's state: ``verified_fact`` is the
    semantically natural state for a claim that survives the
    8.4 CVE-lite step and is the one the Final Answer Gate consumes
    when consulting entailment in 8.8A-GATE-CODE. The composite
    UNIQUE ``cle_id_logical_uq (id, claim_logical_id)`` on
    ``claim_ledger_entries`` is the target of the
    ``cec_entry_logical_consistency`` composite FK on
    ``claim_entailment_checks`` introduced by 0009; consumers MUST
    pass the (entry_id, logical_id) pair from the SAME ledger row to
    honor it.
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

    NOTE: ``claim_evidence_links`` is NOT a strict pre-condition of
    inserting a row into ``claim_entailment_checks`` (0009 does not
    require a matching ``cel`` row); however, in production the
    orchestrator only emits entailment checks for pairs derived
    from existing ``cel`` rows, so we seed the link for fidelity.
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
# DB seeding helpers — claim_entailment_checks
# ---------------------------------------------------------------------------
# Default mock-like values mirroring what the mock checker writes today
# (see apps/worker/app/services/claim_entailment_checker.py). Tests
# override individual fields as needed.
_DEFAULT_CHECKER_NAME = "mvp0_mock_entailment_checker"
_DEFAULT_CHECKER_VERSION = "0.1.0"
_DEFAULT_POLICY_NAME = "mvp0_mock_entailment"
_DEFAULT_POLICY_VERSION = "0.1.0"


def _insert_claim_entailment_check(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
    claim_ledger_entry_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
    version_no: int = 1,
    verdict: str = "entailed",
    confidence: float | None = 0.8,
    idempotency_key: str | None = None,
    rationale: str | None = None,
    payload: dict[str, Any] | None = None,
    created_at_sql: str | None = None,
) -> uuid.UUID:
    """Insert ONE claim_entailment_checks row directly via SQL.

    We do NOT pass an explicit ``id`` so the table's DEFAULT
    ``app_new_uuid()`` is exercised.

    Caller-controllable fields (the rest stay at the mock defaults):
      - version_no       (default 1)
      - verdict          (default 'entailed')
      - confidence       (default 0.8)
      - idempotency_key  (default: fresh hex per call)
      - rationale        (default NULL)
      - payload          (default: ``{"mock": True}``)
      - created_at_sql   (default: rely on table's DEFAULT NOW();
                          if provided, must be a SQL expression
                          string interpolated directly into the
                          query — use ONLY trusted callers; see
                          note below)

    SECURITY note on ``created_at_sql``:
      This argument is interpolated VERBATIM into the SQL string.
      It exists ONLY so the ordering test can produce rows with
      controlled, distinct created_at values (e.g.
      ``NOW() - interval '1 hour'``). All callers in this file pass
      trusted constant strings; no test-user input reaches this
      argument. The corresponding ``claim_entailment_checks.created_at``
      column has DEFAULT NOW() — passing ``None`` (the default) is
      the safe, normal path.
    """
    eff_payload = payload if payload is not None else {"mock": True}
    eff_key = idempotency_key if idempotency_key is not None else _unique_hex()

    if created_at_sql is None:
        sql = text(
            """
            INSERT INTO claim_entailment_checks (
                tenant_id, project_id, task_id,
                claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                version_no,
                verdict, confidence,
                checker_name, checker_version,
                policy_name, policy_version,
                idempotency_key, rationale, payload
            ) VALUES (
                :tenant_id, :project_id, :task_id,
                :claim_logical_id, :claim_ledger_entry_id, :evidence_span_id,
                :version_no,
                :verdict, :confidence,
                :checker_name, :checker_version,
                :policy_name, :policy_version,
                :idempotency_key, :rationale, CAST(:payload AS JSONB)
            )
            RETURNING id
            """
        )
    else:
        # created_at_sql is a TRUSTED constant from this file only
        # (e.g. "NOW() - interval '1 hour'"). Do NOT expose this
        # argument to test-user input.
        sql = text(
            f"""
            INSERT INTO claim_entailment_checks (
                tenant_id, project_id, task_id,
                claim_logical_id, claim_ledger_entry_id, evidence_span_id,
                version_no,
                verdict, confidence,
                checker_name, checker_version,
                policy_name, policy_version,
                idempotency_key, rationale, payload,
                created_at
            ) VALUES (
                :tenant_id, :project_id, :task_id,
                :claim_logical_id, :claim_ledger_entry_id, :evidence_span_id,
                :version_no,
                :verdict, :confidence,
                :checker_name, :checker_version,
                :policy_name, :policy_version,
                :idempotency_key, :rationale, CAST(:payload AS JSONB),
                {created_at_sql}
            )
            RETURNING id
            """
        )

    row = conn.execute(
        sql,
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "task_id": task_id,
            "claim_logical_id": claim_logical_id,
            "claim_ledger_entry_id": claim_ledger_entry_id,
            "evidence_span_id": evidence_span_id,
            "version_no": version_no,
            "verdict": verdict,
            "confidence": confidence,
            "checker_name": _DEFAULT_CHECKER_NAME,
            "checker_version": _DEFAULT_CHECKER_VERSION,
            "policy_name": _DEFAULT_POLICY_NAME,
            "policy_version": _DEFAULT_POLICY_VERSION,
            "idempotency_key": eff_key,
            "rationale": rationale,
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
# this fixed set. Mirrors the pattern adopted by all 8.6 / 8.7F test
# modules.
_COUNTABLE_TABLES = frozenset(
    {
        "claim_entailment_checks",
        "claim_ledger_entries",
        "coverage_gap_statements",
        "final_gate_reports",
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
# 1 — 404 for missing task
# ===========================================================================
def test_get_task_claim_entailment_returns_404_for_missing_task() -> None:
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
# 2 — Task exists but no checks: 200 with items=[]
# ===========================================================================
def test_get_task_claim_entailment_returns_empty_items_for_task_without_checks() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        _tenant_id, _project_id, _user_id, task_id = _seeded_dev(conn)

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["items"] == []


# ===========================================================================
# 3 — Task with at least one check: full row roundtrip
# ===========================================================================
def test_get_task_claim_entailment_returns_checks_for_task() -> None:
    """Full happy path: seed the FK chain, insert one check, expect 200
    with one item whose key fields match what we wrote, including the
    JSONB payload (verbatim).
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
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )

    seeded_payload = {"mock": True, "rule": "containment_match", "n": 42}
    seeded_idem = f"happy-{_unique_hex()}"
    with engine.begin() as conn:
        check_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
            version_no=1,
            verdict="entailed",
            confidence=0.8,
            idempotency_key=seeded_idem,
            payload=seeded_payload,
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

    # Key fields per the block prompt.
    assert item["id"] == str(check_id)
    assert item["task_id"] == str(task_id)
    assert item["claim_logical_id"] == str(lc_id)
    assert item["claim_ledger_entry_id"] == str(cle_id)
    assert item["evidence_span_id"] == str(chain["evidence_span_id"])
    assert item["verdict"] == "entailed"
    assert item["checker_name"] == _DEFAULT_CHECKER_NAME
    assert item["checker_version"] == _DEFAULT_CHECKER_VERSION
    assert item["policy_name"] == _DEFAULT_POLICY_NAME
    assert item["policy_version"] == _DEFAULT_POLICY_VERSION
    assert item["version_no"] == 1
    assert isinstance(item["confidence"], float)
    assert item["confidence"] == pytest.approx(0.8)
    assert item["idempotency_key"] == seeded_idem
    # JSONB roundtrip — payload exposed verbatim (no redaction in MVP-0).
    assert item["payload"] == seeded_payload
    assert item["payload"]["mock"] is True


# ===========================================================================
# 4 — limit + ordering
# ===========================================================================
def test_get_task_claim_entailment_respects_limit_and_ordering() -> None:
    """Insert 3 checks for the same task on DISTINCT (entry, span) pairs
    with controlled, monotonically increasing created_at values, then
    assert:

      - ``?limit=2`` returns exactly 2 items;
      - the returned items are the two MOST RECENT (DESC by created_at).

    Note on uniqueness:
      The UNIQUE indexes ``cec_entry_span_version_uq`` and
      ``cec_entry_span_idem_uq`` are scoped to
      ``(claim_ledger_entry_id, evidence_span_id, ...)``. We therefore
      use a distinct (claim_logical_id, claim_ledger_entry_id,
      evidence_span_id) triple per row. Three independent logical
      claims and three independent evidence spans is the simplest
      shape that always honors both UNIQUE constraints without any
      need for higher version_no values.

    Note on created_at:
      ``claim_entailment_checks.created_at`` has DEFAULT NOW() and is
      append-only (no UPDATE allowed by the trigger). We set
      controlled values at INSERT time using a SQL interpolation
      argument that is a TRUSTED constant string in this file
      (``_insert_claim_entailment_check(created_at_sql=...)``); no
      test-user input reaches that argument.
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
        chain_c = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        lc_a, cle_a = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        lc_b, cle_b = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        lc_c, cle_c = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )

    # Oldest -> newest: 'old', 'mid', 'new'. With ORDER BY created_at
    # DESC, the response order should be new, mid, old.
    with engine.begin() as conn:
        check_old_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_a,
            claim_ledger_entry_id=cle_a,
            evidence_span_id=chain_a["evidence_span_id"],
            verdict="entailed",
            created_at_sql="NOW() - interval '2 hours'",
        )
        check_mid_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_b,
            claim_ledger_entry_id=cle_b,
            evidence_span_id=chain_b["evidence_span_id"],
            verdict="uncertain",
            created_at_sql="NOW() - interval '1 hour'",
        )
        check_new_id = _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_c,
            claim_ledger_entry_id=cle_c,
            evidence_span_id=chain_c["evidence_span_id"],
            verdict="not_supported",
            created_at_sql="NOW()",
        )

    client = TestClient(app)

    # Sanity check: no limit (or large limit) returns all three, in
    # DESC order new -> mid -> old.
    resp_full = client.get(_endpoint(task_id))
    assert resp_full.status_code == 200, resp_full.text
    items_full = resp_full.json()["items"]
    assert len(items_full) == 3
    ids_full = [it["id"] for it in items_full]
    assert ids_full == [
        str(check_new_id),
        str(check_mid_id),
        str(check_old_id),
    ], (
        "expected DESC ordering by created_at; "
        f"got {ids_full!r}"
    )

    # limit=2 must clip to the two most recent.
    resp = client.get(_endpoint(task_id), params={"limit": 2})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert len(items) == 2
    ids = [it["id"] for it in items]
    assert ids == [str(check_new_id), str(check_mid_id)], (
        "expected the two MOST RECENT rows with limit=2; "
        f"got {ids!r}"
    )


# ===========================================================================
# 5 — Read-only invariant
# ===========================================================================
def test_get_task_claim_entailment_is_read_only() -> None:
    """The endpoint MUST NOT mutate any DB row. We snapshot row counts
    on every relevant table AFTER seeding the test data, hit the
    endpoint several times (including a 404 path and a limit variant),
    and assert the snapshot is identical afterward.

    Tables in the snapshot (per the block prompt):
      - claim_entailment_checks
      - claim_ledger_entries
      - coverage_gap_statements
      - final_gate_reports
      - audit_records

    The seed itself is NOT counted as a mutation: we snapshot AFTER
    the seed transactions commit. The whitelist in ``_count_table``
    prevents accidental SQL interpolation from non-trusted callers.
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
        lc_id, cle_id = _create_logical_claim_with_verified_entry(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
        )
        _link_claim_to_span(
            conn,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
        )

    with engine.begin() as conn:
        _insert_claim_entailment_check(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            claim_logical_id=lc_id,
            claim_ledger_entry_id=cle_id,
            evidence_span_id=chain["evidence_span_id"],
            verdict="entailed",
        )

    # Snapshot BEFORE the GETs (i.e. after all seed transactions).
    with engine.connect() as conn:
        before = _snapshot_all_counts(conn)

    client = TestClient(app)

    # Happy path.
    r_ok = client.get(_endpoint(task_id))
    assert r_ok.status_code == 200, r_ok.text
    assert len(r_ok.json()["items"]) == 1

    # 404 path: must also be free of side effects.
    r_404 = client.get(_endpoint(uuid.uuid4()))
    assert r_404.status_code == 404, r_404.text

    # Limit variant.
    r_lim = client.get(_endpoint(task_id), params={"limit": 10})
    assert r_lim.status_code == 200, r_lim.text

    # Snapshot AFTER the GETs.
    with engine.connect() as conn:
        after = _snapshot_all_counts(conn)

    assert after == before, (
        "row counts drifted after read-only GETs; "
        f"before={before!r}, after={after!r}"
    )
