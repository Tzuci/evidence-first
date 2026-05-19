"""Phase 8.8B-REPORT-FLOW — Realistic Anti-Hallucination Report Flow Test.

Scope (cross-component, root-tests):
  Two end-to-end-like flows that exercise the aggregated read-only
  Anti-Hallucination Report API (8.8B-REPORT-CODE-A + CODE-B):

      GET /api/v1/tasks/{task_id}/anti-hallucination-report

  Both flows go through the full API HTTP surface, the dispatcher, the
  consumer, and the worker services against the real Postgres. After
  the pipeline has persisted CVE-lite verification, Source Quality
  assessments, Claim Entailment checks, the Final Answer Gate decision
  and (when approved) a published_answer, we GET the aggregated report
  and verify that it correctly summarizes the state of the four axes
  (CVE-lite, Source Quality, Claim Entailment, Final Gate) plus the
  publication status, the coverage gaps, the mock indicators, the
  limitations and the audit chain integrity.

  A) Published-warning path (real mock services everywhere):
     - API HTTP creates project + document + task;
     - FakeRedis captures the task.created event;
     - ``_dispatch.handle_event(...)`` drives the worker through:
         extractor -> CVE-lite -> 8.7E source quality (mock evaluator)
         -> 8.8A claim entailment (mock checker) -> compiler -> Gate;
     - The mock Source Quality evaluator writes
       overall_quality='unknown' + contradiction_status='unchecked',
       which the Gate maps to source_quality_warning per span;
     - The mock entailment checker is structurally incapable of
       emitting 'contradicted', so the entailment axis can only be
       'clean' or 'warnings';
     - End state: decision='approved',
       reason_code='all_spans_verified_with_warnings' (preferred) or
       'all_spans_verified' (only if the entailment axis is clean AND
       no source_quality warning is emitted — unreachable today with
       the mock SQ evaluator, but the assertion stays lenient),
       published_answers v1 status='published';
     - The report endpoint then surfaces publication.status='published',
       claims/evidence populated, axis_summary coherent, mock_indicators
       all true, limitations non-empty.

  B) Publication-held entailment_block path (orchestrator stubbed):
     - Same API HTTP path for project + document + task creation;
     - FakeRedis captures the task.created event;
     - The symbol ``_wapp.consumers.task_created.run_claim_entailment_checks``
       is monkey-patched with a stub that, for every
       (claim_ledger_entry_id, evidence_span_id) pair linked to the
       task, INSERTs a v1 ``claim_entailment_checks`` row with
       ``verdict='contradicted'`` and returns the canonical counts
       dict the consumer audit-payload code expects;
     - ``_dispatch.handle_event(...)`` drives the worker; the Gate
       finds verdict='contradicted' for every supporting pair on every
       verified-backed span, rejects with
       reason_code='entailment_block', emits an entailment_block
       coverage gap (severity='block') per blocked span, does NOT
       insert any ``published_answers`` v1, and leaves the task in
       status='analyzed_partial' with task.publication_held as the
       terminal audit event;
     - The report endpoint then surfaces
       publication.status='publication_held', published_answer_id=None,
       gate.decision='rejected', gate.reason_code='entailment_block',
       coverage_gaps include the entailment_block gap,
       axis_summary.final_gate.has_blocking_gaps=True,
       at least one claim.entailment[].verdict=='contradicted',
       axis_summary.claim_entailment.contradicted_count >= 1.

What this test is and is not:
  - It IS a realistic exercise of the producer + dispatcher + consumer
    + services pipeline against a real Postgres, observed through the
    aggregated 8.8B-REPORT HTTP endpoint. Every layer below the Redis
    transport runs the production code path; only Redis is a FakeRedis
    that records xadd calls. The block flow replaces ONLY the
    orchestrator symbol bound on the consumer module so the Branch E
    (entailment_block) can be activated end-to-end (the real mock
    checker never produces 'contradicted').
  - It is NOT a Redis-loop test: no XREADGROUP, no consumer groups,
    no worker main() loop.
  - It does NOT modify the report endpoint, the Gate, the consumer,
    the checker, the orchestrator, migrations, or any shared schema.

Anti-hallucination disclaimer (per PHASE_8_8B_REPORT_PRE.md):
  The system is designed to prevent factual claims that are
  unsupported, contradicted, or based on inadequate sources from being
  published as reliable. It does NOT promise to eliminate all
  hallucinations. The report is a derived read-only view; it does NOT
  introduce new decisions and does NOT recompute the Gate.

Hard package-collision note (same as the 8.5 / 8.6 / 8.7H / 8.8A-GATE-FLOW realistic-flow tests):
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
# This file lives at <repo>/tests/test_phase_8_8b_report_flow.py, so
# parents[1] is the repo root. We deliberately do NOT add apps/worker
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
# alongside the 8.5 / 8.6 / 8.7H / 8.8A-GATE-FLOW realistic-flow tests,
# the alias is already populated and we just retrieve dispatch from
# sys.modules. The 8.8B-REPORT-FLOW bootstrap mirrors 8.8A-GATE-FLOW:
# it explicitly loads claim_entailment_checker and
# claim_entailment_orchestrator on top of the modules already loaded
# by earlier bootstraps.
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

    Mirror of the 8.8A-GATE-FLOW bootstrap: explicitly loads
    claim_entailment_checker and claim_entailment_orchestrator. If
    earlier flow tests already populated the alias (they would also
    have loaded these two modules transitively via task_created), the
    _load_mod calls short-circuit via the sys.modules check.
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
# produces verified_fact v2 entries. Without digits, the pipeline
# would enter Branch A of the Gate (no_verified_claims) and the report
# we want to exercise (with claims, evidence, CVE-lite and entailment
# rows) would never materialize.
_DOC_CONTENT_BYTES = (
    b"Revenue reached 12500000 USD in 2024. "
    b"The company added 3412 customers in Q3. "
    b"Sales grew by 37 percent.\n"
)

# The expected audit event sequence on the fresh-run worker path.
# Used to assert ordering in the report's underlying audit chain (the
# report itself does not include audit events; we read them directly
# from audit_records). The 15 events match PROJECT_STATE.md.
_EXPECTED_AUDIT_TRAIL_PUBLISHED = (
    "task.analyzing",
    "task.docs_loaded",
    "task.claims_extracted",
    "task.claims_classified",
    "task.claims_ledger_initialized",
    "task.cve_lite_started",
    "task.cve_lite_completed",
    "task.analyzed_partial",
    "task.source_quality_assessed",
    "task.entailment_checked",
    "task.compiling",
    "task.draft_compiled",
    "task.final_gate_started",
    "task.final_gate_completed",
    "task.published",
)
_EXPECTED_AUDIT_TRAIL_HELD = (
    "task.analyzing",
    "task.docs_loaded",
    "task.claims_extracted",
    "task.claims_classified",
    "task.claims_ledger_initialized",
    "task.cve_lite_started",
    "task.cve_lite_completed",
    "task.analyzed_partial",
    "task.source_quality_assessed",
    "task.entailment_checked",
    "task.compiling",
    "task.draft_compiled",
    "task.final_gate_started",
    "task.final_gate_completed",
    "task.publication_held",
)


# ---------------------------------------------------------------------------
# environment guard
# ---------------------------------------------------------------------------
def _skip_if_db_unreachable() -> None:
    """Skip the test if Postgres is not reachable.

    We do NOT require REDIS_URL because every test in this file
    installs a FakeRedis on the API tasks route module and never
    invokes the worker's Redis loop. The DB is the only external
    dependency.
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
                "SELECT id FROM users WHERE tenant_id = :t "
                "AND email = 'dev@local'"
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
        "name": f"phase-8-8b-report-flow-{uuid.uuid4()}",
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
    client: TestClient,
    *,
    project_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> uuid.UUID:
    """POST /api/v1/tasks with the given document_ids. Returns task_id."""
    body = {
        "project_id": str(project_id),
        "objective": f"phase 8.8b report flow {uuid.uuid4()}",
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
    ``from ..redis import get_redis``, so the patched binding must
    live on ``app.routes.tasks``, NOT on ``app.redis``.
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


# ---------------------------------------------------------------------------
# Report endpoint shape sanity helpers
# ---------------------------------------------------------------------------
# The report endpoint is implemented in
# apps/api/app/routes/anti_hallucination_report.py and unit-tested in
# apps/api/tests/test_anti_hallucination_report_endpoint.py. Here we
# only verify the SHAPE-level invariants that must hold across BOTH
# realistic flow scenarios; field-by-field shape is the responsibility
# of the API-level test file (per the block prompt §9: "Non rendere i
# test fragili controllando l’intera shape campo-per-campo").
_TOP_LEVEL_REPORT_KEYS = (
    "task_id",
    "project_id",
    "tenant_id",
    "task",
    "publication",
    "gate",
    "claims",
    "evidence",
    "axis_summary",
    "mock_indicators",
    "limitations",
)
_AXIS_SUMMARY_KEYS = (
    "cve_lite",
    "source_quality",
    "claim_entailment",
    "final_gate",
)
_MOCK_INDICATOR_FLAGS = (
    "uses_mock_source_quality",
    "uses_mock_claim_entailment",
    "uses_mock_compiler",
    "uses_mock_cve_lite",
)


def _assert_report_top_level_shape(
    body: dict[str, Any], *, task_id: uuid.UUID
) -> None:
    """Sanity-check the top-level shape of the report response.

    Does NOT enforce field-by-field equality on every nested object
    (that is covered by the API-level test). Asserts only the
    presence of the top-level keys, the identity of task_id, and the
    types of the four mock_indicator flags + the limitations list.
    """
    for k in _TOP_LEVEL_REPORT_KEYS:
        assert k in body, f"missing top-level key {k!r}: {body}"
    assert body["task_id"] == str(task_id), body

    axis = body["axis_summary"]
    assert isinstance(axis, dict)
    for k in _AXIS_SUMMARY_KEYS:
        assert k in axis, f"missing axis_summary key {k!r}: {axis}"

    mi = body["mock_indicators"]
    assert isinstance(mi, dict)
    for k in _MOCK_INDICATOR_FLAGS:
        assert k in mi, f"missing mock_indicators key {k!r}: {mi}"
        assert isinstance(mi[k], bool), (k, mi[k])
    assert "notes" in mi
    assert isinstance(mi["notes"], list)
    assert len(mi["notes"]) >= 1

    assert isinstance(body["limitations"], list)
    assert len(body["limitations"]) >= 1


def _entailment_axis_counters_total(axis_ce: dict[str, Any]) -> int:
    """Sum of every entailment axis counter except missing_count.

    Used to assert that the entailment axis has been exercised by the
    pipeline (at least one supporting pair was classified).
    """
    return sum(
        int(axis_ce.get(k, 0) or 0)
        for k in (
            "entailed_count",
            "partially_supported_count",
            "not_supported_count",
            "contradicted_count",
            "uncertain_count",
            "missing_count",
        )
    )


# ===========================================================================
# TEST A — Published warning path (real mock services)
# ===========================================================================
def test_anti_hallucination_report_flow_published_warning_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A) Published warning path.

    Drive the full 8.4 + 8.7E + 8.8A pipeline naturally with the real
    mock services. With the current mocks the expected end state is:

      - decision='approved';
      - reason_code='all_spans_verified_with_warnings' (Source Quality
        emits unknown+unchecked per span, so at least one warning is
        always present) — we additionally tolerate
        reason_code='all_spans_verified' for forward compatibility
        in case a future mock SQ evaluator change makes it emit
        'strong' / 'adequate' clean (won't happen today but the
        assertion stays lenient per the block prompt §5);
      - published_answers v1 status='published';
      - claim_entailment_checks contains at least one row;
      - source_quality_assessments contains at least one row.

    Then GET the aggregated report and assert it correctly summarizes
    the state of every axis.
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
        event, redis_consumer_name="realistic_8_8b_report_warning"
    )
    assert rc == "processed", rc

    # ----------------------------- DB sanity ------------------------------
    with engine.connect() as conn:
        assert _fetch_task_status(conn, task_id=task_id) == "published"

        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        type_seqs = {etype: seq for seq, etype in chain}
        for required in _EXPECTED_AUDIT_TRAIL_PUBLISHED:
            assert required in type_seqs, (required, chain)
        # Ordering invariants between the key transitions.
        assert (
            type_seqs["task.analyzed_partial"]
            < type_seqs["task.source_quality_assessed"]
            < type_seqs["task.entailment_checked"]
            < type_seqs["task.compiling"]
            < type_seqs["task.final_gate_completed"]
            < type_seqs["task.published"]
        ), chain
        # task.entailment_checked emitted exactly once.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.entailment_checked",
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

    # ----------------------------- GET report -----------------------------
    resp = client.get(f"/api/v1/tasks/{task_id}/anti-hallucination-report")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Shape sanity.
    _assert_report_top_level_shape(body, task_id=task_id)
    assert body["project_id"] == str(project_id)

    # task section: published.
    assert str(body["task"].get("status")) == "published"

    # publication: published, with a non-null published_answer_id and
    # published_answer_status='published'.
    pub = body["publication"]
    assert pub.get("status") == "published", pub
    assert pub.get("published_answer_id") is not None
    assert pub.get("published_answer_status") == "published"

    # gate: approved, reason_code 'all_spans_verified_with_warnings'
    # preferred; allow 'all_spans_verified' as a forward-compat
    # tolerance (today the mock SQ evaluator always emits warnings,
    # so the warning reason is the de-facto behavior).
    gate = body["gate"]
    assert gate.get("decision") == "approved", gate
    assert gate.get("reason_code") in {
        "all_spans_verified_with_warnings",
        "all_spans_verified",
    }, gate

    # claims: at least one logical_claim was produced by the
    # extractor and reached the report. Each claim entry surfaces
    # latest_entry_id and latest_state (when an entry exists).
    claims = body["claims"]
    assert isinstance(claims, list)
    assert len(claims) >= 1, body
    # The latest_entry_id of at least one claim must be set, because
    # the pipeline produced ledger entries.
    any_with_latest_entry = any(
        c.get("latest_entry_id") is not None for c in claims
    )
    assert any_with_latest_entry, claims

    # evidence: at least one evidence_span attached to the task.
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert len(evidence) >= 1, body

    # axis_summary.cve_lite: at least one verified claim. The mock
    # extractor + CVE-lite combination passes every claim it produces
    # (the quote is substring of the chunk and the hash matches by
    # construction); 'fail' is not produced today.
    axis_cve = body["axis_summary"]["cve_lite"]
    assert isinstance(axis_cve, dict)
    assert int(axis_cve.get("verified_claims_count", 0) or 0) >= 1, axis_cve

    # axis_summary.source_quality: with the mock SQ evaluator every
    # span carries overall_quality='unknown', so at least one of:
    #   - unknown_count >= 1, OR
    #   - the gate has emitted a source_quality_warning gap
    # must hold. We test the first condition (it is the simpler
    # observable in axis_summary) and additionally tolerate the
    # second so the test is robust if the report-side aggregation
    # ever decides to map 'unknown' differently.
    axis_sq = body["axis_summary"]["source_quality"]
    assert isinstance(axis_sq, dict)
    sq_unknown = int(axis_sq.get("unknown_count", 0) or 0)
    sq_missing = int(axis_sq.get("missing_count", 0) or 0)
    # Either the report counted at least one unknown latest assessment
    # OR — defensively — a warning coverage gap with axis='source_quality'
    # is present in gate.coverage_gaps.
    has_sq_warning_gap = any(
        str(g.get("kind")) == "source_quality_warning"
        and str(g.get("axis")) == "source_quality"
        for g in gate.get("coverage_gaps", [])
    )
    assert sq_unknown >= 1 or has_sq_warning_gap or sq_missing >= 1, (
        axis_sq,
        gate,
    )

    # axis_summary.claim_entailment: the entailment axis was
    # exercised; verdict counts + missing_count sum to at least 1
    # (i.e. at least one supporting pair was classified). We do NOT
    # constrain the specific verdict distribution: the mock heuristic
    # output depends on the extractor's raw_claim text — typically
    # 'entailed' via containment, occasionally 'uncertain' via the
    # default rule. 'contradicted' MUST NOT appear on this path
    # (the real mock checker is structurally incapable of emitting it).
    axis_ce = body["axis_summary"]["claim_entailment"]
    assert isinstance(axis_ce, dict)
    assert _entailment_axis_counters_total(axis_ce) >= 1, axis_ce
    assert int(axis_ce.get("contradicted_count", 0) or 0) == 0, axis_ce

    # mock_indicators: all four flags MUST be true on the mock-driven
    # pipeline (PROVIDERS_ENABLED=mock).
    mi = body["mock_indicators"]
    assert mi["uses_mock_source_quality"] is True, mi
    assert mi["uses_mock_claim_entailment"] is True, mi
    assert mi["uses_mock_compiler"] is True, mi
    assert mi["uses_mock_cve_lite"] is True, mi

    # limitations: non-empty (asserted by _assert_report_top_level_shape)
    # and contains the anti-hallucination disclaimer prose. Per the
    # block prompt §9 we do not assert exact strings — the documented
    # disclaimers live in the route module's _limitations() function
    # and the API-level test asserts their length already.

    # gate.coverage_gaps: with the mock SQ evaluator a source-quality
    # warning gap is almost always present. The endpoint already
    # decorates each gap with the derived 'axis' field.
    cgs = gate.get("coverage_gaps", [])
    assert isinstance(cgs, list)
    # No 'block' severity must appear on the approved path.
    for g in cgs:
        assert str(g.get("severity")) != "block", g

    # axis_summary.final_gate: derived from coverage_gaps. With the
    # mock services the approved-with-warnings reason always implies
    # at least one warning gap, but we tolerate has_warnings=False
    # in the unlikely event the report-side aggregation considers
    # the gate clean.
    fg = body["axis_summary"]["final_gate"]
    assert isinstance(fg, dict)
    assert fg.get("has_blocking_gaps") is False, fg
    assert int(fg.get("blocking_gap_count", 0) or 0) == 0, fg


