"""Phase 8.8A-GATE-FLOW — Realistic Claim Entailment Gate Flow Test.

Scope (cross-component, root-tests):
  Two end-to-end-like flows that exercise the 8.8A claim entailment step
  inside the real task.created pipeline, AND the 8.8A-GATE policy that
  consumes ``claim_entailment_checks`` from the Final Answer Gate. Both
  flows go through the full API HTTP surface, the dispatcher, the
  consumer, and the worker services against the real Postgres.

  A) Warning path (Branch W with the real mock checker):
     - API HTTP creates project + document + task;
     - FakeRedis captures the task.created event;
     - ``_dispatch.handle_event(...)`` drives the worker through:
         extractor -> CVE-lite -> 8.7E source quality (mock evaluator)
         -> 8.8A claim entailment (mock checker) -> compiler -> Gate;
     - The mock entailment checker NEVER emits 'contradicted', so the
       Gate's entailment axis can only contribute warnings (the mock
       typically emits 'entailed' via the containment rule when
       extractor-produced raw_claim text equals the supporting quote,
       and 'uncertain' or 'not_supported' otherwise);
     - The mock Source Quality evaluator writes
       overall_quality='unknown' + contradiction_status='unchecked',
       which the Gate maps to a source_quality_warning per span;
     - End state: decision='approved',
       reason_code='all_spans_verified_with_warnings',
       at least one ``claim_entailment_checks`` row, at least a
       source_quality_warning OR an entailment_warning gap exists, and
       ``published_answers`` v1 status='published'.

  B) Block path (Branch E via orchestrator stub):
     - Same API HTTP path for project + document + task creation;
     - FakeRedis captures the task.created event;
     - The symbol ``_wapp.consumers.task_created.run_claim_entailment_checks``
       is monkey-patched with a stub that, for each
       (claim_ledger_entry_id, evidence_span_id) pair linked to the
       task, INSERTs a v1 ``claim_entailment_checks`` row with
       ``verdict='contradicted'`` and returns the canonical counts dict
       the consumer audit-payload code expects;
     - ``_dispatch.handle_event(...)`` drives the worker; the Gate
       finds verdict='contradicted' for at least one supporting pair
       on every verified-backed span, rejects with
       reason_code='entailment_block', emits an entailment_block
       coverage_gap_statements row (severity='block') for each
       blocked span, does NOT insert any ``published_answers`` v1, and
       leaves the task in status='analyzed_partial' with
       task.publication_held as the terminal audit event.

What this test is and is not:
  - It IS a realistic exercise of the producer + dispatcher + consumer
    + services pipeline against a real Postgres. Every layer below the
    Redis transport runs the production code path; only Redis is a
    FakeRedis that records xadd calls. The block flow replaces ONLY
    the orchestrator symbol bound on the consumer module so the
    Branch E (entailment_block) can be activated end-to-end (the real
    mock checker never produces 'contradicted').
  - It is NOT a Redis-loop test: no XREADGROUP, no consumer groups,
    no worker main() loop.
  - It does NOT exercise the Final Answer Gate directly; the Gate is
    invoked by the pipeline as part of task.created processing.
  - It does NOT modify the Gate, the consumer, the checker, the
    orchestrator, the migrations, or any shared schema.

Anti-hallucination disclaimer (per PHASE_8_8A_PRE.md / GATE-PRE):
  The system is designed to prevent factual claims that are unsupported,
  contradicted, or based on inadequate sources from being published as
  reliable. It does NOT promise to eliminate all hallucinations. The
  mock entailment checker is a deterministic 3-rule heuristic and is
  not a real NLI/LLM entailment model.

Hard package-collision note (same as the 8.5 / 8.6 / 8.7H realistic-flow tests):
  Both apps/api/app and apps/worker/app are top-level packages literally
  named ``app``. We:
    1) prepend apps/api + packages/shared to sys.path so ``import app``
       resolves to the API,
    2) import API normally,
    3) load the worker package via importlib.util under a synthetic
       top-level alias ``_wapp``, registering every submodule in
       sys.modules so the worker's relative imports resolve within its
       own namespace.

DB requirement:
  Same Postgres used by ``make test-db`` — DATABASE_URL is set, the
  migrations (0001..0010) are applied. We never set DATABASE_URL
  ourselves; if it is missing or unreachable, the test is skipped.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# sys.path setup — runs at import time, BEFORE any project import
# ---------------------------------------------------------------------------
# This file lives at <repo>/tests/test_phase_8_8a_entailment_gate_flow.py,
# so parents[1] is the repo root. We deliberately do NOT add apps/worker
# to sys.path: worker is loaded by file path under an alias namespace
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
from app.routes import tasks as tasks_route  # noqa: E402

# Shared helpers used to validate the audit chain end-to-end.
from evidencefirst_shared.db.audit import verify_task_audit_chain  # noqa: E402


# ---------------------------------------------------------------------------
# Worker bootstrap under alias namespace ``_wapp``
# ---------------------------------------------------------------------------
# We load apps/worker/app/* under the synthetic top-level name ``_wapp``
# so that worker's relative imports resolve within their own namespace
# and do not collide with API's ``app``. Must happen exactly once per
# interpreter; subsequent calls reuse the cached entries. When run
# alongside the 8.5 / 8.6 / 8.7H realistic-flow tests, the alias is
# already populated and we just retrieve dispatch from sys.modules.
# The 8.8A-GATE-FLOW bootstrap is a superset of the 8.7H one: it ALSO
# explicitly loads claim_entailment_checker and
# claim_entailment_orchestrator (the two services introduced in
# 8.8A-SERVICE / 8.8A-ORCHESTRATOR).
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

    The 8.8A-GATE-FLOW bootstrap explicitly loads
    claim_entailment_checker and claim_entailment_orchestrator. If
    earlier flow tests already populated the alias without those two
    modules (they get loaded transitively when task_created is
    imported), the _load_mod calls are short-circuited via the
    sys.modules check.
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
    # claim_entailment_checker and claim_entailment_orchestrator are
    # listed explicitly so the dependency surface is documented in
    # this file even if earlier flow tests already loaded them via the
    # task_created import cascade.
    for _svc in (
        "compiler",
        "cve_lite",
        "extractor",
        "final_answer_gate",
        "published_answer_lifecycle",
        "source_loss_propagator",
        "source_quality_evaluator",
        "source_quality_orchestrator",
        "claim_entailment_checker",
        "claim_entailment_orchestrator",
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
TASK_CREATED_STREAM = "app.events.task_created"
TASK_CREATED_EVENT_TYPE = "task.created"

# Document contents: must contain at least one sentence with digits so
# the mock-driven extractor emits raw_claim rows and the CVE-lite step
# produces verified_fact v2 entries. Without digits, the pipeline would
# enter Branch A of the Gate (no_verified_claims) and the test would
# not exercise the Entailment branch at all.
_DOC_CONTENT_BYTES = (
    b"Revenue reached 12500000 USD in 2024. "
    b"The company added 3412 customers in Q3. "
    b"Sales grew by 37 percent.\n"
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    We do NOT require REDIS_URL because every test in this file installs
    a FakeRedis on the API tasks route module and never invokes the
    worker's Redis loop. The DB is the only external dependency.
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
    Accept both so the assertions are robust across environments.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# DB seeding helpers — minimum dev tenant + user
# ---------------------------------------------------------------------------
def _seeded_dev(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure tenant 'dev' + user 'dev@local' exist.

    The API project/task/document routes resolve the dev tenant via
    DEV_TENANT_SLUG ('dev') and the dev user via DEV_USER_EMAIL
    ('dev@local'). The tenant + user are created idempotently here so
    the test does not depend on the external ``make seed`` step.

    Returns (tenant_id, user_id).
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
    return tenant_id, user_id


# ---------------------------------------------------------------------------
# API helpers — create project / upload document / create task
# ---------------------------------------------------------------------------
def _create_project_via_api(client: TestClient) -> uuid.UUID:
    """POST /api/v1/projects with a unique name; return project_id."""
    body = {
        "name": f"phase-8-8a-gate-flow-{uuid.uuid4()}",
        "mode_default": "closed_corpus",
    }
    resp = client.post("/api/v1/projects", json=body)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _upload_document_via_api(
    client: TestClient, *, project_id: uuid.UUID
) -> uuid.UUID:
    """POST /api/v1/projects/{id}/documents with a multipart upload of
    a .txt file. Returns document_id.

    Content contains sentences with digits so the extractor mock can
    identify factual claims (the extractor filters sentences that
    contain at least one digit).
    """
    filename = f"doc-{uuid.uuid4().hex[:12]}.txt"
    files = {
        "file": (filename, io.BytesIO(_DOC_CONTENT_BYTES), "text/plain"),
    }
    resp = client.post(
        f"/api/v1/projects/{project_id}/documents",
        files=files,
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _create_task_via_api(
    client: TestClient, *, project_id: uuid.UUID, document_ids: list[uuid.UUID]
) -> uuid.UUID:
    """POST /api/v1/tasks with the given document_ids. Returns task_id."""
    body = {
        "project_id": str(project_id),
        "objective": f"phase 8.8a gate flow {uuid.uuid4()}",
        "mode": "closed_corpus",
        "policy": {},
        "document_ids": [str(d) for d in document_ids],
    }
    resp = client.post("/api/v1/tasks", json=body)
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_task_status(conn: Connection, *, task_id: uuid.UUID) -> str:
    return str(
        conn.execute(
            text("SELECT status FROM task_masters WHERE id = :t"),
            {"t": task_id},
        ).scalar_one()
    )


def _fetch_audit_event_types_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> list[tuple[int, str]]:
    """Return [(chain_seq, event_type), ...] ordered by chain_seq ASC."""
    rows = conn.execute(
        text(
            """
            SELECT chain_seq, event_type
            FROM audit_records
            WHERE chain_scope = 'task' AND scope_id = :t
            ORDER BY chain_seq ASC
            """
        ),
        {"t": task_id},
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


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


def _fetch_final_gate_report(
    conn: Connection, *, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, task_id, draft_final_answer_id,
                   decision, reason_code, payload
            FROM final_gate_reports
            WHERE task_id = :t
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"t": task_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "id": uuid.UUID(str(m["id"])),
        "task_id": uuid.UUID(str(m["task_id"])),
        "draft_final_answer_id": uuid.UUID(str(m["draft_final_answer_id"])),
        "decision": str(m["decision"]),
        "reason_code": str(m["reason_code"]),
        "payload": _normalize_jsonb(m["payload"]),
    }


def _fetch_coverage_gaps(
    conn: Connection, *, draft_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT id, kind, severity, gap_key, details
            FROM coverage_gap_statements
            WHERE draft_final_answer_id = :did
            ORDER BY kind ASC, gap_key ASC
            """
        ),
        {"did": draft_id},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        m = dict(r._mapping)
        m["details"] = _normalize_jsonb(m.get("details"))
        out.append(m)
    return out


