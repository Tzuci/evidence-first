"""Phase 8.7H — Realistic Source Quality Flow Test.

Scope (cross-component, root-tests):
  Two end-to-end-like flows that exercise the 8.7E source quality step
  inside the real task.created pipeline, observed via the 8.7F read
  endpoints and the 8.4 final-gate/published-answer endpoints.

  A) Warning flow (Branch B', `all_spans_verified_with_warnings`):
     - API HTTP creates project + document + task;
     - FakeRedis captures task.created;
     - dispatch.handle_event(...) drives the worker through the full
       pipeline (extractor -> CVE-lite -> 8.7E source quality with the
       mock evaluator -> compiler -> Final Answer Gate consulting Source
       Quality);
     - With the mock evaluator the Gate finds overall_quality='unknown'
       + contradiction_status='unchecked' for every supporting
       evidence_span, emits coverage_gap_statements of
       kind='source_quality_warning' (severity='warn'), approves the
       draft with reason_code='all_spans_verified_with_warnings', and
       inserts published_answers v1 status='published'.

  B) Block flow (Branch C', `source_quality_block`):
     - Same API HTTP path for project + document + task creation;
     - FakeRedis captures task.created;
     - The simbolo `_wapp.consumers.task_created.run_source_quality_assessment`
       is monkey-patched with a stub that, for each evidence_span linked
       to the task, INSERTs a v1 source_quality_assessments row with
       overall_quality='unsuitable' and returns the canonical counts
       dict the consumer expects;
     - dispatch.handle_event(...) drives the worker; the Gate finds
       overall_quality='unsuitable' for every span, rejects with
       reason_code='source_quality_block', emits
       kind='source_quality_block' gaps (severity='block'), does NOT
       insert any published_answers v1, and leaves the task in
       status='analyzed_partial' with task.publication_held as the
       terminal audit event.

What this test is and is not:
  - It IS a realistic exercise of the producer + dispatcher + consumer
    + services pipeline against a real Postgres. Every layer below the
    Redis transport runs the production code path; only Redis is a
    FakeRedis that records xadd calls. The block flow replaces ONLY
    the orchestrator symbol bound on the consumer module so the
    branch can be activated end-to-end (the real mock evaluator never
    produces overall_quality='unsuitable').
  - It is NOT a Redis-loop test: no XREADGROUP, no consumer groups,
    no worker main() loop.
  - It does NOT exercise the Final Answer Gate directly; the Gate is
    invoked by the pipeline as part of task.created processing.

Hard package-collision note (same as the 8.5 / 8.6 realistic-flow tests):
  Both apps/api/app and apps/worker/app are top-level packages literally
  named ``app``. We:
    1) prepend apps/api + packages/shared to sys.path so ``import app``
       resolves to the API,
    2) import API normally (``from app.main import app as api_app``,
       ``from app.routes import tasks as tasks_route``),
    3) load the worker package via importlib.util under a synthetic
       top-level alias ``_wapp``, registering every submodule in
       sys.modules so the worker's relative imports resolve within its
       own namespace.
  The 8.7H bootstrap loads source_quality_evaluator and
  source_quality_orchestrator explicitly into the alias, on top of the
  modules already loaded by the 8.5 / 8.6 bootstraps.

DB requirement:
  Same Postgres used by ``make test-db`` — DATABASE_URL is set, the
  migrations (0001..0008) are applied. We never set DATABASE_URL
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
# This file lives at <repo>/tests/test_phase_8_7_source_quality_flow.py, so
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
from app.routes import tasks as tasks_route  # noqa: E402

# Shared helpers used to validate the audit chain end-to-end.
from evidencefirst_shared.db.audit import verify_task_audit_chain  # noqa: E402


# ---------------------------------------------------------------------------
# Worker bootstrap under alias namespace ``_wapp``
# ---------------------------------------------------------------------------
# We load apps/worker/app/* under the synthetic top-level name ``_wapp`` so
# that worker's relative imports resolve within their own namespace and do
# not collide with API's ``app``. Must happen exactly once per interpreter;
# subsequent calls reuse the cached entries. When run alongside the 8.5 /
# 8.6 realistic-flow tests, the alias is already populated and we just
# retrieve dispatch from sys.modules. The 8.7H bootstrap is a superset of
# the 8.5 / 8.6 ones: it ALSO explicitly loads source_quality_evaluator
# and source_quality_orchestrator (the two services introduced in 8.7E).
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

    The 8.7H bootstrap explicitly loads source_quality_evaluator and
    source_quality_orchestrator. If the 8.5 / 8.6 bootstrap already
    populated the alias without those two modules (in 8.7E they get
    loaded transitively when task_created is imported, but we keep the
    list explicit here for documentation), the _load_mod calls are
    short-circuited via the sys.modules check.
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
    # source_quality_evaluator and source_quality_orchestrator are
    # listed explicitly so the dependency surface is documented in
    # this file even if 8.5 / 8.6 already loaded them via the
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
WORKER_CONSUMER_NAME = "task_created"

# Document contents: must contain at least one sentence with digits so
# the mock-driven extractor emits raw_claim rows and the CVE-lite step
# produces verified_fact v2 entries. Without digits, the pipeline would
# enter Branch A of the Gate (no_verified_claims) and the test would
# not exercise the Source Quality branch at all.
_DOC_CONTENT_BYTES = (
    b"Sales grew by 37 percent in Q3. "
    b"There were 3412 new customers in the same quarter. "
    b"Revenue reached 12500000 USD in 2024.\n"
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
    """POST /api/v1/projects with a unique name; return project_id.

    The route resolves tenant via DEV_TENANT_SLUG ('dev') and returns
    the created project.
    """
    body = {
        "name": f"phase-8-7h-flow-{uuid.uuid4()}",
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
        "objective": f"phase 8.7h realistic flow {uuid.uuid4()}",
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


def _distinct_evidence_span_ids_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT cel.evidence_span_id
            FROM claim_evidence_links cel
            JOIN logical_claims lc ON lc.id = cel.claim_logical_id
            WHERE lc.task_id = :tid
              AND cel.evidence_span_id IS NOT NULL
            """
        ),
        {"tid": task_id},
    ).fetchall()
    return {uuid.UUID(str(r[0])) for r in rows}


