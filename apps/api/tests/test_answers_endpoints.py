"""API e2e tests for Phase 8.4 read-only answers endpoints.

Endpoints exercised:
  GET /api/v1/tasks/{task_id}/draft
  GET /api/v1/tasks/{task_id}/final-gate-report
  GET /api/v1/tasks/{task_id}/published-answer
  GET /api/v1/published-answers/{published_answer_id}

Design notes:
  - This test file lives under apps/api/tests/. Inside that test run, the
    Python package `app` resolves to the API app (apps/api/app), NOT the
    worker. We therefore MUST NOT import anything from the worker package
    here, otherwise the resolution `app.consumers.task_created` would point
    at apps/api/app/consumers/task_created.py and fail (or, worse, succeed
    against an unrelated module). To stay self-contained, this module:
      * imports only `from app.main import app` and `from app.db import get_engine`;
      * seeds directly into the database the minimal rows needed for each
        scenario (no consumer, no Redis, no compiler, no gate);
      * exercises the API via fastapi.testclient.TestClient.

  - Normalized error envelope (verified against the repo):
        {"error": {"code": "RESOURCE_NOT_FOUND", "message": "...",
                   "details": {"resource": "...", "id": "..."}, ...}}
    All assertions read fields from `body["error"]`, never from the top-level
    body.

  - ErrorCode.NOT_PUBLISHED does not exist in MVP-0. The /published-answer
    endpoint returns RESOURCE_NOT_FOUND with details.resource='published_answers'
    when the task exists but has not been published.

  - All tests are rerun-safe (UUID/hash/marker unique per invocation).
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db import get_engine
from app.main import app


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _err(resp_json: dict[str, Any]) -> dict[str, Any]:
    """Extract the normalized error envelope from a NormalizedError response.

    Envelope shape: {"error": {"code": "...", "details": {...}, ...}}.
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


# ---------------------------------------------------------------------------
# DB seeding helpers (no consumer, no Redis, no worker modules)
# ---------------------------------------------------------------------------
def _seed_tenant_user(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure the (Dev, dev@local) tenant + user exist. Returns (tenant_id, user_id)."""
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
            text("SELECT id FROM users WHERE tenant_id = :t AND email = 'dev@local'"),
            {"t": tenant_id},
        ).one()
    user_id = uuid.UUID(str(row[0]))
    return tenant_id, user_id


def _seed_project_and_task(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a fresh project and a task in status='created'. Returns (project_id, task_id)."""
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
                {"t": tenant_id, "n": f"answers-endpoints-{uuid.uuid4()}"},
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
    return project_id, task_id


def _seed_logical_claim_and_verified_ledger(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one logical_claim plus a verified_fact ledger entry (version_no=1).

    For Phase 8.4 the gate considers "latest" the row with the greatest
    version_no per logical_claim. Inserting a single v1='verified_fact' row is
    sufficient to make the gate accept a span pointing at it as verified-backed.

    Returns (claim_logical_id, claim_ledger_entry_id).
    """
    lc_id = uuid.UUID(
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
                    "ch": _unique_hash(),
                },
            ).first()[0]
        )
    )

    entry_id = uuid.UUID(
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
                {"id": uuid.uuid4(), "lc": lc_id},
            ).first()[0]
        )
    )
    return lc_id, entry_id


def _seed_draft(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    summary_text: str,
) -> uuid.UUID:
    return uuid.UUID(
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


def _seed_span(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    span_index: int,
    span_text: str,
) -> uuid.UUID:
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_answer_spans
                        (id, draft_final_answer_id, span_index,
                         char_start, char_end, span_text, span_hash)
                    VALUES (:id, :d, :idx,
                            0, :ce, :st, :sh)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "d": draft_id,
                    "idx": span_index,
                    "ce": len(span_text),
                    "st": span_text,
                    "sh": hashlib.sha256(span_text.encode("utf-8")).hexdigest(),
                },
            ).first()[0]
        )
    )


def _seed_span_claim_link(
    conn: Connection,
    *,
    span_id: uuid.UUID,
    entry_id: uuid.UUID,
    claim_logical_id: uuid.UUID,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO final_answer_span_claim_links
                (id, final_answer_span_id, claim_ledger_entry_id,
                 claim_logical_id, link_role)
            VALUES (:id, :sp, :ent, :lc, 'primary_support')
            """
        ),
        {
            "id": uuid.uuid4(),
            "sp": span_id,
            "ent": entry_id,
            "lc": claim_logical_id,
        },
    )