def _fetch_published(
    conn: Connection, *, task_id: uuid.UUID
) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """
            SELECT id, status, version_no
            FROM published_answers
            WHERE task_id = :t AND version_no = 1
            """
        ),
        {"t": task_id},
    ).first()
    if row is None:
        return None
    m = row._mapping
    return {
        "id": uuid.UUID(str(m["id"])),
        "status": str(m["status"]),
        "version_no": int(m["version_no"]),
    }


def _count_published_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM published_answers WHERE task_id = :t"
            ),
            {"t": task_id},
        ).scalar_one()
    )


def _distinct_pairs_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Return DISTINCT (claim_ledger_entry_id, evidence_span_id) pairs
    linked to this task via ``claim_evidence_links JOIN logical_claims``.

    Mirrors the discovery query the entailment orchestrator and the
    block-flow stub both use.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT
                cel.claim_ledger_entry_id AS entry_id,
                cel.evidence_span_id      AS span_id
            FROM claim_evidence_links cel
            JOIN logical_claims        lc ON lc.id = cel.claim_logical_id
            WHERE lc.task_id = :tid
              AND cel.evidence_span_id IS NOT NULL
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return {
        (uuid.UUID(str(r[0])), uuid.UUID(str(r[1])))
        for r in rows
    }


def _count_entailment_checks_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> int:
    return int(
        conn.execute(
            text(
                """
                SELECT COUNT(*) FROM claim_entailment_checks
                WHERE task_id = :tid
                """
            ),
            {"tid": task_id},
        ).scalar_one()
    )