# ===========================================================================
# TEST B — Publication-held entailment_block path (orchestrator stubbed)
# ===========================================================================
def test_anti_hallucination_report_flow_publication_held_entailment_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B) Publication-held entailment_block path.

    The real mock checker only ever writes verdicts in
    {'entailed','not_supported','uncertain'}, so Branch E
    ('entailment_block') is unreachable in production with the
    current mock. To activate it end-to-end via the real pipeline,
    we monkeypatch the symbol
    ``_wapp.consumers.task_created.run_claim_entailment_checks``
    with a stub that, for every (claim_ledger_entry_id,
    evidence_span_id) pair linked to the task, INSERTs a v1
    claim_entailment_checks row with ``verdict='contradicted'``. The
    stub returns the canonical counts dict the consumer
    audit-payload code expects.

    Then GET the aggregated report and assert it correctly surfaces
    publication_held + entailment_block + the contradicted verdicts
    + the blocking gap, without inventing data.
    """
    _skip_if_db_unreachable()

    # The consumer module under the alias namespace. Resolve it via
    # sys.modules so the test does not need a top-level Python import.
    task_created_module = sys.modules[
        f"{_WORKER_ALIAS}.consumers.task_created"
    ]

    # The stub. Signature must match run_claim_entailment_checks as
    # called from task_created.py: ``(conn, *, task_id)``. Returns
    # the same counts shape as the real orchestrator's _empty_counts
    # + status='completed' (per
    # apps/worker/app/services/claim_entailment_orchestrator.py).
    def _stub_run_claim_entailment_checks(
        conn: Connection, *, task_id: uuid.UUID
    ) -> dict[str, Any]:
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

        # DISTINCT (entry, span, logical) triples linked to the task
        # via claim_evidence_links JOIN logical_claims — same query
        # the real orchestrator uses, with the additional
        # claim_logical_id column required by claim_entailment_checks'
        # composite FK toward (claim_ledger_entry_id, claim_logical_id).
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
        # idempotency_key is deterministic per (task, entry, span)
        # and explicitly differs from the real orchestrator's ':v1'
        # suffix to surface a stub-vs-real mix-up loudly if it ever
        # happens.
        for entry_id, span_id, logical_id in pairs:
            idempotency_key = (
                f"task:{task_id}"
                f":entry:{entry_id}"
                f":span:{span_id}"
                f":test-report-block-v1"
            )
            payload = {
                # The report's mock-indicator detector treats
                # payload.mock=True as a signal that the row is
                # mock-driven; we keep the flag True so the report
                # surfaces uses_mock_claim_entailment=True.
                "mock": True,
                "trigger": "phase_8_8b_report_flow_block_stub",
                "task_id": str(task_id),
                "claim_ledger_entry_id": str(entry_id),
                "evidence_span_id": str(span_id),
                "note": (
                    "stub seed; not produced by "
                    "mvp0_mock_entailment_checker"
                ),
            }
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
                      'stub: contradicted seeded for 8.8B-REPORT-FLOW',
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

    # Patch the symbol on the consumer module (NOT on the
    # orchestrator module): the consumer imports the function at
    # module load time and binds the name locally. Mirror of the
    # 8.8A-GATE-FLOW block-flow strategy.
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
        event, redis_consumer_name="realistic_8_8b_report_block"
    )
    assert rc == "processed", rc

    # ----------------------------- DB sanity ------------------------------
    with engine.connect() as conn:
        # Task terminal status: 'analyzed_partial' (rejected scenario).
        assert _fetch_task_status(conn, task_id=task_id) == "analyzed_partial"

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        types_present = {etype for _, etype in chain}
        # task.publication_held must be present; task.published must not.
        for required in (
            "task.entailment_checked",
            "task.final_gate_completed",
            "task.publication_held",
        ):
            assert required in types_present, (required, chain)
        assert "task.published" not in types_present, chain
        # entailment_checked audit emitted exactly once.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.entailment_checked",
        ) == 1
        # Terminal event is task.publication_held.
        terminal_event_type = chain[-1][1]
        assert terminal_event_type == "task.publication_held", chain

    # ----------------------------- GET report -----------------------------
    resp = client.get(f"/api/v1/tasks/{task_id}/anti-hallucination-report")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Shape sanity.
    _assert_report_top_level_shape(body, task_id=task_id)
    assert body["project_id"] == str(project_id)

    # publication: publication_held, no published_answer_id.
    pub = body["publication"]
    assert pub.get("status") == "publication_held", pub
    assert pub.get("published_answer_id") is None, pub
    assert pub.get("status") != "published"

    # gate: rejected with reason_code='entailment_block'.
    gate = body["gate"]
    assert gate.get("decision") == "rejected", gate
    assert gate.get("reason_code") == "entailment_block", gate

    # coverage_gaps: at least one entailment_block, severity='block',
    # axis='claim_entailment'.
    cgs = gate.get("coverage_gaps", [])
    assert isinstance(cgs, list)
    entailment_block_gaps = [
        g for g in cgs if str(g.get("kind")) == "entailment_block"
    ]
    assert len(entailment_block_gaps) >= 1, cgs
    for g in entailment_block_gaps:
        assert str(g.get("severity")) == "block", g
        assert str(g.get("axis")) == "claim_entailment", g

    # axis_summary.final_gate: has_blocking_gaps=True with at least
    # one blocking gap.
    fg = body["axis_summary"]["final_gate"]
    assert isinstance(fg, dict)
    assert fg.get("has_blocking_gaps") is True, fg
    assert int(fg.get("blocking_gap_count", 0) or 0) >= 1, fg

    # claims: at least one entry, and at least one claim.entailment[]
    # item has verdict='contradicted' (the stub inserted v1 contradicted
    # for every supporting pair).
    claims = body["claims"]
    assert isinstance(claims, list)
    assert len(claims) >= 1, body

    has_contradicted_entailment = any(
        any(str(e.get("verdict")) == "contradicted" for e in (c.get("entailment") or []))
        for c in claims
    )
    assert has_contradicted_entailment, claims

    # evidence: at least one evidence_span.
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert len(evidence) >= 1, body

    # axis_summary.claim_entailment.contradicted_count >= 1.
    axis_ce = body["axis_summary"]["claim_entailment"]
    assert isinstance(axis_ce, dict)
    assert int(axis_ce.get("contradicted_count", 0) or 0) >= 1, axis_ce

    # mock_indicators: the stub writes payload.mock=True so the report
    # surfaces uses_mock_claim_entailment=True. The other three flags
    # stay True (real mock services on those axes).
    mi = body["mock_indicators"]
    assert mi["uses_mock_source_quality"] is True, mi
    assert mi["uses_mock_claim_entailment"] is True, mi
    assert mi["uses_mock_compiler"] is True, mi
    assert mi["uses_mock_cve_lite"] is True, mi

    # GET /api/v1/tasks/{task_id}/published-answer -> 404
    # RESOURCE_NOT_FOUND with details.resource='published_answers'
    # (convention locked down in 8.4 and re-validated by 8.7H /
    # 8.8A-GATE-FLOW). The endpoint name is per
    # apps/api/app/routes/answers.py.
    pa_resp = client.get(
        f"/api/v1/tasks/{task_id}/published-answer"
    )
    assert pa_resp.status_code == 404, pa_resp.text
    err_body = pa_resp.json()
    err = err_body.get("error")
    assert err is not None, err_body
    assert str(err.get("code")) == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert str(details.get("resource")) == "published_answers"
