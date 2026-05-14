"""Phase 8.6 — Realistic Read Flow Test.

Scope (cross-component, root-tests):
  Demonstrate that the 8.6 read-only HTTP endpoints observe, via the API
  surface, the DB effects produced by the asynchronous 8.5 pipeline.

  Two independent end-to-end-like flows are exercised in this file:

    A) Withdrawal read flow
       - seed: tenant/user/project/task + draft v1 + approved gate +
         published_answer status='published';
       - POST /api/v1/published-answers/{id}/withdrawal-requests with an
         explicit idempotency_key;
       - FakeRedis captures the published_answer.withdrawal_requested
         event;
       - dispatch.handle_event(event) drives the worker withdrawal
         consumer + lifecycle service against the real DB;
       - assert dispatcher status == "processed";
       - GET /api/v1/published-answers/{id}/lifecycle-events: HTTP 200,
         items contains exactly [withdrawal_requested, withdrawn] ASC;
       - GET /api/v1/published-answers/{id}: status == "withdrawn";
       - verify_task_audit_chain ok=True.

    B) Source-loss read flow
       - seed: tenant/user/project/task + storage chain ending in
         evidence_span + logical_claim verified_fact v1 +
         claim_evidence_link binding the verified entry to the span;
       - POST /api/v1/source-loss-events with explicit idempotency_key,
         loss_kind='quote_mismatch', non-empty event_payload;
       - FakeRedis captures the source_loss.detected event;
       - dispatch.handle_event(event) drives the worker source_loss
         consumer + propagator against the real DB;
       - assert dispatcher status == "processed";
       - GET /api/v1/source-loss-events/{id}: HTTP 200, fields surfaced
         including task_id == null and event_payload roundtrip;
       - GET /api/v1/source-loss-events/{id}/propagation: HTTP 200,
         items contains at least one claim_marked_unverifiable/recorded
         row AND at least one no_active_published_answers_impacted/recorded
         row (the propagator emits both for a task with no active
         published_answer);
       - GET /api/v1/tasks/{task_id}/source-loss-events: HTTP 200,
         contains the seeded SLE with impacted_via == "claim_evidence_link"
         and source_loss_event.task_id == null (NULL must NOT be
         camouflaged on the SLE itself, by design — the producer leaves
         task_id NULL because an evidence_span can back claims of
         multiple tasks);
       - claim head: state='unverifiable', support_scope='unsupported',
         transition_reason='source_lost';
       - verify_task_audit_chain ok=True.

Strict scope (Phase 8.6 — read-only observability):
  - No Source Quality Evaluator. The propagation rows surfaced by the
    8.6 endpoints record THAT a source was lost and the system reacted,
    not WHETHER the lost source was authoritative, primary, independent
    or fresh. The quality of sources is out of scope for 8.6.
  - source_loss does NOT auto-withdraw published_answers. The 8.5 plan
    locks this in: even when a SLE impacts an active PA, the PA status
    is not changed. The withdrawal pipeline is separate.
  - source_loss_events.task_id stays NULL for API-originated rows. The
    8.6D endpoint surfaces NULL verbatim and uses impacted_via to
    signal that the task scope was resolved via claim_evidence_links.

What this test is and is not:
  - It IS a realistic exercise of the producer + dispatcher + consumer
    + service pipeline against a real Postgres, observed end-to-end via
    the 8.6 HTTP endpoints. Every layer below the Redis transport runs
    the production code path; only Redis itself is a FakeRedis that
    records xadd calls.
  - It is NOT a Redis-loop test: no XREADGROUP, no consumer groups, no
    worker main() loop. Transport semantics are out of scope here.

Hard package-collision note (same as the 8.5 realistic-flow tests):
  Both apps/api/app and apps/worker/app are top-level packages literally
  named ``app``. We:
    1) prepend apps/api + packages/shared to sys.path so ``import app``
       resolves to the API,
    2) import API normally (``from app.main import app as api_app``,
       ``from app.routes import answers as answers_route``,
       ``from app.routes import source_loss as source_loss_route``),
    3) load the worker package via importlib.util under a synthetic
       top-level alias ``_wapp``, registering every submodule in
       sys.modules so the worker's relative imports resolve within its
       own namespace.
  When this test runs alongside the 8.5 realistic flow tests in the
  same interpreter, the ``_wapp`` namespace is already populated and
  ``_bootstrap_worker()`` short-circuits — all flow tests share the
  same worker module instances, which is fine since they share a DB.

DB requirement:
  Same Postgres used by ``make test-db`` — DATABASE_URL is set, the
  migrations (0001..0006) are applied. We never set DATABASE_URL
  ourselves; if it is missing or unreachable, the test is skipped.
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
# This file lives at <repo>/tests/test_phase_8_6_read_flow.py, so
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
from app.routes import answers as answers_route  # noqa: E402
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
# same pytest session as the 8.5 realistic-flow tests, the alias is
# already populated and we just retrieve dispatch from sys.modules.
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

    Identical implementation to the 8.5 realistic-flow tests: when run
    alongside them in the same session this returns the same dispatch
    module instance, so no double-load occurs.
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
WITHDRAWAL_STREAM = "app.events.published_answer_withdrawal_requested"
WITHDRAWAL_EVENT_TYPE = "published_answer.withdrawal_requested"
SOURCE_LOSS_STREAM = "app.events.source_loss_detected"
SOURCE_LOSS_EVENT_TYPE = "source_loss.detected"


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    We do NOT require REDIS_URL because this test installs a FakeRedis
    on the relevant API route modules and never invokes the worker's
    Redis loop. The DB is the only external dependency.
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
    We accept both forms so the assertions are robust across
    environments.
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
    """Ensure tenant + user; create a FRESH project and task per
    invocation.

    Returns (tenant_id, project_id, user_id, task_id).

    Project and task are always fresh so each test invocation operates
    on an isolated scope (no cross-test interference on task-scoped
    audit chains, no UNIQUE collisions on project name).
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
                {"t": tenant_id, "n": f"phase-8-6-read-flow-{uuid.uuid4()}"},
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
# DB seeding helpers — published_answer chain (for withdrawal flow)
# ---------------------------------------------------------------------------
def _create_published_answer(
    conn: Connection, *, task_id: uuid.UUID
) -> uuid.UUID:
    """Create draft_final_answers v1 + approved final_gate_reports +
    published_answers v1 (status='published') for the given task.

    Returns published_answer_id.

    The FK chain task -> draft -> gate -> published is locked down by
    the composite UNIQUE/FK declared in 0005_answers_gate.sql. We keep
    the chain minimal (no spans, no claim links): the producer endpoint
    resolves only through (published_answers JOIN task_masters), and
    the lifecycle service only touches published_answers itself plus
    published_answer_lifecycle_events.
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
# DB seeding helpers — evidence_span chain (for source-loss flow)
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

    Returns the chain ids dict; mirrors the helpers in the existing
    realistic flow tests / 8.6 endpoint tests.

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
        f"Phase 8.6 read flow marker {marker}. "
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
      - one claim_ledger_entries v1 row in state 'verified_fact' (the
        typical head state of a published claim);
      - one claim_evidence_links row connecting (logical, entry) to the
        given evidence_span (cel_origin_xor honored:
        retrieved_source_span_id stays NULL).

    Returns (claim_logical_id, claim_ledger_entry_id_v1).

    This is the minimal corpus state the propagator needs to mark the
    claim 'unverifiable / source_lost' on a source loss against the
    span. The lc_task_canonical_uq UNIQUE constraint requires
    canonical_claim_hash to be unique per task; we use _unique_hex()
    for that, making the seed rerun-safe.
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
def _fetch_claim_head(
    conn: Connection, *, claim_logical_id: uuid.UUID
) -> dict[str, Any]:
    """Return the latest claim_ledger_entries row (highest version_no)
    for the given claim. Caller must have already inserted at least one
    ledger entry — otherwise this raises NoResultFound.
    """
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


# ---------------------------------------------------------------------------
# FakeRedis
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub.

    Only ``xadd`` is implemented — that is the entire Redis surface
    used by ``app.routes.answers.request_published_answer_withdrawal``
    and by ``app.routes.source_loss.create_source_loss_event``. We
    deliberately do NOT add other Redis methods; this object is meant
    to be installed as the return value of the routes' ``get_redis``
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


# ===========================================================================
# TEST A — Withdrawal read flow
# ===========================================================================
def test_phase_8_6_withdrawal_read_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A) Withdrawal read flow.

    Drive the 8.5 withdrawal pipeline end-to-end-like, then observe its
    effects exclusively through the 8.6A read endpoint and the existing
    8.4 published-answer GET.

    Steps:
      1. Seed a published_answer in status='published'.
      2. Install FakeRedis on the answers route module.
      3. POST /api/v1/published-answers/{pa_id}/withdrawal-requests
         with an explicit idempotency_key.
      4. Assert 202 envelope and capture the FakeRedis fields.
      5. Drive dispatch.handle_event(fields) — assert "processed".
      6. GET /api/v1/published-answers/{pa_id}/lifecycle-events.
         Assert HTTP 200 and items == [withdrawal_requested, withdrawn]
         in ASC order.
      7. GET /api/v1/published-answers/{pa_id}. Assert
         status=="withdrawn".
      8. Assert task audit chain ok=True.
      9. (Sanity) task_masters.status invariant — the lifecycle lives
         on published_answers, not on the task.
    """
    _skip_if_db_unreachable()

    # ----------------------------- (1) seed -------------------------------
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        pa_id = _create_published_answer(conn, task_id=task_id)
        task_status_before = str(
            conn.execute(
                text("SELECT status FROM task_masters WHERE id = :t"),
                {"t": task_id},
            ).scalar_one()
        )

    # --------------------- (2) install FakeRedis --------------------------
    fake = FakeRedis()
    monkeypatch.setattr(answers_route, "get_redis", lambda: fake)
    client = TestClient(api_app)

    # --------------------- (3) POST withdrawal-requests -------------------
    consumer_idem = f"read-flow-withdrawal-consumer-{_unique_hex()}"
    lifecycle_idem = f"read-flow-withdrawal-lifecycle-{_unique_hex()}"
    request_body = {
        "reason": "phase 8.6 read flow withdrawal",
        "idempotency_key": consumer_idem,
        "lifecycle_idempotency_key": lifecycle_idem,
        "requested_by": str(user_id),
        "event_payload": {"scenario": "phase_8_6_withdrawal_read_flow"},
    }

    resp = client.post(
        f"/api/v1/published-answers/{pa_id}/withdrawal-requests",
        json=request_body,
    )

    # --------------------- (4) assert API + FakeRedis --------------------
    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == WITHDRAWAL_EVENT_TYPE
    assert rb["stream"] == WITHDRAWAL_STREAM
    assert rb["published_answer_id"] == str(pa_id)
    assert rb["idempotency_key"] == consumer_idem
    assert rb["lifecycle_idempotency_key"] == lifecycle_idem

    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    call = fake.xadd_calls[0]
    assert call["stream"] == WITHDRAWAL_STREAM
    fields = call["fields"]
    for k, v in fields.items():
        assert isinstance(k, str)
        assert isinstance(v, str), f"field {k!r} not a str: {v!r}"

    # --------------------- (5) dispatcher: drive the worker --------------
    event = dict(fields)
    rc = _dispatch.handle_event(
        event, redis_consumer_name="read_flow_worker_withdrawal_1"
    )
    assert rc == "processed", rc

    # --------------------- (6) GET lifecycle-events ----------------------
    lifecycle_resp = client.get(
        f"/api/v1/published-answers/{pa_id}/lifecycle-events"
    )
    assert lifecycle_resp.status_code == 200, lifecycle_resp.text
    lifecycle_body = lifecycle_resp.json()
    assert lifecycle_body["published_answer_id"] == str(pa_id)

    items = lifecycle_body["items"]
    assert isinstance(items, list)
    # Exactly two events. The endpoint orders by (created_at, id), not by
    # lifecycle semantics. Since the lifecycle service writes both rows in
    # the same DB transaction, created_at can be identical; UUID ordering may
    # put either event first. Assert the set, not positional order.
    assert len(items) == 2, items
    event_types = {it["event_type"] for it in items}
    assert event_types == {"withdrawal_requested", "withdrawn"}

    # Field-level sanity on both items: the lifecycle service stamps
    # the same idempotency_key on both rows (it is the
    # lifecycle_idempotency_key from the event), and both rows are
    # bound to the same (published_answer_id, task_id) pair.
    for it in items:
        assert it["published_answer_id"] == str(pa_id)
        assert it["task_id"] == str(task_id)
        assert it["idempotency_key"] == lifecycle_idem
        assert it["requested_by"] == str(user_id)
        assert isinstance(it["event_payload"], dict)

    # --------------------- (7) GET published-answer ----------------------
    # The 8.4 GET /api/v1/published-answers/{id} endpoint returns the
    # row by id (mounted in apps/api/app/routes/answers.py); after the
    # worker transition we expect status="withdrawn".
    pa_get = client.get(f"/api/v1/published-answers/{pa_id}")
    assert pa_get.status_code == 200, pa_get.text
    pa_body = pa_get.json()
    assert pa_body["id"] == str(pa_id)
    assert pa_body["status"] == "withdrawn"
    # withdrawn_at must be populated by the status-guarded UPDATE in
    # the lifecycle service; we just check it is non-null without
    # parsing the timestamp.
    assert pa_body.get("withdrawn_at") is not None

    # --------------------- (8) audit chain integrity ---------------------
    with engine.connect() as conn:
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
    assert chain_ok["ok"] is True, chain_ok

    # --------------------- (9) task_masters.status invariant -------------
    with engine.connect() as conn:
        task_status_after = str(
            conn.execute(
                text("SELECT status FROM task_masters WHERE id = :t"),
                {"t": task_id},
            ).scalar_one()
        )
    assert task_status_after == task_status_before