def _fetch_entailment_checks_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT id, claim_ledger_entry_id, evidence_span_id,
                   version_no, verdict, confidence,
                   checker_name, policy_name
            FROM claim_entailment_checks
            WHERE task_id = :tid
            ORDER BY created_at ASC, id ASC
            """
        ),
        {"tid": task_id},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        m = dict(r._mapping)
        m["claim_ledger_entry_id"] = uuid.UUID(str(m["claim_ledger_entry_id"]))
        m["evidence_span_id"] = uuid.UUID(str(m["evidence_span_id"]))
        out.append(m)
    return out


def _gaps_by_kind(
    gaps: list[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    return [g for g in gaps if str(g["kind"]) == kind]


# ---------------------------------------------------------------------------
# FakeRedis
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub.

    Only ``xadd`` is implemented — that is the entire Redis surface
    used by ``app.routes.tasks.create_task`` (post-commit publish on
    ``app.events.task_created``).
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


def _install_fake_redis_on_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> FakeRedis:
    """Patch ``tasks_route.get_redis`` with a fresh FakeRedis.

    The route module captured ``get_redis`` at import time via
    ``from ..redis import get_redis``, so the patched binding must live
    on ``app.routes.tasks``, NOT on ``app.redis``.
    """
    fake = FakeRedis()
    monkeypatch.setattr(tasks_route, "get_redis", lambda: fake)
    return fake


def _reconstruct_event_from_xadd(call: dict[str, Any]) -> dict[str, str]:
    """Reconstruct the event dict as the worker would see it after
    XREADGROUP-decoding a stream entry: a plain dict[str, str].
    """
    assert call["stream"] == TASK_CREATED_STREAM, call
    fields = call["fields"]
    # Every Redis stream field is string/string.
    for k, v in fields.items():
        assert isinstance(k, str)
        assert isinstance(v, str), f"field {k!r} not a str: {v!r}"
    return dict(fields)


# ===========================================================================
# TEST 1 — Warning path end-to-end (real mock entailment checker)
# ===========================================================================
def test_phase_8_8a_entailment_warning_flow_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A) Warning path.

    Pipeline runs naturally. The real mock claim entailment checker
    cannot emit 'contradicted' (verified by inspection of
    ``apps/worker/app/services/claim_entailment_checker.py``: the
    three deterministic rules only produce 'entailed' on containment,
    'not_supported' on numeric mismatch, or 'uncertain' as default).
    The Gate's entailment axis therefore contributes either 'clean'
    (verdict='entailed') or 'warning' (verdict in
    {'not_supported','uncertain'}) per supporting pair.

    The Source Quality axis writes overall_quality='unknown' +
    contradiction_status='unchecked' for every span, which the Gate
    maps to a source_quality_warning per span.

    End state:
      - task_masters.status == 'published';
      - final_gate_reports row with decision='approved' and
        reason_code='all_spans_verified_with_warnings';
      - at least one ``claim_entailment_checks`` row for the task;
      - the entailment summary in the gate report payload either
        reports status='warnings' or status='clean' (the latter when
        the mock produced 'entailed' for every pair via containment);
      - at least one warning coverage gap (entailment_warning OR
        source_quality_warning) attached to the draft;
      - no entailment_block coverage gap;
      - published_answers v1 status='published';
      - audit chain contains, strictly in order:
        analyzed_partial < source_quality_assessed < entailment_checked
        < compiling < final_gate_completed < published;
      - verify_task_audit_chain ok.
    """
    _skip_if_db_unreachable()

    # ----------------------------- seed -----------------------------------
    engine = get_engine()
    with engine.begin() as conn:
        _seeded_dev(conn)

    # ----------------------------- API drive ------------------------------
    fake = _install_fake_redis_on_tasks(monkeypatch)
    client = TestClient(api_app)

    project_id = _create_project_via_api(client)
    document_id = _upload_document_via_api(client, project_id=project_id)
    task_id = _create_task_via_api(
        client, project_id=project_id, document_ids=[document_id]
    )

    # Exactly one xadd observed on task.created.
    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    event = _reconstruct_event_from_xadd(fake.xadd_calls[0])
    assert event["event_type"] == TASK_CREATED_EVENT_TYPE
    assert event["task_id"] == str(task_id)
    assert event["project_id"] == str(project_id)

    # ----------------------------- dispatcher -----------------------------
    rc = _dispatch.handle_event(
        event, redis_consumer_name="realistic_8_8a_warning"
    )
    assert rc == "processed", rc

    # ----------------------------- DB assertions --------------------------
    with engine.connect() as conn:
        # Task reached terminal published.
        assert _fetch_task_status(conn, task_id=task_id) == "published"

        # Audit chain integrity + ordering.
        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        positions = {etype: seq for seq, etype in chain}
        for required in (
            "task.analyzed_partial",
            "task.source_quality_assessed",
            "task.entailment_checked",
            "task.compiling",
            "task.final_gate_completed",
            "task.published",
        ):
            assert required in positions, (required, chain)
        assert (
            positions["task.analyzed_partial"]
            < positions["task.source_quality_assessed"]
            < positions["task.entailment_checked"]
            < positions["task.compiling"]
            < positions["task.final_gate_completed"]
            < positions["task.published"]
        ), chain
        # Exactly one entailment_checked audit on the fresh-run path.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.entailment_checked",
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        # The pipeline must have produced at least one
        # (claim_ledger_entry_id, evidence_span_id) pair AND at least
        # one row in claim_entailment_checks for the task.
        pairs = _distinct_pairs_for_task(conn, task_id=task_id)
        assert len(pairs) >= 1, (
            "the seeded document must yield at least one "
            "claim_evidence_link with a non-null evidence_span_id"
        )
        cec_count = _count_entailment_checks_for_task(conn, task_id=task_id)
        assert cec_count >= 1, (
            "claim_entailment_checks must contain at least one row "
            "after a fresh-run pipeline on a task that produced "
            "claim_evidence_links"
        )

        # The real mock checker MUST NOT have produced 'contradicted'
        # in this flow (it's structurally incapable). If a future
        # change introduces that verdict, this test would silently
        # mis-categorize the flow as Branch W instead of Branch E,
        # so we lock the invariant down.
        cec_rows = _fetch_entailment_checks_for_task(conn, task_id=task_id)
        for r in cec_rows:
            assert str(r["verdict"]) != "contradicted", r

        # Final gate report: approved with warnings (Branch W).
        report = _fetch_final_gate_report(conn, task_id=task_id)
        assert report is not None
        assert report["decision"] == "approved"
        assert report["reason_code"] == "all_spans_verified_with_warnings"
        draft_id = report["draft_final_answer_id"]

        # The gate's payload carries an 'entailment' summary section
        # (see final_answer_gate._aggregate_entailment_reason_counts).
        # Its status reflects the worst-on-block / any-on-warn
        # aggregation across spans: with the real mock checker it can
        # be 'warnings' (typical, since the default rule emits
        # 'uncertain' on the non-containing sentences) or 'clean'
        # (when every supporting pair fell into the containment rule
        # and emitted 'entailed'). It must NEVER be 'blocked' on this
        # path.
        payload = report["payload"] or {}
        assert isinstance(payload, dict)
        entailment_section = payload.get("entailment") or {}
        assert isinstance(entailment_section, dict)
        assert entailment_section.get("policy_name") == (
            "mvp0_entailment_gate_policy"
        )
        assert entailment_section.get("policy_version") == "0.1.0"
        assert int(entailment_section.get("spans_with_block", 0)) == 0
        ent_status = str(entailment_section.get("status") or "")
        assert ent_status in ("clean", "warnings"), entailment_section

        # Coverage gaps: the realistic mock path must prove that the
        # Entailment axis was executed and summarized by the Gate, but it
        # must not force an entailment_warning. With the current mock, if
        # every extracted claim is contained in its supporting quote, all
        # verdicts are 'entailed' and the entailment status is clean.
        #
        # The approved-with-warnings reason in this flow is still expected
        # because Source Quality mock emits unknown + unchecked.
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)
        assert _gaps_by_kind(gaps, "entailment_block") == [], gaps
        assert _gaps_by_kind(gaps, "source_quality_block") == [], gaps
        assert _gaps_by_kind(gaps, "unverified_claim") == [], gaps

        ent_warnings = _gaps_by_kind(gaps, "entailment_warning")
        sq_warnings = _gaps_by_kind(gaps, "source_quality_warning")

        assert len(sq_warnings) >= 1, gaps

        if ent_status == "warnings":
            assert len(ent_warnings) >= 1, gaps
        else:
            assert ent_status == "clean"
            assert ent_warnings == [], gaps
            assert all(str(r["verdict"]) == "entailed" for r in cec_rows), cec_rows

        for wg in ent_warnings + sq_warnings:
            assert str(wg["severity"]) == "warn"
            assert str(wg["gap_key"]).startswith("span:")

        # published_answers v1 status='published'.
        published = _fetch_published(conn, task_id=task_id)
        assert published is not None
        assert published["status"] == "published"
        assert published["version_no"] == 1

    # ----------------------------- HTTP read endpoints --------------------
    # GET /tasks/{id}/final-gate-report
    resp = client.get(f"/api/v1/tasks/{task_id}/final-gate-report")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["decision"] == "approved"
    assert rb["reason_code"] == "all_spans_verified_with_warnings"

    # GET /tasks/{id}/published-answer
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["status"] == "published"
    assert int(rb["version_no"]) == 1


