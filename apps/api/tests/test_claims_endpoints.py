"""End-to-end tests for the claim read-only endpoints (Phase 8.3).

Endpoints covered:
  - GET /api/v1/tasks/{task_id}/raw-claims
  - GET /api/v1/tasks/{task_id}/classified-claims
  - GET /api/v1/tasks/{task_id}/claims                  (latest entry per logical_id)
  - GET /api/v1/claims/{claim_logical_id}/history       (v1 then v2)
  - GET /api/v1/claims/{claim_logical_id}/evidence      (links + verifications)
  - 404 on unknown task_id
  - 404 on unknown claim_logical_id

Drives the live API + worker: uploads a fixture-like document, creates a task with
document_ids, polls until 'analyzed_partial', then asserts on the claim views.

Rerun-safety:
  - project name unique per invocation;
  - document payload contains a uuid.uuid4()-derived marker so content_hash differs
    across runs and dedup paths are not exercised here;
  - bogus IDs for 404 paths are uuid.uuid4()-fresh.

Assertions are deliberately structural (>= 1, version chains, link presence) instead
of exact counts: the deterministic extractor's exact claim count depends on a
sentence splitter that is intentionally not pinned by the API contract.
"""
from __future__ import annotations

import io
import os
import time
import uuid

import pytest


WORKER_TIMEOUT_S = float(os.environ.get("WORKER_E2E_TIMEOUT_S", "30"))


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


def _setup_task_with_doc(client) -> tuple[str, str]:
    """Create a fresh project, upload a small text document, create a task with
    document_ids, and wait until the worker advances the task to 'analyzed_partial'.

    Returns (project_id, task_id) as JSON-string UUIDs (HTTP-friendly).
    """
    rp = client.post("/api/v1/projects", json={"name": f"claims-e2e-{uuid.uuid4()}"})
    assert rp.status_code == 201, rp.text
    pid = rp.json()["id"]

    marker = uuid.uuid4().hex[:8]
    payload = (
        f"Annual report {marker}.\n"
        f"Sales grew by 12 percent.\n"
        f"There were 3412 new customers.\n"
    ).encode("utf-8")
    files = {"file": (f"doc-{marker}.txt", io.BytesIO(payload), "text/plain")}
    ru = client.post(f"/api/v1/projects/{pid}/documents", files=files)
    assert ru.status_code == 201, ru.text
    did = ru.json()["id"]

    rt = client.post(
        "/api/v1/tasks",
        json={
            "project_id": pid,
            "objective": f"claims-e2e {marker}",
            "mode": "closed_corpus",
            "document_ids": [did],
        },
    )
    assert rt.status_code == 201, rt.text
    tid = rt.json()["id"]

    final = _wait_for(client, tid, "analyzed_partial", WORKER_TIMEOUT_S)
    if final != "analyzed_partial":
        pytest.skip(
            f"Worker did not advance task to 'analyzed_partial' within "
            f"{WORKER_TIMEOUT_S}s (last status: {final}). Is the worker container running?"
        )
    return pid, tid


# ---------------------------------------------------------------------------
# raw-claims
# ---------------------------------------------------------------------------
def test_get_raw_claims_returns_items(client):
    _pid, tid = _setup_task_with_doc(client)
    r = client.get(f"/api/v1/tasks/{tid}/raw-claims")
    assert r.status_code == 200
    body = r.json()
    items = body["items"]
    assert len(items) >= 1
    for it in items:
        assert it["task_id"] == tid
        assert it["extractor_name"]
        assert it["extractor_version"]
        assert it["raw_text"]
        # logical_claim_id, document_chunk_id, evidence_span_id are valid UUIDs
        uuid.UUID(it["logical_claim_id"])
        uuid.UUID(it["document_chunk_id"])
        uuid.UUID(it["evidence_span_id"])


def test_get_raw_claims_404_on_unknown_task(client):
    bogus = uuid.uuid4()
    r = client.get(f"/api/v1/tasks/{bogus}/raw-claims")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# classified-claims
# ---------------------------------------------------------------------------
def test_get_classified_claims_returns_items(client):
    _pid, tid = _setup_task_with_doc(client)
    r = client.get(f"/api/v1/tasks/{tid}/classified-claims")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    for it in items:
        assert it["claim_type"] == "factual"
        assert it["domain_tag"] == "general"
        assert it["classifier_name"]
        assert it["classifier_version"]
        uuid.UUID(it["raw_claim_id"])
        uuid.UUID(it["logical_claim_id"])