def _fetch_sqa_for_task(
    conn: Connection, *, task_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Return every source_quality_assessments row whose evidence_span_id
    is linked to a logical_claim of the given task.
    """
    rows = conn.execute(
        text(
            """
            SELECT sqa.id,
                   sqa.evidence_span_id,
                   sqa.version_no,
                   sqa.overall_quality,
                   sqa.contradiction_status,
                   sqa.evaluator_name,
                   sqa.policy_name,
                   sqa.idempotency_key,
                   sqa.payload
            FROM source_quality_assessments sqa
            WHERE sqa.evidence_span_id IN (
                SELECT DISTINCT cel.evidence_span_id
                FROM claim_evidence_links cel
                JOIN logical_claims lc ON lc.id = cel.claim_logical_id
                WHERE lc.task_id = :tid
                  AND cel.evidence_span_id IS NOT NULL
            )
            ORDER BY sqa.created_at ASC, sqa.id ASC
            """
        ),
        {"tid": task_id},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        m = dict(r._mapping)
        m["evidence_span_id"] = uuid.UUID(str(m["evidence_span_id"]))
        m["payload"] = _normalize_jsonb(m.get("payload"))
        out.append(m)
    return out


def _gaps_by_kind(
    gaps: list[dict[str, Any]], kind: str
) -> list[dict[str, Any]]:
    return [g for g in gaps if str(g["kind"]) == kind]


def _reason_codes_in_gap(gap: dict[str, Any]) -> list[str]:
    """Extract the reason_code values from a source_quality_* gap's details."""
    details = gap.get("details") or {}
    reasons = details.get("reasons") or []
    return [str(r.get("reason_code")) for r in reasons]


# ---------------------------------------------------------------------------
# FakeRedis
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub.

    Only ``xadd`` is implemented — that is the entire Redis surface
    used by ``app.routes.tasks.create_task`` (post-commit publish on
    ``app.events.task_created``). We deliberately do NOT add other
    Redis methods.
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
# TEST 1 — Warning flow (Branch B' end-to-end)
# ===========================================================================
def test_phase_8_7_source_quality_warning_flow_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A) Warning flow.

    Pipeline runs naturally with the mock source_quality_evaluator;
    every evidence_span linked to the task gets a v1 assessment with
    overall_quality='unknown' + contradiction_status='unchecked'. The
    Gate's Source Quality policy classifies these as warnings (P1+P3
    matrix), approves the draft with reason_code
    'all_spans_verified_with_warnings', emits at least one
    coverage_gap_statements row of kind='source_quality_warning'
    (severity='warn'), and inserts published_answers v1
    status='published'.
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
        event, redis_consumer_name="realistic_8_7h_warning"
    )
    assert rc == "processed", rc

    # ----------------------------- DB assertions --------------------------
    with engine.connect() as conn:
        # Task reached terminal published.
        assert _fetch_task_status(conn, task_id=task_id) == "published"

        # Audit chain contains task.source_quality_assessed strictly
        # between task.analyzed_partial and task.compiling, and ends in
        # task.published. verify_task_audit_chain ok.
        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        positions = {etype: seq for seq, etype in chain}
        assert "task.analyzed_partial" in positions, chain
        assert "task.source_quality_assessed" in positions, chain
        assert "task.compiling" in positions, chain
        assert "task.published" in positions, chain
        assert (
            positions["task.analyzed_partial"]
            < positions["task.source_quality_assessed"]
            < positions["task.compiling"]
            < positions["task.published"]
        ), chain
        # Exactly one source_quality_assessed audit on the fresh-run path.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.source_quality_assessed",
        ) == 1

        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        # Every evidence_span linked to the task has at least one row in
        # source_quality_assessments, with the mock evaluator's
        # signature dimensions.
        expected_spans = _distinct_evidence_span_ids_for_task(
            conn, task_id=task_id
        )
        assert len(expected_spans) >= 1, (
            "the seeded document must produce at least one verified "
            "claim_evidence_link with a non-null evidence_span_id"
        )
        sqa_rows = _fetch_sqa_for_task(conn, task_id=task_id)
        sqa_span_ids = {r["evidence_span_id"] for r in sqa_rows}
        assert expected_spans.issubset(sqa_span_ids), (
            f"spans missing from source_quality_assessments: "
            f"{expected_spans - sqa_span_ids}"
        )
        for r in sqa_rows:
            if r["evidence_span_id"] in expected_spans:
                # Mock evaluator writes overall_quality='unknown' and
                # contradiction_status='unchecked' for every row.
                assert str(r["overall_quality"]) == "unknown"
                assert str(r["contradiction_status"]) == "unchecked"

        # Final gate report.
        report = _fetch_final_gate_report(conn, task_id=task_id)
        assert report is not None
        assert report["decision"] == "approved"
        assert report["reason_code"] == "all_spans_verified_with_warnings"
        draft_id = report["draft_final_answer_id"]

        # Coverage gaps: at least one source_quality_warning, NO
        # source_quality_block, NO unverified_claim.
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)
        warning_gaps = _gaps_by_kind(gaps, "source_quality_warning")
        assert len(warning_gaps) >= 1, gaps
        assert _gaps_by_kind(gaps, "source_quality_block") == [], gaps
        assert _gaps_by_kind(gaps, "unverified_claim") == [], gaps

        # Every warning gap row has severity='warn' and gap_key prefix
        # 'span:<id>:source_quality_warning'. The mock evaluator
        # produces unknown+unchecked, so the reason list typically
        # includes both source_quality_unknown and
        # source_quality_contradiction_unchecked. We assert presence,
        # not equality, to stay resilient to future policy additions.
        for wg in warning_gaps:
            assert str(wg["severity"]) == "warn"
            assert str(wg["gap_key"]).endswith(":source_quality_warning")
            reasons = _reason_codes_in_gap(wg)
            assert reasons, wg
            # At least one of the two mock-driven reasons is expected.
            assert any(
                r in reasons
                for r in (
                    "source_quality_unknown",
                    "source_quality_contradiction_unchecked",
                )
            ), wg

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
    cgs = rb.get("coverage_gap_statements") or []
    assert any(
        str(g.get("kind")) == "source_quality_warning" for g in cgs
    ), cgs

    # GET /tasks/{id}/published-answer
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["status"] == "published"
    assert int(rb["version_no"]) == 1

    # GET /tasks/{id}/source-quality
    resp = client.get(f"/api/v1/tasks/{task_id}/source-quality")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    summary = rb["summary"]
    counts = summary["latest_overall_quality_counts"]
    assert int(counts.get("unknown", 0)) >= 1, summary
    assert int(summary.get("evidence_spans_total", 0)) >= 1, summary
    assert int(summary.get("spans_with_assessment", 0)) >= 1, summary

    # GET /evidence-spans/{es_id}/source-quality for one of the task's spans.
    span_id = next(iter(expected_spans))
    resp = client.get(f"/api/v1/evidence-spans/{span_id}/source-quality")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    items = rb.get("items") or []
    assert len(items) >= 1, rb
    latest = rb.get("latest_assessment")
    assert latest is not None
    assert str(latest["overall_quality"]) == "unknown"