def _seed_gate_report(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    decision: str,
    reason_code: str,
) -> uuid.UUID:
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO final_gate_reports
                        (id, task_id, draft_final_answer_id,
                         decision, reason_code)
                    VALUES (:id, :t, :d, :dec, :rc)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "d": draft_id,
                    "dec": decision,
                    "rc": reason_code,
                },
            ).first()[0]
        )
    )


def _seed_coverage_gap_no_verified_claims(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key, details)
            VALUES (:id, :d, 'missing_evidence', 'block', 'no_verified_claims',
                    CAST('{"reason":"no verified claims to publish"}' AS JSONB))
            """
        ),
        {"id": uuid.uuid4(), "d": draft_id},
    )


def _seed_published_answer(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    gate_report_id: uuid.UUID,
    summary_text: str,
) -> uuid.UUID:
    content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
    return uuid.UUID(
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
                    "g": gate_report_id,
                    "h": content_hash,
                },
            ).first()[0]
        )
    )


def _set_task_status(conn: Connection, *, task_id: uuid.UUID, status: str) -> None:
    conn.execute(
        text("UPDATE task_masters SET status = :s WHERE id = :id"),
        {"s": status, "id": task_id},
    )


# ---------------------------------------------------------------------------
# scenario seeders
# ---------------------------------------------------------------------------
def _seed_approved_scenario() -> tuple[uuid.UUID, str, uuid.UUID]:
    """Seed a fully approved task: task='published', draft v1 with 1 span, link
    to a verified_fact ledger entry, gate report approved, published_answers v1.

    Returns (task_id, summary_text, published_answer_id).
    """
    summary_text = f"verified-claim-{uuid.uuid4()}\n"
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )

        lc_id, entry_id = _seed_logical_claim_and_verified_ledger(
            conn, tenant_id=tenant_id, project_id=project_id, task_id=task_id
        )

        draft_id = _seed_draft(conn, task_id=task_id, summary_text=summary_text)
        span_id = _seed_span(
            conn, draft_id=draft_id, span_index=0, span_text=summary_text
        )
        _seed_span_claim_link(
            conn, span_id=span_id, entry_id=entry_id, claim_logical_id=lc_id
        )

        gate_id = _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="approved",
            reason_code="all_spans_verified",
        )
        pa_id = _seed_published_answer(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            gate_report_id=gate_id,
            summary_text=summary_text,
        )

        _set_task_status(conn, task_id=task_id, status="published")

    return task_id, summary_text, pa_id


def _seed_rejected_zero_verified_scenario() -> uuid.UUID:
    """Seed a rejected zero-verified task: task='analyzed_partial', draft v1
    with summary_text='' and zero spans, gate report rejected/no_verified_claims,
    one coverage_gap_statements (missing_evidence/block/no_verified_claims),
    no published_answers.

    Returns task_id.
    """
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )

        draft_id = _seed_draft(conn, task_id=task_id, summary_text="")
        _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="rejected",
            reason_code="no_verified_claims",
        )
        _seed_coverage_gap_no_verified_claims(conn, draft_id=draft_id)

        # Mirror the consumer's rejected outcome: task is brought back to
        # 'analyzed_partial' even though we are not running the consumer here.
        _set_task_status(conn, task_id=task_id, status="analyzed_partial")

    return task_id


def _seed_task_only() -> uuid.UUID:
    """Seed a task in status='created' with no draft / gate / published rows."""
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
    return task_id


# ---------------------------------------------------------------------------
# Test 1 — approved end-to-end: all four endpoints return 200
# ---------------------------------------------------------------------------
def test_approved_endpoints_return_200_with_expected_payloads():
    task_id, summary_text, pa_id = _seed_approved_scenario()
    client = TestClient(app)

    # /draft
    resp = client.get(f"/api/v1/tasks/{task_id}/draft")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "draft" in body and "spans" in body
    assert body["draft"]["task_id"] == str(task_id)
    assert body["draft"]["version_no"] == 1
    assert body["draft"]["summary_text"] == summary_text
    assert isinstance(body["spans"], list) and len(body["spans"]) == 1

    # /final-gate-report
    resp = client.get(f"/api/v1/tasks/{task_id}/final-gate-report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["decision"] == "approved"
    assert body["reason_code"] == "all_spans_verified"
    assert body["coverage_gap_statements"] == []

    # /published-answer
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["version_no"] == 1
    assert body["status"] == "published"
    assert body["id"] == str(pa_id)
    expected_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
    assert body["content_hash"] == expected_hash

    # /published-answers/{id}
    resp = client.get(f"/api/v1/published-answers/{pa_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(pa_id)
    assert body["task_id"] == str(task_id)
    assert body["status"] == "published"


# ---------------------------------------------------------------------------
# Test 2 — rejected zero-verified: draft + gate report visible, published 404
# ---------------------------------------------------------------------------
def test_rejected_zero_verified_endpoints_show_gap_and_published_is_404():
    task_id = _seed_rejected_zero_verified_scenario()
    client = TestClient(app)

    # /draft: present, summary_text='', zero spans.
    resp = client.get(f"/api/v1/tasks/{task_id}/draft")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["draft"]["task_id"] == str(task_id)
    assert body["draft"]["summary_text"] == ""
    assert body["spans"] == []

    # /final-gate-report: rejected with missing_evidence/no_verified_claims gap.
    resp = client.get(f"/api/v1/tasks/{task_id}/final-gate-report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == str(task_id)
    assert body["decision"] == "rejected"
    assert body["reason_code"] == "no_verified_claims"
    gaps = body["coverage_gap_statements"]
    assert isinstance(gaps, list) and len(gaps) >= 1
    matched = [
        g for g in gaps
        if g["kind"] == "missing_evidence"
        and g["gap_key"] == "no_verified_claims"
    ]
    assert matched, f"expected at least one missing_evidence/no_verified_claims gap, got {gaps}"
    assert matched[0]["severity"] == "block"

    # /published-answer: 404 RESOURCE_NOT_FOUND, details.resource='published_answers'.
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 404, resp.text
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "published_answers"


# ---------------------------------------------------------------------------
# Test 3 — 404 for unknown task on every task-scoped endpoint
# ---------------------------------------------------------------------------
def test_endpoints_return_404_resource_not_found_for_unknown_task():
    client = TestClient(app)
    unknown_task = uuid.uuid4()

    for path in (
        f"/api/v1/tasks/{unknown_task}/draft",
        f"/api/v1/tasks/{unknown_task}/final-gate-report",
        f"/api/v1/tasks/{unknown_task}/published-answer",
    ):
        resp = client.get(path)
        assert resp.status_code == 404, path
        err = _err(resp.json())
        assert err["code"] == "RESOURCE_NOT_FOUND"
        details = err.get("details") or {}
        assert details.get("resource") == "task_masters"
        assert details.get("id") == str(unknown_task)


# ---------------------------------------------------------------------------
# Test 4 — 404 for sub-resource missing on an existing task without artifacts
# ---------------------------------------------------------------------------
def test_endpoints_return_404_for_existing_task_without_pipeline_artifacts():
    task_id = _seed_task_only()
    client = TestClient(app)

    # /draft: task exists, draft does not.
    resp = client.get(f"/api/v1/tasks/{task_id}/draft")
    assert resp.status_code == 404
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    assert (err.get("details") or {}).get("resource") == "draft_final_answers"

    # /final-gate-report: task exists, gate report does not.
    resp = client.get(f"/api/v1/tasks/{task_id}/final-gate-report")
    assert resp.status_code == 404
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    assert (err.get("details") or {}).get("resource") == "final_gate_reports"

    # /published-answer: task exists, never published.
    resp = client.get(f"/api/v1/tasks/{task_id}/published-answer")
    assert resp.status_code == 404
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    assert (err.get("details") or {}).get("resource") == "published_answers"


# ---------------------------------------------------------------------------
# Test 5 — 404 for unknown published_answer id
# ---------------------------------------------------------------------------
def test_get_published_answer_by_id_returns_404_for_unknown_id():
    client = TestClient(app)
    unknown_id = uuid.uuid4()

    resp = client.get(f"/api/v1/published-answers/{unknown_id}")
    assert resp.status_code == 404
    err = _err(resp.json())
    assert err["code"] == "RESOURCE_NOT_FOUND"
    details = err.get("details") or {}
    assert details.get("resource") == "published_answers"
    assert details.get("id") == str(unknown_id)
