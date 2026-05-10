"""Phase 8.5 — Realistic Flow Test 2: source_loss end-to-end-like.

Scope (cross-component, root-tests):
  - API POST /api/v1/source-loss-events
    intercepted with FakeRedis (no real Redis), captured xadd
    -> reconstructed as a Redis-decoded event dict
    -> fed to apps/worker/app/consumers/dispatch.handle_event
    -> worker source_loss consumer + source_loss_propagator service
       run against the real DB
    -> assert source_loss_events row, EPR row, claim ledger transition
       (verified_fact -> unverifiable / source_lost), claim_lineage,
       source_loss_propagation_records, audit emission, audit-chain
       integrity, and task_masters.status invariant.

What this test is and is not:
  - It IS a realistic exercise of the producer + dispatcher + consumer
    + service pipeline against a real Postgres. Every layer below the
    Redis transport runs the production code path; only Redis itself is
    a FakeRedis that records xadd calls.
  - It is NOT a Redis-loop test: no XREADGROUP, no consumer groups, no
    worker main() loop. The transport semantics (delivery, ack, claim)
    are covered by their own dedicated tests in apps/worker/tests/.
  - It does NOT seed a published_answer for this task. We deliberately
    exercise the propagator's "no active published_answers impacted"
    branch in this realistic test; the impact-on-published-answer path
    is fully covered by
    apps/worker/tests/test_source_loss_consumer.py::
        test_source_loss_consumer_marks_published_answer_impacted_without_withdrawing.

Cross-component bootstrap (same approach as the withdrawal flow test):
  Both apps/api/app and apps/worker/app are top-level packages literally
  named ``app``. We:
    1) prepend apps/api + packages/shared to sys.path so ``import app``
       resolves to the API,
    2) import API normally (``from app.main import app as api_app``,
       ``from app.routes import source_loss as source_loss_route``),
    3) load the worker package via importlib.util under a synthetic
       top-level alias ``_wapp``, registering every submodule
       (``_wapp``, ``_wapp.consumers``, ``_wapp.services``, plus each
       leaf module) in sys.modules so the worker's relative imports
       resolve within its own namespace, without colliding with API's
       ``app`` namespace.
  When this test runs alongside tests/test_phase_8_5_withdrawal_flow.py
  in the same interpreter, the ``_wapp`` namespace is already populated
  and ``_bootstrap_worker()`` short-circuits — both files share the
  same worker module instances, which is fine since they share a DB.

DB requirement:
  Same Postgres used by ``make test-db`` — DATABASE_URL is set, the
  migrations are applied. We never set DATABASE_URL ourselves; if it
  is missing or unreachable, the test is skipped.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# sys.path setup — runs at import time, BEFORE any project import
# ---------------------------------------------------------------------------
# This file lives at <repo>/tests/test_phase_8_5_source_loss_flow.py, so
# parents[1] is the repo root. We deliberately do NOT add apps/worker to
# sys.path: worker is loaded by file path under an alias namespace
# further down. Adding it here would re-introduce the ``app`` collision.
ROOT = Path(__file__).resolve().parents[1]
for _p in (
    ROOT / "apps" / "api",        # so ``import app`` resolves to the API package
    ROOT / "packages" / "shared", # evidencefirst_shared
    ROOT,                         # repo root, harmless and aligns with make test-db
):
    _p_str = str(_p)
    if _p_str not in sys.path:
        sys.path.insert(0, _p_str)


import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

# API imports — ``app`` here is apps/api/app/.
from app.db import get_engine  # noqa: E402
from app.main import app as api_app  # noqa: E402
from app.routes import source_loss as source_loss_route  # noqa: E402

# Shared helpers used to validate the audit chain end-to-end.
from evidencefirst_shared.db.audit import verify_task_audit_chain  # noqa: E402


# ---------------------------------------------------------------------------
# Worker bootstrap under alias namespace ``_wapp``
# ---------------------------------------------------------------------------
# We load apps/worker/app/* under the synthetic top-level name ``_wapp`` so
# that worker's ``from ..db import transaction`` resolves to ``_wapp.db``
# rather than to API's ``app.db``. This must happen exactly once per
# interpreter; subsequent calls reuse the cached entries. When run in the
# same pytest session as tests/test_phase_8_5_withdrawal_flow.py, the
# alias is already populated and we just retrieve dispatch from sys.modules.
_WORKER_ALIAS = "_wapp"
_WORKER_ROOT = ROOT / "apps" / "worker" / "app"


def _load_pkg(alias: str, path: Path) -> None:
    """Register ``alias`` as a Python package whose __init__ lives at path/."""
    if alias in sys.modules:
        return
    init_file = path / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        alias,
        str(init_file),
        submodule_search_locations=[str(path)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)


def _load_mod(alias: str, path: Path) -> None:
    """Register a single .py file as ``alias`` in sys.modules."""
    if alias in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(alias, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)


def _bootstrap_worker() -> Any:
    """Idempotently load the worker package under the ``_wapp`` alias and
    return the dispatch module. Order matters: each module must see its
    dependencies already registered in sys.modules before it executes.

    Identical implementation to tests/test_phase_8_5_withdrawal_flow.py:
    when both flow tests run in the same session, this function returns
    the same dispatch module instance the first test bootstrapped, so
    no double-load occurs.
    """
    if f"{_WORKER_ALIAS}.consumers.dispatch" in sys.modules:
        return sys.modules[f"{_WORKER_ALIAS}.consumers.dispatch"]

    # Package skeletons.
    _load_pkg(_WORKER_ALIAS, _WORKER_ROOT)
    _load_pkg(f"{_WORKER_ALIAS}.consumers", _WORKER_ROOT / "consumers")
    _load_pkg(f"{_WORKER_ALIAS}.services", _WORKER_ROOT / "services")

    # Worker-level config + db.
    _load_mod(f"{_WORKER_ALIAS}.config", _WORKER_ROOT / "config.py")
    _load_mod(f"{_WORKER_ALIAS}.db", _WORKER_ROOT / "db.py")

    # All services that the three consumers transitively import.
    for _svc in (
        "compiler",
        "cve_lite",
        "extractor",
        "final_answer_gate",
        "published_answer_lifecycle",
        "source_loss_propagator",
    ):
        _load_mod(
            f"{_WORKER_ALIAS}.services.{_svc}",
            _WORKER_ROOT / "services" / f"{_svc}.py",
        )

    # Consumers other than dispatch (dispatch imports them).
    for _cons in ("task_created", "published_answer_withdrawal", "source_loss"):
        _load_mod(
            f"{_WORKER_ALIAS}.consumers.{_cons}",
            _WORKER_ROOT / "consumers" / f"{_cons}.py",
        )

    # Finally, the dispatch module itself.
    _load_mod(
        f"{_WORKER_ALIAS}.consumers.dispatch",
        _WORKER_ROOT / "consumers" / "dispatch.py",
    )
    return sys.modules[f"{_WORKER_ALIAS}.consumers.dispatch"]


_dispatch = _bootstrap_worker()


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
EXPECTED_STREAM = "app.events.source_loss_detected"
EXPECTED_EVENT_TYPE = "source_loss.detected"
WORKER_CONSUMER_NAME = "source_loss"  # stable, logical default
EXPECTED_DEFAULT_DETECTED_BY = "api"

AUDIT_EVENT_PROPAGATED_TO_CLAIM = "source_loss.propagated_to_claim"
AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER = (
    "source_loss.propagated_to_published_answer"
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    We do NOT require REDIS_URL because every test in this file installs
    a FakeRedis on the API route and never invokes the worker's Redis
    loop. The DB is the only external dependency.
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


def _normalize_jsonb(value: Any) -> Any:
    """Normalize a JSONB column read into a Python object.

    psycopg (3.x) returns JSONB as native Python values, but on some
    driver / pool combinations the value may surface as a JSON string.
    We accept both so the assertions are robust across environments.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# DB seeding helpers
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
                {"t": tenant_id, "n": f"phase-8-5-source-loss-flow-{uuid.uuid4()}"},
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

    Returns:
        {
          "document_id": uuid,
          "document_version_id": uuid,
          "document_chunk_id": uuid,
          "evidence_span_id": uuid,
          "quote": str,
          "chunk_text": str,
        }

    Notes:
      - The content_hash is salted with a per-call uuid suffix so the
        global UNIQUE (content_hash, hash_algorithm) WHERE
        tenant_namespace_id IS NULL never collides on a long-running
        dev DB.
      - dc_origin_xor is honored by setting document_version_id and
        leaving source_version_id NULL.
    """
    marker = uuid.uuid4().hex[:12]
    quote = f"quotable span {marker}"
    chunk_text = (
        f"Source loss realistic flow marker {marker}. "
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


def _create_claim_linked_to_span(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    evidence_span_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed:
      - one logical_claims row scoped to this task;
      - one claim_ledger_entries v1 row in state 'verified_fact' /
        support_scope='supported_by_user_corpus_only' (the typical head
        state of a published claim);
      - one claim_evidence_links row connecting (logical, entry) to the
        given evidence_span (cel_origin_xor honored:
        retrieved_source_span_id stays NULL).

    Returns (claim_logical_id, claim_ledger_entry_id_v1).

    This is the minimal corpus state the propagator needs to mark the
    claim 'unverifiable / source_lost' on a source loss against the
    span.
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

    conn.execute(
        text(
            """
            INSERT INTO claim_evidence_links
                (id, claim_logical_id, claim_ledger_entry_id,
                 evidence_span_id, retrieved_source_span_id, link_role)
            VALUES (:id, :lc, :le, :es, NULL, 'primary_support')
            """
        ),
        {
            "id": uuid.uuid4(),
            "lc": claim_logical_id,
            "le": claim_ledger_entry_id,
            "es": evidence_span_id,
        },
    )
    return claim_logical_id, claim_ledger_entry_id


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
        "project_id": (
            uuid.UUID(str(m["project_id"])) if m["project_id"] is not None else None
        ),
        "task_id": (
            uuid.UUID(str(m["task_id"])) if m["task_id"] is not None else None
        ),
        "evidence_span_id": uuid.UUID(str(m["evidence_span_id"])),
        "document_chunk_id": (
            uuid.UUID(str(m["document_chunk_id"]))
            if m["document_chunk_id"] is not None
            else None
        ),
        "document_version_id": (
            uuid.UUID(str(m["document_version_id"]))
            if m["document_version_id"] is not None
            else None
        ),
        "document_id": (
            uuid.UUID(str(m["document_id"])) if m["document_id"] is not None else None
        ),
        "loss_kind": str(m["loss_kind"]),
        "loss_reason": str(m["loss_reason"]),
        "detected_by": str(m["detected_by"]),
        "event_payload": _normalize_jsonb(m["event_payload"]),
        "idempotency_key": str(m["idempotency_key"]),
    }


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


def _fetch_claim_head_or_latest(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> dict[str, Any]:
    """Return the latest claim_ledger_entries row (highest version_no).

    Caller must have already inserted at least one ledger entry for this
    claim — otherwise this raises NoResultFound.
    """
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


def _count_claim_lineage_pair(
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


def _count_source_loss_propagation_records(
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


def _fetch_task_status(conn: Connection, *, task_id: uuid.UUID) -> str:
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


def _count_lifecycle_events_global_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> int:
    """Count published_answer_lifecycle_events for any published_answer
    whose task_id matches. The source_loss pipeline must NOT write to
    this table for any task.
    """
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE task_id = :t
                """
            ),
            {"t": task_id},
        ).scalar_one()
    )


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
        "tenant_id": (
            uuid.UUID(str(m["tenant_id"])) if m["tenant_id"] is not None else None
        ),
        "project_id": (
            uuid.UUID(str(m["project_id"])) if m["project_id"] is not None else None
        ),
        "task_id": (
            uuid.UUID(str(m["task_id"])) if m["task_id"] is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# FakeRedis (Block 4B-1 surface area only)
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub.

    Only ``xadd`` is implemented — that is the entire Redis surface used
    by ``app.routes.source_loss.create_source_loss_event``. We
    deliberately do NOT add other Redis methods; this object is meant to
    be installed as the return value of ``source_loss_route.get_redis``
    and nothing else.

    The captured ``fields`` are stored via ``dict(fields)`` so that any
    later mutation by the producer would not silently change what the
    test asserts on.
    """

    def __init__(self) -> None:
        self.xadd_calls: list[dict[str, Any]] = []

    def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        self.xadd_calls.append(
            {
                "stream": stream,
                "fields": dict(fields),
                "maxlen": maxlen,
                "approximate": approximate,
            }
        )
        return "1700000000000-0"


def _install_fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Patch ``source_loss_route.get_redis`` with a fresh FakeRedis.

    The route module captured ``get_redis`` at import time via
    ``from ..redis import get_redis``, so the patched binding must live
    on ``app.routes.source_loss``, NOT on ``app.redis``.
    """
    fake = FakeRedis()
    monkeypatch.setattr(source_loss_route, "get_redis", lambda: fake)
    return fake


# ===========================================================================
# THE realistic flow test
# ===========================================================================
def test_phase_8_5_source_loss_api_to_worker_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end-like source_loss flow: API -> FakeRedis -> dispatcher
    -> source_loss consumer -> source_loss_propagator -> DB + audit.

    Steps in order:

      (1) seed DB: tenant + user + fresh project + fresh task +
          storage chain (blob/object/document/version/chunk/span) +
          one logical_claim with v1 ledger entry verified_fact +
          claim_evidence_link tying that v1 to the seeded span.

      (2) install FakeRedis on the API source_loss route module.

      (3) POST /api/v1/source-loss-events with a fully-populated body
          (loss_kind='quote_mismatch', stable idempotency_key, opaque
          event_payload).

      (4) assert 202 response envelope (status, event_type, stream,
          source_loss_event_id, evidence_span_id, idempotency_key).

      (5) assert FakeRedis observed exactly one xadd with the expected
          stream and fields shape, and capture the fields dict. Also
          assert the source_loss_events row has been committed to the
          DB with the canonical scope (tenant/project/document_*
          derived from the evidence_span chain, task_id IS NULL).

      (6) hand the captured fields to dispatch.handle_event with a
          synthetic redis_consumer_name (the dispatcher must NOT
          forward this to the source_loss consumer).

      (7) assert DB post-processing:
            - claim_ledger_entries grew from 1 to 2 for the seeded
              claim; the head is unverifiable / unsupported / source_lost;
              its payload references the source_loss_event_id and span;
            - exactly one supersedes lineage edge from v1 to v2;
            - exactly one claim_marked_unverifiable/recorded propagation
              row, exactly one no_active_published_answers_impacted/recorded
              row;
            - exactly one source_loss.propagated_to_claim audit event on
              the task chain; verify_task_audit_chain ok=True;
            - task_masters.status invariant;
            - no published_answer_lifecycle_events were written for this
              task;
            - EPR row exists with consumer_name='source_loss' (NOT the
              dispatcher's redis_consumer_name), processing_status
              succeeded, scope (tenant_id, project_id) carried, task_id
              IS NULL because the source_loss row carries task_id NULL.

      (8) replay the same event with a different redis_consumer_name:
          dispatcher returns "skipped_already_succeeded"; DB unchanged.

      (9) replay a variant with a fresh consumer-level idempotency_key
          but the same source_loss_event_id. Dispatcher returns
          "processed" (fresh EPR slot), but the propagator's per-claim
          handler detects the head is already unverifiable/source_lost
          and emits a 'skipped' propagation row that hits ON CONFLICT
          DO NOTHING against the partial UNIQUE: ledger count stays 2,
          lineage stays 1, propagation rows stay (1 recorded + 0 skipped)
          for claim_marked_unverifiable, audit count stays 1.
    """
    _skip_if_db_unreachable()

    # ----------------------------- (1) seed -------------------------------
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        chain = _create_evidence_span_chain(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        claim_logical_id, original_entry_id = _create_claim_linked_to_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=chain["evidence_span_id"],
        )
        task_status_before = _fetch_task_status(conn, task_id=task_id)

    # Sanity: pre-call corpus state is what the test assumes.
    with engine.connect() as conn:
        assert _count_ledger_entries_for_claim(
            conn, claim_logical_id=claim_logical_id
        ) == 1
        latest_v1 = _fetch_claim_head_or_latest(
            conn, claim_logical_id=claim_logical_id
        )
        assert int(latest_v1["version_no"]) == 1
        assert str(latest_v1["state"]) == "verified_fact"
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 0

    # --------------------- (2) install FakeRedis --------------------------
    fake = _install_fake_redis(monkeypatch)
    client = TestClient(api_app)

    # --------------------- (3) POST source-loss-events --------------------
    consumer_idem_1 = f"realistic-source-loss-{_unique_hex()}"
    request_body = {
        "evidence_span_id": str(chain["evidence_span_id"]),
        "loss_kind": "quote_mismatch",
        "loss_reason": "realistic source loss",
        "idempotency_key": consumer_idem_1,
        "event_payload": {"scenario": "phase_8_5_source_loss_flow"},
    }

    resp = client.post("/api/v1/source-loss-events", json=request_body)

    # --------------------- (4) assert API response ------------------------
    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == EXPECTED_EVENT_TYPE
    assert rb["stream"] == EXPECTED_STREAM
    assert rb["evidence_span_id"] == str(chain["evidence_span_id"])
    assert rb["idempotency_key"] == consumer_idem_1
    response_event_id = uuid.UUID(rb["event_id"])
    response_sle_id = uuid.UUID(rb["source_loss_event_id"])

    # --------------------- (5a) assert FakeRedis --------------------------
    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    call = fake.xadd_calls[0]
    assert call["stream"] == EXPECTED_STREAM

    fields = call["fields"]
    # Every Redis stream field is string/string.
    for k, v in fields.items():
        assert isinstance(k, str)
        assert isinstance(v, str), f"field {k!r} not a str: {v!r}"

    # Required fields, with exact identity match against the request /
    # the resolved scope.
    assert fields["event_id"] == str(response_event_id)
    assert fields["event_type"] == EXPECTED_EVENT_TYPE
    assert fields["source_loss_event_id"] == str(response_sle_id)
    assert fields["evidence_span_id"] == str(chain["evidence_span_id"])
    assert fields["idempotency_key"] == consumer_idem_1
    assert fields["tenant_id"] == str(tenant_id)
    assert fields["project_id"] == str(project_id)
    assert fields["document_chunk_id"] == str(chain["document_chunk_id"])
    assert fields["document_version_id"] == str(chain["document_version_id"])
    assert fields["document_id"] == str(chain["document_id"])
    assert fields["loss_kind"] == "quote_mismatch"
    assert fields["loss_reason"] == "realistic source loss"
    assert fields["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    # event_payload is non-empty in this scenario, so the JSON-encoded
    # form is published. Producer uses separators=(",", ":") + sort_keys.
    assert fields["event_payload_json"] == json.dumps(
        request_body["event_payload"], separators=(",", ":"), sort_keys=True
    )

    # --------------------- (5b) assert source_loss_events DB row ----------
    with engine.connect() as conn:
        sle_row = _fetch_source_loss_event(conn, source_loss_event_id=response_sle_id)
    assert sle_row is not None
    assert sle_row["id"] == response_sle_id
    assert sle_row["tenant_id"] == tenant_id
    assert sle_row["project_id"] == project_id
    # The endpoint deliberately leaves task_id NULL: an evidence_span can
    # back claims belonging to multiple tasks, so there is no unique
    # task scope to materialize at producer time.
    assert sle_row["task_id"] is None
    assert sle_row["evidence_span_id"] == chain["evidence_span_id"]
    assert sle_row["document_chunk_id"] == chain["document_chunk_id"]
    assert sle_row["document_version_id"] == chain["document_version_id"]
    assert sle_row["document_id"] == chain["document_id"]
    assert sle_row["loss_kind"] == "quote_mismatch"
    assert sle_row["loss_reason"] == "realistic source loss"
    assert sle_row["detected_by"] == EXPECTED_DEFAULT_DETECTED_BY
    assert sle_row["idempotency_key"] == consumer_idem_1
    assert sle_row["event_payload"] == request_body["event_payload"]

    # --------------------- (6) dispatcher first run -----------------------
    # Reconstruct the event exactly the way the worker would after
    # decoding a Redis stream entry: a plain dict[str, str].
    event = dict(fields)

    rc = _dispatch.handle_event(
        event, redis_consumer_name="realistic_worker_source_loss_1"
    )
    assert rc == "processed"

    # --------------------- (7) post-processing assertions -----------------
    with engine.connect() as conn:
        # claim_ledger_entries: now 2 for the seeded claim.
        assert _count_ledger_entries_for_claim(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        latest = _fetch_claim_head_or_latest(
            conn, claim_logical_id=claim_logical_id
        )
        assert int(latest["version_no"]) == 2
        assert str(latest["state"]) == "unverifiable"
        assert str(latest["support_scope"]) == "unsupported"
        assert str(latest["user_provided_dependency"]) == "unsupported"
        assert str(latest["transition_reason"]) == "source_lost"

        # The propagator stamps source-loss context onto the new ledger
        # entry's payload. Assert the keys explicitly so a future
        # contract change is caught.
        payload = _normalize_jsonb(latest["payload"])
        assert isinstance(payload, dict)
        assert str(payload.get("source_loss_event_id")) == str(response_sle_id)
        assert str(payload.get("evidence_span_id")) == str(chain["evidence_span_id"])
        assert str(payload.get("previous_entry_id")) == str(original_entry_id)
        assert str(payload.get("previous_state")) == "verified_fact"
        assert str(payload.get("loss_kind")) == "quote_mismatch"

        new_entry_id = uuid.UUID(str(latest["id"]))

        # Lineage edge v1 -> v2 ('supersedes') exists exactly once.
        assert _count_claim_lineage_pair(
            conn,
            parent_entry_id=original_entry_id,
            child_entry_id=new_entry_id,
            relation_kind="supersedes",
        ) == 1

        # Propagation records:
        #   - 1 claim_marked_unverifiable / recorded
        #   - 1 no_active_published_answers_impacted / recorded
        # (We seeded NO published_answer for this task, so the propagator
        #  emits the dedicated 'no active' row alongside the per-claim
        #  record.)
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
        ) == 1
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="skipped",
        ) == 0
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="published_answer_impacted",
        ) == 0
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="no_active_published_answers_impacted",
            status="recorded",
        ) == 1
        # Total propagation rows for this source_loss_event: exactly 2.
        assert _count_source_loss_propagation_records(
            conn, source_loss_event_id=response_sle_id
        ) == 2

        # Audit: 'source_loss.propagated_to_claim' emitted exactly once
        # on the task's chain. Audit for published_answer impact must
        # NOT have been emitted (no impacted active PA).
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 1
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_PUBLISHED_ANSWER,
        ) == 0

        # Audit chain integrity verifies end-to-end after the new event.
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        # task_masters.status MUST stay invariant.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        # The source_loss pipeline must NOT touch
        # published_answer_lifecycle_events anywhere on this task.
        assert _count_lifecycle_events_global_for_task(
            conn, task_id=task_id
        ) == 0

        # EPR: keyed on the consumer's STABLE logical name and on the
        # consumer-level idempotency_key from the event. The dispatcher
        # MUST NOT have shadowed the consumer_name with
        # 'realistic_worker_source_loss_1' — verified both by the lookup
        # succeeding under WORKER_CONSUMER_NAME and (defensively) by
        # counting zero rows under the per-instance worker name.
        epr = _fetch_epr(
            conn,
            consumer_name=WORKER_CONSUMER_NAME,
            idempotency_key=consumer_idem_1,
        )
        assert epr is not None
        assert epr["processing_status"] == "succeeded"
        assert epr["tenant_id"] == tenant_id
        assert epr["project_id"] == project_id
        # source_loss_events.task_id is NULL by API contract, so the
        # consumer's resolved scope carries task_id=None and the EPR
        # row's task_id is NULL. This is the canonical behavior locked
        # down by apps/worker/tests/test_source_loss_consumer.py.
        assert epr["task_id"] is None
        assert _count_epr(
            conn,
            consumer_name="realistic_worker_source_loss_1",
            idempotency_key=consumer_idem_1,
        ) == 0

    # --------------------- (8) redelivery, same keys ----------------------
    rc2 = _dispatch.handle_event(
        event, redis_consumer_name="realistic_worker_source_loss_2"
    )
    assert rc2 == "skipped_already_succeeded"

    with engine.connect() as conn:
        # Ledger count unchanged.
        assert _count_ledger_entries_for_claim(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        # Lineage unchanged.
        assert _count_claim_lineage_pair(
            conn,
            parent_entry_id=original_entry_id,
            child_entry_id=new_entry_id,
            relation_kind="supersedes",
        ) == 1

        # Propagation records unchanged: still 1 recorded for
        # claim_marked_unverifiable and 1 recorded for
        # no_active_published_answers_impacted.
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="claim_marked_unverifiable",
        ) == 1
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="no_active_published_answers_impacted",
        ) == 1
        assert _count_source_loss_propagation_records(
            conn, source_loss_event_id=response_sle_id
        ) == 2

        # Audit unchanged.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 1

        # Exactly one EPR row keyed on (stable consumer, consumer_idem_1).
        assert _count_epr(
            conn,
            consumer_name=WORKER_CONSUMER_NAME,
            idempotency_key=consumer_idem_1,
        ) == 1

        # task_masters.status invariant survives the redelivery.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

    # --------------------- (9) fresh consumer key, same SLE id ------------
    # Fresh consumer-level EPR slot. The propagator runs end-to-end again,
    # but the per-claim handler detects the head is already
    # unverifiable/source_lost and emits a 'skipped' propagation row that
    # hits ON CONFLICT DO NOTHING against
    # slpr_claim_marked_unverifiable_uq (which covers BOTH 'recorded' and
    # 'skipped' statuses for the same source_loss_event_id +
    # claim_logical_id). Net effect: ledger count stays 2, lineage stays
    # 1, propagation rows for claim_marked_unverifiable stay at 1 (still
    # 'recorded'), audit count stays 1.
    consumer_idem_3 = f"realistic-source-loss-{_unique_hex()}"
    event_third: dict[str, Any] = dict(event)
    event_third["event_id"] = str(uuid.uuid4())
    event_third["idempotency_key"] = consumer_idem_3

    rc3 = _dispatch.handle_event(
        event_third, redis_consumer_name="realistic_worker_source_loss_3"
    )
    assert rc3 == "processed"

    with engine.connect() as conn:
        # Ledger UNCHANGED.
        assert _count_ledger_entries_for_claim(
            conn, claim_logical_id=claim_logical_id
        ) == 2

        # Lineage UNCHANGED.
        assert _count_claim_lineage_pair(
            conn,
            parent_entry_id=original_entry_id,
            child_entry_id=new_entry_id,
            relation_kind="supersedes",
        ) == 1

        # Propagation rows UNCHANGED: 1 recorded for
        # claim_marked_unverifiable, 0 skipped (the partial UNIQUE
        # absorbs the 'skipped' insert via ON CONFLICT DO NOTHING).
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="recorded",
        ) == 1
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="claim_marked_unverifiable",
            status="skipped",
        ) == 0
        assert _count_source_loss_propagation_records(
            conn,
            source_loss_event_id=response_sle_id,
            propagation_kind="no_active_published_answers_impacted",
        ) == 1

        # Audit UNCHANGED: the per-call audit gate fires only when the
        # propagation INSERT actually creates a new row (RETURNING id),
        # and that did not happen this time.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type=AUDIT_EVENT_PROPAGATED_TO_CLAIM,
        ) == 1

        # Two EPR rows total under the stable consumer_name, both
        # succeeded: one for consumer_idem_1 and one for consumer_idem_3.
        assert _count_epr(
            conn,
            consumer_name=WORKER_CONSUMER_NAME,
            idempotency_key=consumer_idem_1,
        ) == 1
        epr_third = _fetch_epr(
            conn,
            consumer_name=WORKER_CONSUMER_NAME,
            idempotency_key=consumer_idem_3,
        )
        assert epr_third is not None
        assert epr_third["processing_status"] == "succeeded"
        assert epr_third["tenant_id"] == tenant_id
        assert epr_third["project_id"] == project_id
        assert epr_third["task_id"] is None  # source_loss_events.task_id IS NULL

        # task_masters.status invariant survives every redelivery.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        # And the audit chain still verifies after all three deliveries.
        chain_ok_final = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok_final["ok"] is True, chain_ok_final