# ===========================================================================
# TEST 2 — Block flow (Branch C' end-to-end, orchestrator stubbed)
# ===========================================================================
def test_phase_8_7_source_quality_block_flow_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B) Block flow.

    The mock evaluator only ever writes overall_quality='unknown', so
    Branch C' ('source_quality_block') is unreachable in production
    with the current mock. To activate it end-to-end via the real
    pipeline, we monkeypatch the symbol
    ``_wapp.consumers.task_created.run_source_quality_assessment``
    with a stub that, for every evidence_span linked to the task,
    inserts a v1 source_quality_assessments row with
    overall_quality='unsuitable'. The stub returns the canonical
    counts dict the consumer audit-payload code expects. The
    monkeypatch lives on the CONSUMER module because task_created.py
    imports the function at module load time.
    """
    _skip_if_db_unreachable()

    # The consumer module under the alias namespace. Resolve it via
    # sys.modules so the test does not need a top-level Python import
    # (which would re-trigger the worker bootstrap into the API
    # namespace).
    task_created_module = sys.modules[f"{_WORKER_ALIAS}.consumers.task_created"]

    # The stub. Signature must match run_source_quality_assessment as
    # called from task_created.py: ``(conn, *, task_id)``. Returns the
    # same counts shape as the real orchestrator's _empty_counts +
    # status='completed'.
    def _stub_run_source_quality_assessment(
        conn: Connection, *, task_id: uuid.UUID
    ) -> dict[str, Any]:
        # Resolve scope from task_masters. If the task does not exist,
        # mirror the real orchestrator's 'not_found' behavior (won't
        # happen in this test, but matches the contract).
        scope_row = conn.execute(
            text(
                "SELECT tenant_id, project_id FROM task_masters WHERE id = :t"
            ),
            {"t": task_id},
        ).first()
        if scope_row is None:
            return {
                "status": "not_found",
                "spans_total": 0,
                "assessed_count": 0,
                "already_assessed_count": 0,
                "not_found_count": 0,
                "invalid_target_count": 0,
                "error_count": 0,
            }
        tenant_id = uuid.UUID(str(scope_row[0]))
        project_id = uuid.UUID(str(scope_row[1]))

        # DISTINCT evidence_span_id linked to the task via
        # claim_evidence_links JOIN logical_claims (same query as the
        # real orchestrator). The filter on evidence_span_id IS NOT NULL
        # honors the cel_origin_xor CHECK.
        span_rows = conn.execute(
            text(
                """
                SELECT DISTINCT cel.evidence_span_id
                FROM claim_evidence_links cel
                JOIN logical_claims lc ON lc.id = cel.claim_logical_id
                WHERE lc.task_id = :tid
                  AND cel.evidence_span_id IS NOT NULL
                ORDER BY cel.evidence_span_id
                """
            ),
            {"tid": task_id},
        ).fetchall()
        span_ids = [uuid.UUID(str(r[0])) for r in span_rows]

        # Insert one v1 source_quality_assessments row per span with
        # overall_quality='unsuitable'. All codomains are 0007-valid.
        # The idempotency_key is deterministic per (task, span) and
        # explicitly differs from the real orchestrator's ':v1' suffix
        # to surface a stub-vs-real mix-up loudly if it ever happens.
        for span_id in span_ids:
            idempotency_key = (
                f"task:{task_id}:span:{span_id}:test-block-v1"
            )
            payload = {
                "trigger": "phase_8_7h_block_flow",
                "task_id": str(task_id),
                "evidence_span_id": str(span_id),
            }
            conn.execute(
                text(
                    """
                    INSERT INTO source_quality_assessments (
                      tenant_id, project_id,
                      evidence_span_id, document_chunk_id, document_id,
                      version_no,
                      source_type, source_role, authority_level,
                      independence_level, freshness, relevance,
                      extract_quality, contradiction_status,
                      overall_quality, confidence,
                      evaluator_name, evaluator_version,
                      policy_name, policy_version,
                      idempotency_key, payload
                    ) VALUES (
                      :tenant_id, :project_id,
                      :evidence_span_id, NULL, NULL,
                      1,
                      'user_document', 'unclear', 'unknown',
                      'unknown', 'undated', 'direct_support',
                      'exact_quote_match', 'no_known_contradiction',
                      'unsuitable', 0.5,
                      'test_source_quality_evaluator', '0.1.0',
                      'test_source_quality_block_policy', '0.1.0',
                      :idempotency_key, CAST(:payload AS JSONB)
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "evidence_span_id": span_id,
                    "idempotency_key": idempotency_key,
                    "payload": json.dumps(payload, sort_keys=True),
                },
            )

        n = len(span_ids)
        return {
            "status": "completed",
            "spans_total": n,
            "assessed_count": n,
            "already_assessed_count": 0,
            "not_found_count": 0,
            "invalid_target_count": 0,
            "error_count": 0,
        }

    # Patch the symbol on the consumer module (NOT on the orchestrator
    # module): the consumer imports the function at module load time
    # and binds the name locally.
    monkeypatch.setattr(
        task_created_module,
        "run_source_quality_assessment",
        _stub_run_source_quality_assessment,
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
        event, redis_consumer_name="realistic_8_7h_block"
    )
    assert rc == "processed", rc

    # ----------------------------- DB assertions --------------------------
    with engine.connect() as conn:
        # Task terminal status: 'analyzed_partial' (rejected scenario).
        assert _fetch_task_status(conn, task_id=task_id) == "analyzed_partial"

        # Audit chain integrity.
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        # Audit chain ends with task.publication_held (NOT task.published).
        chain = _fetch_audit_event_types_for_task(conn, task_id=task_id)
        assert chain, chain
        terminal_event_type = chain[-1][1]
        assert terminal_event_type == "task.publication_held", chain
        # task.published must NOT appear at all.
        published_count = _count_audit_event(
            conn, task_id=task_id, event_type="task.published"
        )
        assert published_count == 0, chain
        # source_quality_assessed audit was emitted exactly once.
        assert _count_audit_event(
            conn,
            task_id=task_id,
            event_type="task.source_quality_assessed",
        ) == 1

        # Source quality rows: at least one with overall_quality='unsuitable'
        # and the stub's evaluator_name (sanity check that the stub fired
        # instead of the real mock evaluator).
        sqa_rows = _fetch_sqa_for_task(conn, task_id=task_id)
        assert len(sqa_rows) >= 1, "stub must have inserted at least one row"
        for r in sqa_rows:
            assert str(r["overall_quality"]) == "unsuitable", r
            assert str(r["evaluator_name"]) == "test_source_quality_evaluator", r

        # Final gate report.
        report = _fetch_final_gate_report(conn, task_id=task_id)
        assert report is not None
        assert report["decision"] == "rejected"
        assert report["reason_code"] == "source_quality_block"
        draft_id = report["draft_final_answer_id"]

        # Coverage gaps: at least one source_quality_block of
        # severity='block' whose details.reasons contains
        # 'source_quality_unsuitable'. NO source_quality_warning, NO
        # unverified_claim.
        gaps = _fetch_coverage_gaps(conn, draft_id=draft_id)
        block_gaps = _gaps_by_kind(gaps, "source_quality_block")
        assert len(block_gaps) >= 1, gaps
        assert _gaps_by_kind(gaps, "source_quality_warning") == [], gaps
        assert _gaps_by_kind(gaps, "unverified_claim") == [], gaps

        for bg in block_gaps:
            assert str(bg["severity"]) == "block"
            assert str(bg["gap_key"]).endswith(":source_quality_block")
            reasons = _reason_codes_in_gap(bg)
            assert "source_quality_unsuitable" in reasons, bg

        # No published_answers v1 inserted.
        assert _count_published_for_task(conn, task_id=task_id) == 0

    # ----------------------------- HTTP read endpoints --------------------
    # GET /tasks/{id}/final-gate-report -> 200, rejected, block reason.
    resp = client.get(f"/api/v1/tasks/{task_id}/final-gate-report")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    assert rb["decision"] == "rejected"
    assert rb["reason_code"] == "source_quality_block"
    cgs = rb.get("coverage_gap_statements") or []
    assert any(
        str(g.get("kind")) == "source_quality_block" for g in cgs
    ), cgs

    # GET /tasks/{id}/published-answer -> 404 RESOURCE_NOT_FOUND with
    # details.resource='published_answers'.
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 404, resp.text
    err_body = resp.json()
    err = err_body.get("error")
    assert err is not None, err_body
    assert str(err.get("code")) == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert str(details.get("resource")) == "published_answers"

    # GET /tasks/{id}/source-quality -> 200,
    # summary.latest_overall_quality_counts.unsuitable >= 1.
    resp = client.get(f"/api/v1/tasks/{task_id}/source-quality")
    assert resp.status_code == 200, resp.text
    rb = resp.json()
    summary = rb["summary"]
    counts = summary["latest_overall_quality_counts"]
    assert int(counts.get("unsuitable", 0)) >= 1, summary
