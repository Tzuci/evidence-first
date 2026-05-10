"""Phase 8.5 — Realistic Flow Test 1: published_answer withdrawal end-to-end-like.

Scope (cross-component, root-tests):
  - API POST /api/v1/published-answers/{id}/withdrawal-requests
    intercepted with FakeRedis (no real Redis), captured xadd
    -> reconstructed as a Redis-decoded event dict
    -> fed to apps/worker/app/consumers/dispatch.handle_event
    -> worker consumer + lifecycle service run against the real DB
    -> assert published_answers transition, lifecycle events, audit,
       EPR, audit-chain integrity.

What this test is and is not:
  - It IS a realistic exercise of the producer + dispatcher + consumer
    + service pipeline against a real Postgres. Every layer below the
    Redis transport runs the production code path; only Redis itself is
    a FakeRedis that records xadd calls.
  - It is NOT a Redis-loop test: no XREADGROUP, no consumer groups, no
    worker main() loop. The transport semantics (delivery, ack, claim)
    are covered by their own dedicated tests in apps/worker/tests/.
  - It does NOT touch source_loss; that is a separate realistic-flow
    test scheduled for the next block.

Hard package-collision note (why this file is unusual):
  Both apps/api/app and apps/worker/app are top-level packages literally
  named ``app``. Inside each, modules use relative imports
  (``from ..db import ...``). A naive ``sys.path`` trick that lists both
  apps/api and apps/worker would let one ``app`` win and break the other.
  We therefore:
    1) prepend apps/api + packages/shared to sys.path so ``import app``
       resolves to the API,
    2) import API normally (``from app.main import app as api_app``,
       ``from app.routes import answers as answers_route``),
    3) load the worker package via importlib.util under a synthetic
       top-level alias ``_wapp``, registering every submodule
       (``_wapp``, ``_wapp.consumers``, ``_wapp.services``, plus each
       leaf module) in sys.modules so the worker's relative imports
       resolve within its own namespace, without colliding with API's
       ``app`` namespace.
  This is the only way to keep this file at tests/ root (cross-component
  by design) without disturbing either package. The alternative would be
  to drop it under apps/api/tests/ and use a similar alias trick anyway,
  with the disadvantage of pretending this is an API-suite test when in
  fact it crosses worker code.

DB requirement:
  The same Postgres used by ``make test-db`` — i.e. DATABASE_URL is set
  and the migrations + seed are applied. We do not set DATABASE_URL
  ourselves: if it's missing or unreachable, the test is skipped
  (matching the convention in apps/api/tests/test_published_answer_withdrawal_request.py).

Redelivery scenarios covered in a single test:
  - First delivery: ``processed``.
  - Second delivery with the same idempotency_key (different
    redis_consumer_name): ``skipped_already_succeeded``; nothing in the
    DB grows.
  - Third delivery with a fresh consumer-level idempotency_key but the
    same lifecycle_idempotency_key: ``processed`` (a new EPR slot is
    consumed), but the service-level lifecycle UNIQUE and the
    status-guarded UPDATE make the call a no-op on the lifecycle table,
    on published_answers, and on the audit chain.
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
# This file lives at <repo>/tests/test_phase_8_5_withdrawal_flow.py, so
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

# Shared helpers used to validate the audit chain end-to-end.
from evidencefirst_shared.db.audit import verify_task_audit_chain  # noqa: E402


# ---------------------------------------------------------------------------
# Worker bootstrap under alias namespace ``_wapp``
# ---------------------------------------------------------------------------
# We load apps/worker/app/* under the synthetic top-level name ``_wapp`` so
# that worker's ``from ..db import transaction`` resolves to ``_wapp.db``
# rather than to API's ``app.db``. This must happen exactly once per
# interpreter; subsequent calls reuse the cached entries.
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

    # All services that the three consumers transitively import. We load
    # them upfront so the consumer modules can resolve their relative
    # imports without going through Python's normal package machinery
    # (which would consult sys.path and re-trigger the ``app`` collision).
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


# Load the worker once, at import time. If the DB is unreachable the
# individual tests will skip — but the worker code must at least be
# importable for the test module to be collectable.
_dispatch = _bootstrap_worker()


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
EXPECTED_STREAM = "app.events.published_answer_withdrawal_requested"
EXPECTED_EVENT_TYPE = "published_answer.withdrawal_requested"
WORKER_CONSUMER_NAME = "published_answer_withdrawal"  # stable, logical default
AUDIT_EVENT_WITHDRAWN = "published_answer.withdrawn"


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


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------
def _seeded_dev(
    conn: Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Ensure tenant + user; create a FRESH project and task per invocation.

    Returns (tenant_id, project_id, user_id, task_id).

    Project and task are always fresh so each test invocation operates on
    an isolated scope (no cross-test interference on task_id-scoped audit
    chains, no UNIQUE collisions on project name).
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
                {"t": tenant_id, "n": f"phase-8-5-flow-{uuid.uuid4()}"},
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


def _create_published_answer(
    conn: Connection, *, task_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create draft_final_answers v1 + final_gate_reports approved +
    published_answers v1 (status='published') for the given task.

    Returns (draft_id, gate_report_id, published_answer_id).

    The FK chain task -> draft -> gate -> published is locked down by the
    composite UNIQUE/FK declared in 0005_answers_gate.sql. We keep the
    chain minimal (no spans, no claim links): the producer endpoint
    resolves only through (published_answers JOIN task_masters), and the
    lifecycle service touches only published_answers itself plus
    published_answer_lifecycle_events.
    """
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
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "st": f"summary-{uuid.uuid4()}",
                },
            ).first()[0]
        )
    )

    gate_id = uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_gate_reports
                        (id, task_id, draft_final_answer_id, decision, reason_code)
                    VALUES (:id, :t, :d, 'approved', 'all_spans_verified')
                    RETURNING id
                    """
                ),
                {"id": uuid.uuid4(), "t": task_id, "d": draft_id},
            ).first()[0]
        )
    )

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
                    "h": _unique_hex(),
                },
            ).first()[0]
        )
    )

    return draft_id, gate_id, pa_id


# ---------------------------------------------------------------------------
# DB inspection helpers
# ---------------------------------------------------------------------------
def _fetch_published_answer(
    conn: Connection, *, pa_id: uuid.UUID
) -> dict[str, Any]:
    row = conn.execute(
        text(
            """
            SELECT status, withdrawn_at, superseded_at, superseded_by_id
            FROM published_answers
            WHERE id = :pid
            """
        ),
        {"pid": pa_id},
    ).one()
    return dict(row._mapping)


def _count_lifecycle_events(
    conn: Connection,
    *,
    pa_id: uuid.UUID,
    event_type: str | None = None,
) -> int:
    if event_type is None:
        n = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE published_answer_id = :pid
                """
            ),
            {"pid": pa_id},
        ).scalar_one()
    else:
        n = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM published_answer_lifecycle_events
                WHERE published_answer_id = :pid AND event_type = :etype
                """
            ),
            {"pid": pa_id, "etype": event_type},
        ).scalar_one()
    return int(n)


def _fetch_lifecycle_event_idempotency_keys(
    conn: Connection, *, pa_id: uuid.UUID
) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT idempotency_key FROM published_answer_lifecycle_events
            WHERE published_answer_id = :pid
            """
        ),
        {"pid": pa_id},
    ).fetchall()
    return {str(r[0]) for r in rows}


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
# FakeRedis (Block 4A-1 surface area only)
# ---------------------------------------------------------------------------
class FakeRedis:
    """Minimal Redis stub.

    Only ``xadd`` is implemented — that is the entire Redis surface used
    by ``app.routes.answers.request_published_answer_withdrawal``. We
    deliberately do NOT add other Redis methods; this object is meant to
    be installed as the return value of ``answers_route.get_redis`` and
    nothing else.

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
    """Patch ``answers_route.get_redis`` with a fresh FakeRedis.

    The route module captured ``get_redis`` at import time via
    ``from ..redis import get_redis``, so the patched binding must live
    on ``app.routes.answers``, NOT on ``app.redis``.
    """
    fake = FakeRedis()
    monkeypatch.setattr(answers_route, "get_redis", lambda: fake)
    return fake


# ===========================================================================
# THE realistic flow test
# ===========================================================================
def test_phase_8_5_withdrawal_request_api_to_worker_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end-like withdrawal flow: API -> FakeRedis -> dispatcher
    -> withdrawal consumer -> lifecycle service -> DB + audit.

    Steps in order:

      (1) seed DB: tenant + user + fresh project + fresh task + draft +
          approved gate + published_answer status='published'.
      (2) install FakeRedis on the API route module.
      (3) POST /api/v1/published-answers/{pa_id}/withdrawal-requests
          with a fully-populated body (stable consumer + lifecycle keys,
          requested_by, opaque event_payload).
      (4) assert 202 response envelope.
      (5) assert FakeRedis observed exactly one xadd with the expected
          stream and field shape, and capture the fields dict.
      (6) hand the captured fields to dispatch.handle_event with a
          synthetic redis_consumer_name (the dispatcher must NOT forward
          this to the withdrawal consumer).
      (7) assert DB post-processing: published_answers transitioned to
          'withdrawn', lifecycle log has the canonical pair, audit
          'published_answer.withdrawn' emitted exactly once, audit chain
          verifies, task_masters.status invariant, EPR row reflects the
          consumer's stable name + full scope.
      (8) replay the same event with a different redis_consumer_name:
          dispatcher returns "skipped_already_succeeded", DB unchanged.
      (9) replay a variant: fresh consumer-level idempotency_key but the
          same lifecycle_idempotency_key. Dispatcher returns "processed"
          (fresh EPR slot), but the lifecycle UNIQUE and the
          status-guarded UPDATE collapse the service call to a no-op:
          lifecycle count stays 2, audit stays 1, published_answers
          row unchanged. This exercises both idempotency layers at once.
    """
    _skip_if_db_unreachable()

    # ----------------------------- (1) seed -------------------------------
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, project_id, user_id, task_id = _seeded_dev(conn)
        _draft_id, _gate_id, pa_id = _create_published_answer(
            conn, task_id=task_id
        )
        task_status_before = _fetch_task_status(conn, task_id=task_id)

    # Sanity: starting state is exactly what the test assumes.
    with engine.connect() as conn:
        pa_initial = _fetch_published_answer(conn, pa_id=pa_id)
        assert pa_initial["status"] == "published"
        assert pa_initial["withdrawn_at"] is None
        assert pa_initial["superseded_at"] is None
        assert pa_initial["superseded_by_id"] is None
        assert _count_lifecycle_events(conn, pa_id=pa_id) == 0
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 0

    # --------------------- (2) install FakeRedis --------------------------
    fake = _install_fake_redis(monkeypatch)
    client = TestClient(api_app)

    # --------------------- (3) POST withdrawal-requests -------------------
    # Stable keys so the test can assert on the exact values that travel
    # through Redis and into the lifecycle row. The lifecycle key is
    # deliberately distinct from the consumer key so step (9) can reuse
    # it independently.
    consumer_idem_1 = f"realistic-consumer-{_unique_hex()}"
    lifecycle_idem = f"realistic-lifecycle-{_unique_hex()}"
    request_body = {
        "reason": "realistic test withdrawal",
        "idempotency_key": consumer_idem_1,
        "lifecycle_idempotency_key": lifecycle_idem,
        "requested_by": str(user_id),
        "event_payload": {"scenario": "phase_8_5_withdrawal_flow"},
    }

    resp = client.post(
        f"/api/v1/published-answers/{pa_id}/withdrawal-requests",
        json=request_body,
    )

    # --------------------- (4) assert API response ------------------------
    assert resp.status_code == 202, resp.text
    rb = resp.json()
    assert rb["status"] == "queued"
    assert rb["event_type"] == EXPECTED_EVENT_TYPE
    assert rb["stream"] == EXPECTED_STREAM
    assert rb["published_answer_id"] == str(pa_id)
    assert rb["idempotency_key"] == consumer_idem_1
    assert rb["lifecycle_idempotency_key"] == lifecycle_idem
    response_event_id = uuid.UUID(rb["event_id"])

    # --------------------- (5) assert FakeRedis ---------------------------
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
    assert fields["published_answer_id"] == str(pa_id)
    assert fields["idempotency_key"] == consumer_idem_1
    assert fields["lifecycle_idempotency_key"] == lifecycle_idem
    assert fields["event_reason"] == "realistic test withdrawal"
    assert fields["requested_by"] == str(user_id)
    assert fields["tenant_id"] == str(tenant_id)
    assert fields["task_id"] == str(task_id)
    assert fields["project_id"] == str(project_id)
    # event_payload travels as JSON, compact + sort_keys, under the
    # ``event_payload_json`` key (the consumer doesn't read it; it's
    # there for replay/forensic purposes only).
    assert fields["event_payload_json"] == json.dumps(
        request_body["event_payload"], separators=(",", ":"), sort_keys=True
    )

    # --------------------- (6) dispatcher first run -----------------------
    # Reconstruct the event exactly the way the worker would after
    # decoding a Redis stream entry: a plain dict[str, str].
    event = dict(fields)

    rc = _dispatch.handle_event(event, redis_consumer_name="realistic_worker_1")
    assert rc == "processed"

    # --------------------- (7) post-processing assertions -----------------
    with engine.connect() as conn:
        # published_answers transitioned to terminal 'withdrawn'.
        pa_after = _fetch_published_answer(conn, pa_id=pa_id)
        assert pa_after["status"] == "withdrawn"
        assert pa_after["withdrawn_at"] is not None
        assert pa_after["superseded_at"] is None
        assert pa_after["superseded_by_id"] is None

        # Lifecycle: exactly one withdrawal_requested + one withdrawn,
        # both bound to lifecycle_idem.
        assert _count_lifecycle_events(conn, pa_id=pa_id) == 2
        assert _count_lifecycle_events(
            conn, pa_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, pa_id=pa_id, event_type="withdrawn"
        ) == 1
        idem_keys = _fetch_lifecycle_event_idempotency_keys(conn, pa_id=pa_id)
        assert idem_keys == {lifecycle_idem}

        # Audit: 'published_answer.withdrawn' emitted exactly once on the
        # task's chain, and the chain itself verifies end-to-end.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 1
        chain_ok = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok["ok"] is True, chain_ok

        # task_masters.status is invariant. The lifecycle lives on
        # published_answers, NOT on the task — this is the Phase 8.5
        # invariant we lock down in DB-state assertions.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        # EPR: keyed on the consumer's STABLE logical name and on the
        # consumer-level idempotency_key from the event. The dispatcher
        # MUST NOT have shadowed the consumer_name with
        # 'realistic_worker_1' — verified both by the lookup succeeding
        # under WORKER_CONSUMER_NAME and (defensively) by counting zero
        # rows under the per-instance worker name.
        epr = _fetch_epr(
            conn,
            consumer_name=WORKER_CONSUMER_NAME,
            idempotency_key=consumer_idem_1,
        )
        assert epr is not None
        assert epr["processing_status"] == "succeeded"
        assert epr["tenant_id"] == tenant_id
        assert epr["project_id"] == project_id
        assert epr["task_id"] == task_id
        assert _count_epr(
            conn,
            consumer_name="realistic_worker_1",
            idempotency_key=consumer_idem_1,
        ) == 0

    # --------------------- (8) redelivery, same keys ----------------------
    rc2 = _dispatch.handle_event(
        event, redis_consumer_name="realistic_worker_2"
    )
    assert rc2 == "skipped_already_succeeded"

    with engine.connect() as conn:
        # Lifecycle log size unchanged.
        assert _count_lifecycle_events(conn, pa_id=pa_id) == 2
        assert _count_lifecycle_events(
            conn, pa_id=pa_id, event_type="withdrawal_requested"
        ) == 1
        assert _count_lifecycle_events(
            conn, pa_id=pa_id, event_type="withdrawn"
        ) == 1

        # Audit emission unchanged.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
        ) == 1

        # Exactly one EPR row keyed on (stable consumer, consumer_idem_1).
        assert _count_epr(
            conn,
            consumer_name=WORKER_CONSUMER_NAME,
            idempotency_key=consumer_idem_1,
        ) == 1

        # Published answer row unchanged across the redelivery.
        pa_after_replay = _fetch_published_answer(conn, pa_id=pa_id)
        assert pa_after_replay["status"] == "withdrawn"
        assert pa_after_replay["withdrawn_at"] == pa_after["withdrawn_at"]
        assert pa_after_replay["superseded_at"] is None
        assert pa_after_replay["superseded_by_id"] is None

    # --------------------- (9) fresh consumer key, same lifecycle key -----
    # New event with a fresh event_id and a fresh consumer-level
    # idempotency_key. The lifecycle key is intentionally identical so
    # the service-level UNIQUE catches the duplicate. The producer's
    # scope fields (tenant/project/task) stay the same because the
    # published_answer is the same.
    consumer_idem_3 = f"realistic-consumer-{_unique_hex()}"
    event_third: dict[str, Any] = dict(event)
    event_third["event_id"] = str(uuid.uuid4())
    event_third["idempotency_key"] = consumer_idem_3
    # lifecycle_idempotency_key stays == lifecycle_idem.

    rc3 = _dispatch.handle_event(
        event_third, redis_consumer_name="realistic_worker_3"
    )
    # Fresh consumer EPR slot -> begin_processing returns 'started', the
    # consumer proceeds to the service, the service detects the row is
    # already withdrawn ('already_withdrawn' branch), and the consumer
    # maps the outcome to "processed".
    assert rc3 == "processed"

    with engine.connect() as conn:
        # Lifecycle log size UNCHANGED: the service-level UNIQUE on
        # (published_answer_id, event_type, idempotency_key) absorbs the
        # second pair of inserts. (In this scenario the service actually
        # short-circuits earlier, in the 'already_withdrawn' branch, so
        # no inserts are even attempted — but the UNIQUE would catch
        # them anyway. Either path is safe.)
        assert _count_lifecycle_events(conn, pa_id=pa_id) == 2
        # Audit count UNCHANGED: the status-guarded UPDATE only emits an
        # audit row when it actually changes a row; a redelivery against
        # an already-withdrawn published_answer is a no-op.
        assert _count_audit_event(
            conn, task_id=task_id, event_type=AUDIT_EVENT_WITHDRAWN
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
        assert epr_third["task_id"] == task_id

        # Published answer row is the very same row, untouched, with the
        # original withdrawn_at preserved.
        pa_final = _fetch_published_answer(conn, pa_id=pa_id)
        assert pa_final["status"] == "withdrawn"
        assert pa_final["withdrawn_at"] == pa_after["withdrawn_at"]
        assert pa_final["superseded_at"] is None
        assert pa_final["superseded_by_id"] is None

        # task_masters.status invariant survives every redelivery.
        assert _fetch_task_status(conn, task_id=task_id) == task_status_before

        # And the audit chain still verifies after all three deliveries.
        chain_ok_final = verify_task_audit_chain(conn, task_id=task_id)
        assert chain_ok_final["ok"] is True, chain_ok_final