# ===========================================================================
# TEST B — Source-loss read flow
# ===========================================================================
def test_phase_8_6_source_loss_read_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B) Source-loss read flow.

    Drive the 8.5 source_loss pipeline end-to-end-like, then observe
    its effects exclusively through the 8.6B / 8.6C / 8.6D read
    endpoints.

    Steps:
      1. Seed task + evidence_span chain + one logical_claim with v1
         verified_fact + claim_evidence_link binding the v1 entry to
         the seeded span.
      2. Install FakeRedis on the source_loss route module.
      3. POST /api/v1/source-loss-events with explicit
         idempotency_key, loss_kind='quote_mismatch', non-empty
         event_payload.
      4. Assert 202 envelope and capture the FakeRedis fields.
      5. Drive dispatch.handle_event(fields) — assert "processed".
      6. GET /api/v1/source-loss-events/{id}. Assert HTTP 200 and
         fields:
           - id matches;
           - evidence_span_id matches;
           - task_id is null (by design — producer leaves it NULL);
           - event_payload roundtrip.
      7. GET /api/v1/source-loss-events/{id}/propagation. Assert
         HTTP 200 and items contains:
           - at least one claim_marked_unverifiable / recorded row;
           - at least one no_active_published_answers_impacted /
             recorded row.
      8. GET /api/v1/tasks/{task_id}/source-loss-events. Assert
         HTTP 200 and the seeded SLE is listed with:
           - impacted_via == "claim_evidence_link";
           - source_loss_event.task_id is null (NOT camouflaged).
      9. Assert task audit chain ok=True.
     10. Assert claim head: state='unverifiable',
         support_scope='unsupported',
         transition_reason='source_lost'.
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
        claim_logical_id, _original_entry_id = _create_claim_linked_to_span(
            conn,
            tenant_id=tenant_id,
            project_id=project_id,
            task_id=task_id,
            evidence_span_id=chain["evidence_span_id"],
        )

    # --------------------- (2) install FakeRedis --------------------------
    fake = FakeRedis()
    monkeypatch.setattr(source_loss_route, "get_redis", lambda: fake)
    client = TestClient(api_app)

    # --------------------- (3) POST source-loss-events -------------------
    consumer_idem = f"read-flow-source-loss-{_unique_hex()}"
    seeded_payload = {
        "scenario": "phase_8_6_source_loss_read_flow",
        "extra": {"nested": True},
    }
    request_body = {
        "evidence_span_id": str(chain["evidence_span_id"]),
        "loss_kind": "quote_mismatch",
        "loss_reason": "phase 8.6 read flow source loss",
        "idempotency_key": consumer_idem,
        "event_payload": seeded_payload,
    }

    resp = client.post("/api/v1/source-loss-events", json=request_body)

    # --------------------- (4) assert API + FakeRedis --------------------
    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == SOURCE_LOSS_EVENT_TYPE
    assert rb["stream"] == SOURCE_LOSS_STREAM
    assert rb["evidence_span_id"] == str(chain["evidence_span_id"])
    assert rb["idempotency_key"] == consumer_idem
    sle_id = uuid.UUID(rb["source_loss_event_id"])

    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    call = fake.xadd_calls[0]
    assert call["stream"] == SOURCE_LOSS_STREAM
    fields = call["fields"]
    for k, v in fields.items():
        assert isinstance(k, str)
        assert isinstance(v, str), f"field {k!r} not a str: {v!r}"

    # --------------------- (5) dispatcher: drive the worker --------------
    event = dict(fields)
    rc = _dispatch.handle_event(
        event, redis_consumer_name="read_flow_worker_source_loss_1"
    )
    assert rc == "processed", rc

    # --------------------- (6) GET single source-loss-event --------------
    sle_resp = client.get(f"/api/v1/source-loss-events/{sle_id}")
    assert sle_resp.status_code == 200, sle_resp.text
    sle_body = sle_resp.json()

    assert sle_body["id"] == str(sle_id)
    assert sle_body["evidence_span_id"] == str(chain["evidence_span_id"])
    # CRITICAL: by design, the API producer leaves task_id NULL on
    # source_loss_events (a span may back claims of multiple tasks).
    # The 8.6B endpoint surfaces NULL verbatim.
    assert sle_body["task_id"] is None
    # event_payload roundtrip — equal to what we sent.
    assert isinstance(sle_body["event_payload"], dict)
    assert sle_body["event_payload"] == seeded_payload
    # Sanity on the scope columns derived by the producer from the
    # evidence_span chain (tenant_id is NOT NULL on the schema).
    assert sle_body["tenant_id"] == str(tenant_id)
    assert sle_body["project_id"] == str(project_id)
    assert sle_body["loss_kind"] == "quote_mismatch"
    assert sle_body["idempotency_key"] == consumer_idem

    # --------------------- (7) GET propagation ---------------------------
    prop_resp = client.get(f"/api/v1/source-loss-events/{sle_id}/propagation")
    assert prop_resp.status_code == 200, prop_resp.text
    prop_body = prop_resp.json()
    assert prop_body["source_loss_event_id"] == str(sle_id)
    prop_items = prop_body["items"]
    assert isinstance(prop_items, list)

    # The propagator emits at least:
    #   - one claim_marked_unverifiable/recorded for the seeded claim;
    #   - one no_active_published_answers_impacted/recorded because we
    #     intentionally did NOT seed a published_answer for this task.
    # We assert "at least one" of each kind/status so this test stays
    # robust against future propagator enhancements that might emit
    # additional records.
    has_claim_marked_unverifiable_recorded = any(
        it["propagation_kind"] == "claim_marked_unverifiable"
        and it["status"] == "recorded"
        for it in prop_items
    )
    has_no_active_pa_impacted_recorded = any(
        it["propagation_kind"] == "no_active_published_answers_impacted"
        and it["status"] == "recorded"
        for it in prop_items
    )
    assert has_claim_marked_unverifiable_recorded, prop_items
    assert has_no_active_pa_impacted_recorded, prop_items

    # The claim_marked_unverifiable row must reference our seeded
    # logical claim (idempotency target of the partial unique index).
    matching_claim_rows = [
        it
        for it in prop_items
        if it["propagation_kind"] == "claim_marked_unverifiable"
        and it["status"] == "recorded"
        and it["claim_logical_id"] == str(claim_logical_id)
    ]
    assert len(matching_claim_rows) == 1, matching_claim_rows

    # --------------------- (8) GET task source-loss-events ---------------
    task_sle_resp = client.get(f"/api/v1/tasks/{task_id}/source-loss-events")
    assert task_sle_resp.status_code == 200, task_sle_resp.text
    task_sle_body = task_sle_resp.json()
    assert task_sle_body["task_id"] == str(task_id)
    task_sle_items = task_sle_body["items"]
    assert isinstance(task_sle_items, list)

    # The seeded SLE must appear in the task-level listing. Because the
    # producer leaves source_loss_events.task_id NULL, the SLE is
    # reached via S2 (claim_evidence_link), so impacted_via must be
    # "claim_evidence_link". The SLE row's task_id MUST stay JSON
    # null — the endpoint never camouflages NULL on the SLE.
    matching = [
        it
        for it in task_sle_items
        if it["source_loss_event"]["id"] == str(sle_id)
    ]
    assert len(matching) == 1, task_sle_items
    item = matching[0]
    assert item["impacted_via"] == "claim_evidence_link"
    assert item["source_loss_event"]["task_id"] is None
    assert item["source_loss_event"]["evidence_span_id"] == str(
        chain["evidence_span_id"]
    )

    # --------------------- (9) audit chain integrity ---------------------
    with engine.connect() as conn:
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
    assert chain_ok["ok"] is True, chain_ok

    # --------------------- (10) claim head invariants --------------------
    # The propagator's per-claim handler appended a v2 ledger entry
    # in state='unverifiable' / support_scope='unsupported' /
    # transition_reason='source_lost' for the seeded claim. We
    # inspect the DB directly here (no 8.6 endpoint exposes ledger
    # entries by claim_logical_id — that surface is the 8.4 claims
    # endpoints, not 8.6).
    with engine.connect() as conn:
        head = _fetch_claim_head(conn, claim_logical_id=claim_logical_id)
    assert int(head["version_no"]) == 2
    assert str(head["state"]) == "unverifiable"
    assert str(head["support_scope"]) == "unsupported"
    assert str(head["transition_reason"]) == "source_lost"