# ===========================================================================
# TEST 2 — Block path end-to-end (orchestrator stubbed to emit contradicted)
# ===========================================================================
def test_phase_8_8a_entailment_block_flow_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B) Block path.

    The real mock checker only ever writes verdicts in
    {'entailed','not_supported','uncertain'}, so Branch E
    ('entailment_block') is unreachable in production with the
    current mock. To activate it end-to-end via the real pipeline,
    we monkeypatch the symbol
    ``_wapp.consumers.task_created.run_claim_entailment_checks``
    with a stub that, for every (claim_ledger_entry_id,
    evidence_span_id) pair linked to the task, INSERTs a v1
    claim_entailment_checks row with verdict='contradicted'. The
    stub returns the canonical counts dict the consumer audit-payload
    code expects. The monkeypatch lives on the CONSUMER module
    because task_created.py imports the function at module load time
    (mirror of the 8.7H block-flow strategy).

    End state:
      - task_masters.status == 'analyzed_partial';
      - final_gate_reports with decision='rejected' and
        reason_code='entailment_block';
      - at least one coverage_gap_statements kind='entailment_block'
        severity='block';
      - at least one claim_entailment_checks row with
        verdict='contradicted' for the task;
      - no published_answers row for the task;
      - audit chain contains task.entailment_checked,
        task.final_gate_completed, and task.publication_held as the
        terminal event; task.published is NEVER present;
      - verify_task_audit_chain ok.
    """
    _skip_if_db_unreachable()

    # The consumer module under the alias namespace. Resolve it via
    # sys.modules so the test does not need a top-level Python import
    # (which would re-trigger the worker bootstrap into the API
    # namespace).
    task_created_module = sys.modules[
        f"{_WORKER_ALIAS}.consumers.task_created"
    ]

    # The stub. Signature must match run_claim_entailment_checks as
    # called from task_created.py: ``(conn, *, task_id)``. Returns the
    # same counts shape as the real orchestrator's _empty_counts +
    # status='completed' (per
    # apps/worker/app/services/claim_entailment_orchestrator.py).
    def _stub_run_claim_entailment_checks(
        conn: Connection, *, task_id: uuid.UUID
    ) -> dict[str, Any]:
        # Resolve scope from task_masters. If the task does not exist,
        # mirror the real orchestrator's 'not_found' behavior. We
        # explicitly read project_id even though the FK on
        # claim_entailment_checks.project_id is NULLABLE: the real
        # orchestrator path passes a non-NULL project_id whenever
        # available, and matching that improves audit fidelity.
        scope_row = conn.execute(
            text(
                "SELECT tenant_id, project_id FROM task_masters WHERE id = :t"
            ),
            {"t": task_id},
        ).first()
        if scope_row is None:
            return {
                "status": "not_found",
                "pairs_total": 0,
                "assessed_count": 0,
                "already_assessed_count": 0,
                "not_found_count": 0,
                "invalid_target_count": 0,
                "error_count": 0,
            }
        tenant_id = uuid.UUID(str(scope_row[0]))
        project_id = uuid.UUID(str(scope_row[1]))

        # DISTINCT (entry, span) pairs linked to the task via
        # claim_evidence_links JOIN logical_claims — same query the
        # real orchestrator uses.
        pair_rows = conn.execute(
            text(
                """
                SELECT DISTINCT
                    cel.claim_ledger_entry_id AS entry_id,
                    cel.evidence_span_id      AS span_id,
                    cel.claim_logical_id      AS logical_id
                FROM claim_evidence_links cel
                JOIN logical_claims        lc ON lc.id = cel.claim_logical_id
                WHERE lc.task_id = :tid
                  AND cel.evidence_span_id IS NOT NULL
                ORDER BY cel.claim_ledger_entry_id ASC,
                         cel.evidence_span_id ASC
                """
            ),
            {"tid": task_id},
        ).fetchall()
        pairs = [
            (
                uuid.UUID(str(r._mapping["entry_id"])),
                uuid.UUID(str(r._mapping["span_id"])),
                uuid.UUID(str(r._mapping["logical_id"])),
            )
            for r in pair_rows
        ]

        # Insert one v1 claim_entailment_checks row per pair with
        # verdict='contradicted'. All codomains are 0009-valid. The
        # idempotency_key is deterministic per (task, entry, span) and
        # explicitly differs from the real orchestrator's ':v1' suffix
        # to surface a stub-vs-real mix-up loudly if it ever happens.
        payload_template = {
            "trigger": "phase_8_8a_block_flow_stub",
            "task_id": str(task_id),
            "note": (
                "stub seed; not produced by mvp0_mock_entailment_checker"
            ),
        }
        for entry_id, span_id, logical_id in pairs:
            idempotency_key = (
                f"task:{task_id}"
                f":entry:{entry_id}"
                f":span:{span_id}"
                f":test-block-v1"
            )
            payload = dict(payload_template)
            payload["claim_ledger_entry_id"] = str(entry_id)
            payload["evidence_span_id"] = str(span_id)
            conn.execute(
                text(
                    """
                    INSERT INTO claim_entailment_checks (
                      tenant_id, project_id, task_id,
                      claim_logical_id, claim_ledger_entry_id,
                      evidence_span_id,
                      version_no, verdict, confidence,
                      checker_name, checker_version,
                      policy_name, policy_version,
                      idempotency_key, rationale, payload
                    ) VALUES (
                      :tenant_id, :project_id, :task_id,
                      :logical_id, :entry_id,
                      :span_id,
                      1, 'contradicted', 0.9,
                      'test_entailment_checker', '0.1.0',
                      'test_entailment_block_policy', '0.1.0',
                      :idempotency_key,
                      'stub: contradicted seeded for 8.8A-GATE-FLOW block test',
                      CAST(:payload AS JSONB)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "logical_id": logical_id,
                    "entry_id": entry_id,
                    "span_id": span_id,
                    "idempotency_key": idempotency_key,
                    "payload": json.dumps(payload, sort_keys=True),
                },
            )

        n = len(pairs)
        return {
            "status": "completed",
            "pairs_total": n,
            "assessed_count": n,
            "already_assessed_count": 0,
            "not_found_count": 0,
            "invalid_target_count": 0,
            "error_count": 0,
        }

    # Patch the symbol on the consumer module (NOT on the orchestrator
    # module): the consumer imports the function at module load time
    # and binds the name locally. Mirror of the 8.7H block-flow
    # strategy with run_source_quality_assessment.
    monkeypatch.setattr(
        task_created_module,
        "run_claim_entailment_checks",
        _stub_run_claim_entailment_checks,
    )

    # ----------------------------- seed -----------------------------------
    engine = get_engine()
    with engine.begin() as conn:
        _seeded_dev(conn)

    # ----------------------------- API drive ------------------------------
    fake = _install_fake_redis_on_tasks(monkeypatch)
    client = TestClient(api_app)

    project_id = _create_project_via_api(client)
    document_id = _upload_document_via_api(client, project_id=project_id)
    task_id = _create_task_via_api(
        client, project_id=project_id, document_ids=[document_id]
    )

    assert len(fake.xadd_calls) == 1, fake.xadd_calls
    event = _reconstruct_event_from_xadd(fake.xadd_calls[0])
    assert event["task_id"] == str(task_id)

    # ----------------------------- dispatcher -----------------------------
    rc = _dispatch.handle_event(
        event, redis_consumer_name="realistic_8_8a_block"
    )
    assert rc == "processed", rc

    # ----------------------------- DB assertions --------------------------
    with engine.connect() as conn:
        # Task terminal status: 'analyzed_partial' (rejected scenario).
        assert _fetch_task_status(conn, task_id=task_id) == "analyzed_partial"

        # Audit chain integrity.
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        # Audit chain contains the required worker events.
        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        types_present = {etype for _, etype in chain}
        assert "task.entailment_checked" in types_present, chain
        assert "task.final_gate_completed" in types_present, chain
        assert "task.publication_held" in types_present, chain
        # task.published must NOT appear at all.
        assert _count_audit_event(
            conn, task_id=task_id, event_type="task.published"
        ) == 0, chain
        # Terminal event is task.publication_held.
        assert chain, chain
        terminal_event_type = chain[-1][1]
        assert terminal_event_type == "task.publication_held", chain
        # entailment_checked audit was emitted exactly once.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.entailment_checked",
        ) == 1

        # At least one claim_entailment_checks row exists for the
        # task with verdict='contradicted' (the stub's signature
        # checker_name is also asserted to confirm the stub fired
        # instead of the real mock checker).
        cec_rows = _fetch_entailment_checks_for_task(conn, task_id=task_id)
        assert len(cec_rows) >= 1, (
            "stub must have inserted at least one "
            "claim_entailment_checks row"
        )
        contradicted_rows = [
            r for r in cec_rows if str(r["verdict"]) == "contradicted"
        ]
        assert len(contradicted_rows) >= 1, cec_rows
        for r in contradicted_rows:
            assert str(r["checker_name"]) == "test_entailment_checker", r

        # Final gate report.
        report = _fetch_final_gate_report(conn, task_id=task_id)
        assert report is not None
        assert report["decision"] == "rejected"
        assert report["reason_code"] == "entailment_block"
        draft_id = report["draft_final_answer_id"]

        # Coverage gaps: at least one entailment_block severity='block'
        # whose details.reasons references the 'entailment_contradicted'
        # reason_code. NO unverified_claim (CVE-lite passed).
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)
        block_gaps = _gaps_by_kind(gaps, "entailment_block")
        assert len(block_gaps) >= 1, gaps
        assert _gaps_by_kind(gaps, "unverified_claim") == [], gaps

        for bg in block_gaps:
            assert str(bg["severity"]) == "block"
            assert str(bg["gap_key"]).endswith(":entailment_block")
            details = bg.get("details") or {}
            reasons = details.get("reasons") or []
            reason_codes = [str(r.get("reason_code")) for r in reasons]
            assert "entailment_contradicted" in reason_codes, bg

        # No published_answers v1 inserted.
        assert _count_published_for_task(conn, task_id=task_id) == 0

        # source_quality_warning may coexist (the real mock SQ
        # evaluator runs in this flow), but the reason_code must NOT
        # have been overridden — we already asserted reason_code
        # above. We additionally assert the entailment summary in the
        # gate report payload reflects 'blocked'.
        payload = report["payload"] or {}
        assert isinstance(payload, dict)
        entailment_section = payload.get("entailment") or {}
        assert isinstance(entailment_section, dict)
        assert entailment_section.get("policy_name") == (
            "mvp0_entailment_gate_policy"
        )
        assert entailment_section.get("policy_version") == "0.1.0"
        assert str(entailment_section.get("status")) == "blocked"
        assert int(entailment_section.get("spans_with_block", 0)) >= 1

    # ----------------------------- HTTP read endpoints --------------------
    # GET /tasks/{id}/final-gate-report -> 200, rejected, block reason.
    resp = client.get(f"/api/v1/tasks/{task_id}/final-gate-report")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["decision"] == "rejected"
    assert rb["reason_code"] == "entailment_block"
    cgs = rb.get("coverage_gap_statements") or []
    assert any(
        str(g.get("kind")) == "entailment_block" for g in cgs
    ), cgs

    # GET /tasks/{id}/published-answer -> 404 RESOURCE_NOT_FOUND with
    # details.resource='published_answers' (per the convention locked
    # down in 8.4 and re-validated by 8.7H).
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 404, resp.text
    err_body = resp.json()
    err = err_body.get("error")
    assert err is not None, err_body
    assert str(err.get("code")) == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert str(details.get("resource")) == "published_answers"
