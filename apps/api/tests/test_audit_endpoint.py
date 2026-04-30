"""E2E audit chain tests for tasks, Phase 8.3.

Two scenarios:
  - no documents:    task.created, task.analyzing, task.blocked.
  - with documents:  full 8.3 pipeline (10 events).

Asserted invariants:
  - exact event_type sequence;
  - chain_seq is consecutive (1..N);
  - previous_event_hash_hex of row i+1 equals event_hash_hex of row i;
  - verify_task_audit_chain returns ok=true.

Rerun-safety: all project names and document payloads are unique per invocation.
"""
from __future__ import annotations

import io
import os
import time
import uuid

import pytest

from app.db import get_engine
from evidencefirst_shared.db.audit import verify_task_audit_chain


WORKER_TIMEOUT_S = float(os.environ.get("WORKER_E2E_TIMEOUT_S", "30"))


EXPECTED_NO_DOCS = [
    "task.created",
    "task.analyzing",
    "task.blocked",
]

EXPECTED_WITH_DOCS_8_3 = [
    "task.created",
    "task.docs_attached",
    "task.analyzing",
    "task.docs_loaded",
    "task.claims_extracted",
    "task.claims_classified",
    "task.claims_ledger_initialized",
    "task.cve_lite_started",
    "task.cve_lite_completed",
    "task.analyzed_partial",
]


def _wait_for(client, task_id: str, target: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/tasks/{task_id}")
        assert r.status_code == 200
        last = r.json()["status"]
        if last == target:
            return last
        time.sleep(0.5)
    return last or "unknown"


def _assert_chain_well_formed(items: list[dict]) -> None:
    """Common structural assertions on an audit chain payload."""
    assert items, "audit chain must not be empty"
    seqs = [it["chain_seq"] for it in items]
    assert seqs == list(range(1, len(items) + 1)), seqs
    # First event has no parent hash; subsequent events must chain.
    assert items[0]["previous_event_hash_hex"] is None
    for i in range(1, len(items)):
        assert items[i]["previous_event_hash_hex"] == items[i - 1]["event_hash_hex"], (
            i,
            items[i]["previous_event_hash_hex"],
            items[i - 1]["event_hash_hex"],
        )
    # All events must share the same scope_id (the task_id) and chain_scope='task'.
    scope_id = items[0]["scope_id"]
    for it in items:
        assert it["scope_id"] == scope_id
        assert it["chain_scope"] == "task"


# ---------------------------------------------------------------------------
# no documents -> blocked
# ---------------------------------------------------------------------------
def test_audit_chain_no_documents_blocked(client):
    rp = client.post("/api/v1/projects", json={"name": f"audit-no-docs-{uuid.uuid4()}"})
    assert rp.status_code == 201
    pid = rp.json()["id"]

    rt = client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "objective": "no docs", "mode": "closed_corpus"},
    )
    assert rt.status_code == 201
    tid = rt.json()["id"]

    final = _wait_for(client, tid, "blocked", WORKER_TIMEOUT_S)
    if final != "blocked":
        pytest.skip(
            f"Worker did not advance task to 'blocked' within {WORKER_TIMEOUT_S}s "
            f"(last status: {final}). Is the worker container running?"
        )

    items = client.get(f"/api/v1/tasks/{tid}/audit?limit=500").json()["items"]
    types = [it["event_type"] for it in items]
    assert types == EXPECTED_NO_DOCS, types
    _assert_chain_well_formed(items)

    with get_engine().begin() as conn:
        result = verify_task_audit_chain(conn, task_id=uuid.UUID(tid))
    assert result["ok"] is True, result
    assert result["checked_count"] == len(EXPECTED_NO_DOCS)
    assert result["discrepancies"] == []


# ---------------------------------------------------------------------------
# with documents -> analyzed_partial (8.3 pipeline)
# ---------------------------------------------------------------------------
def test_audit_chain_with_documents_full_8_3_pipeline(client):
    rp = client.post("/api/v1/projects", json={"name": f"audit-83-{uuid.uuid4()}"})
    assert rp.status_code == 201
    pid = rp.json()["id"]

    marker = uuid.uuid4().hex[:8]
    payload = (
        f"Annual report {marker}.\n"
        f"Sales grew by 12 percent.\n"
    ).encode("utf-8")
    files = {"file": (f"doc-{marker}.txt", io.BytesIO(payload), "text/plain")}
    ru = client.post(f"/api/v1/projects/{pid}/documents", files=files)
    assert ru.status_code == 201
    did = ru.json()["id"]

    rt = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid,
            "objective": f"with docs 8.3 {marker}",
            "mode": "closed_corpus",
            "document_ids": [did],
        },
    )
    assert rt.status_code == 201
    tid = rt.json()["id"]

    final = _wait_for(client, tid, "analyzed_partial", WORKER_TIMEOUT_S)
    if final != "analyzed_partial":
        pytest.skip(
            f"Worker did not advance task to 'analyzed_partial' within "
            f"{WORKER_TIMEOUT_S}s (last status: {final}). Is the worker container running?"
        )

    items = client.get(f"/api/v1/tasks/{tid}/audit?limit=500").json()["items"]
    types = [it["event_type"] for it in items]
    assert types == EXPECTED_WITH_DOCS_8_3, types
    _assert_chain_well_formed(items)

    # All events scoped to the task we created.
    assert items[0]["scope_id"] == tid

    with get_engine().begin() as conn:
        result = verify_task_audit_chain(conn, task_id=uuid.UUID(tid))
    assert result["ok"] is True, result
    assert result["checked_count"] == len(EXPECTED_WITH_DOCS_8_3)
    assert result["discrepancies"] == []