def test_get_classified_claims_404_on_unknown_task(client):
    bogus = uuid.uuid4()
    r = client.get(f"/api/v1/tasks/{bogus}/classified-claims")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# claims (latest per logical_id)
# ---------------------------------------------------------------------------
def test_get_task_claims_returns_latest_versions(client):
    _pid, tid = _setup_task_with_doc(client)
    r = client.get(f"/api/v1/tasks/{tid}/claims")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    seen_logical_ids: set[str] = set()
    for it in items:
        # Each row in this view is the LATEST entry for its logical claim.
        # In our fixture each quote matches the chunk verbatim, so CVE-lite passes:
        # the latest version is v2 with state='verified_fact'.
        assert it["version_no"] == 2
        assert it["state"] == "verified_fact"
        assert it["support_scope"] == "supported_by_user_corpus_only"
        assert it["user_provided_dependency"] == "supported_by_user_corpus_only"
        assert it["claim_logical_id"] not in seen_logical_ids, "duplicate logical claim in latest view"
        seen_logical_ids.add(it["claim_logical_id"])


def test_get_task_claims_404_on_unknown_task(client):
    bogus = uuid.uuid4()
    r = client.get(f"/api/v1/tasks/{bogus}/claims")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------
def test_claim_history_returns_v1_then_v2(client):
    _pid, tid = _setup_task_with_doc(client)
    latest = client.get(f"/api/v1/tasks/{tid}/claims").json()["items"]
    assert latest, "no latest claims returned by the task view"
    lc_id = latest[0]["claim_logical_id"]

    h = client.get(f"/api/v1/claims/{lc_id}/history")
    assert h.status_code == 200
    versions = h.json()["items"]
    # We expect exactly v1 (candidate) then v2 (verified_fact in the matching fixture).
    assert [v["version_no"] for v in versions] == [1, 2], versions
    assert versions[0]["state"] == "candidate"
    assert versions[0]["transition_reason"]  # populated by extractor
    assert versions[1]["state"] in ("verified_fact", "unverifiable")
    assert versions[1]["transition_reason"] in ("cve_lite_pass", "cve_lite_quote_mismatch")


def test_claim_history_404_on_unknown_logical_claim(client):
    bogus = uuid.uuid4()
    r = client.get(f"/api/v1/claims/{bogus}/history")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# evidence aggregate
# ---------------------------------------------------------------------------
def test_claim_evidence_returns_links_and_verifications(client):
    _pid, tid = _setup_task_with_doc(client)
    latest = client.get(f"/api/v1/tasks/{tid}/claims").json()["items"]
    assert latest
    lc_id = latest[0]["claim_logical_id"]

    e = client.get(f"/api/v1/claims/{lc_id}/evidence")
    assert e.status_code == 200
    body = e.json()

    assert body["claim_logical_id"] == lc_id
    assert body["latest_entry"] is not None
    assert body["latest_entry"]["version_no"] == 2

    links = body["evidence_links"]
    assert len(links) >= 1
    for ln in links:
        assert ln["claim_logical_id"] == lc_id
        # In MVP-0 retrieved_source_span_id must be NULL and evidence_span_id present.
        assert ln["retrieved_source_span_id"] is None
        uuid.UUID(ln["evidence_span_id"])
        assert ln["link_role"] in ("primary_support", "supporting_context", "counter_evidence")
        uuid.UUID(ln["claim_ledger_entry_id"])

    vrs = body["verification_records"]
    assert len(vrs) >= 1
    cve_lite = [vr for vr in vrs if vr["check_kind"] == "cve_lite"]
    assert cve_lite, "CVE-lite must have produced at least one verification record"
    for vr in cve_lite:
        assert vr["check_name"] == "quote_hash_and_substring_v1"
        assert vr["evaluator_id"] == "mvp0_cve_lite_v1"
        assert vr["outcome"] in ("pass", "fail", "inconclusive")
        assert vr["claim_logical_id"] == lc_id


def test_claim_evidence_404_on_unknown_logical_claim(client):
    bogus = uuid.uuid4()
    r = client.get(f"/api/v1/claims/{bogus}/evidence")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# pagination guardrails
# ---------------------------------------------------------------------------
def test_raw_claims_limit_bounds(client):
    _pid, tid = _setup_task_with_doc(client)
    r = client.get(f"/api/v1/tasks/{tid}/raw-claims?limit=1")
    assert r.status_code == 200
    items = r.json()["items"]
    # Even when there are more rows, limit=1 must clip the response.
    assert len(items) <= 1