"""API tests for the Anti-Hallucination Report endpoint (Phase 8.8B-REPORT-CODE-A).

Endpoint exercised:

  GET /api/v1/tasks/{task_id}/anti-hallucination-report

Coverage map (5 mandatory scenarios + 1 optional ordering scenario):

  1. test_get_anti_hallucination_report_returns_404_for_missing_task
  2. test_get_anti_hallucination_report_returns_not_ready_for_empty_task
  3. test_get_anti_hallucination_report_includes_gate_gaps_and_publication_held
  4. test_get_anti_hallucination_report_distinguishes_withdrawn_and_superseded
  5. test_get_anti_hallucination_report_is_read_only
  6. test_get_anti_hallucination_report_orders_coverage_gaps_severity_first
     (optional, included because it is simple to express)

Design notes:
  - This file lives under apps/api/tests/. The Python package ``app``
    therefore resolves to apps/api/app, so ``from app.main import app``
    and ``from app.db import get_engine`` are the canonical imports.
  - We do NOT touch Redis: the endpoint is strictly read-only and does
    not call ``get_redis()``. No FakeRedis is needed.
  - We do NOT import any worker code. All rows are seeded directly via
    SQL — exactly the same pattern used by
    ``test_answers_endpoints.py`` and
    ``test_claim_entailment_read_endpoint.py``.
  - Helpers are LOCAL to this file (per the block prompt: no imports
    from other test files).
  - Append-only tables accept INSERT — the shared
    ``reject_modify_append_only`` trigger only blocks UPDATE / DELETE.
  - All identifiers / hashes / idempotency keys are unique per
    invocation (rerun-safe).
"""
from __future__ import annotations

import hashlib
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

    Mirrors the gating used by every other API test module.
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

    Envelope shape::

        {"error": {"code": "...", "message": "...", "details": {...}, ...}}
    """
    err = resp_json.get("error")
    assert err is not None, f"missing 'error' envelope in response: {resp_json}"
    assert isinstance(err, dict), f"'error' is not a dict: {err!r}"
    return err


def _endpoint(task_id: uuid.UUID) -> str:
    return f"/api/v1/tasks/{task_id}/anti-hallucination-report"


# ---------------------------------------------------------------------------
# DB seeding helpers — tenant / project / task
# ---------------------------------------------------------------------------
def _seed_tenant_user(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """Ensure the (Dev, dev@local) tenant + user exist."""
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


def _seed_project_and_task(
    conn: Connection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a fresh project and a task in status='created'."""
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
                {"t": tenant_id, "n": f"ahr-test-{uuid.uuid4()}"},
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


# ---------------------------------------------------------------------------
# DB seeding helpers — draft / gate / coverage gaps / published
# ---------------------------------------------------------------------------
def _seed_draft(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    summary_text: str = "",
    compiler_name: str = "mvp0_compiler_v1",
    compiler_version: str = "0.1.0",
) -> uuid.UUID:
    """Insert one draft_final_answers v1 for the task."""
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO draft_final_answers
                        (id, task_id, version_no,
                         compiler_name, compiler_version, summary_text)
                    VALUES (:id, :t, 1,
                            :cn, :cv, :st)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "cn": compiler_name,
                    "cv": compiler_version,
                    "st": summary_text,
                },
            ).first()[0]
        )
    )


def _seed_gate_report(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    decision: str,
    reason_code: str,
) -> uuid.UUID:
    """Insert one final_gate_reports row for the draft."""
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


def _seed_coverage_gap(
    conn: Connection,
    *,
    draft_id: uuid.UUID,
    kind: str,
    severity: str,
    gap_key: str,
    details_json: str = '{"reason":"seeded for test"}',
    created_at_sql: str | None = None,
) -> uuid.UUID:
    """Insert one coverage_gap_statements row.

    SECURITY note on ``created_at_sql``:
      This argument is interpolated VERBATIM into the SQL string. It
      exists ONLY so the ordering test can produce rows with
      controlled, distinct ``created_at`` values (e.g.
      ``NOW() - interval '1 hour'``). All callers in this file pass
      TRUSTED constant strings; no test-user input reaches this
      argument.
    """
    if created_at_sql is None:
        sql = text(
            """
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key, details)
            VALUES (:id, :d, :k, :sev, :gk, CAST(:dt AS JSONB))
            RETURNING id
            """
        )
    else:
        sql = text(
            f"""
            INSERT INTO coverage_gap_statements
                (id, draft_final_answer_id, kind, severity, gap_key,
                 details, created_at)
            VALUES (:id, :d, :k, :sev, :gk, CAST(:dt AS JSONB),
                    {created_at_sql})
            RETURNING id
            """
        )
    return uuid.UUID(
        str(
            conn.execute(
                sql,
                {
                    "id": uuid.uuid4(),
                    "d": draft_id,
                    "k": kind,
                    "sev": severity,
                    "gk": gap_key,
                    "dt": details_json,
                },
            ).first()[0]
        )
    )


def _seed_published_answer(
    conn: Connection,
    *,
    task_id: uuid.UUID,
    draft_id: uuid.UUID,
    gate_report_id: uuid.UUID,
    summary_text: str,
    status: str = "published",
) -> uuid.UUID:
    """Insert one published_answers v1 row."""
    content_hash = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
    return uuid.UUID(
        str(
            conn.execute(
                text(
                    """
                    INSERT INTO published_answers
                        (id, task_id, draft_final_answer_id,
                         final_gate_report_id,
                         version_no, content_hash, status)
                    VALUES (:id, :t, :d, :g, 1, :h, :st)
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": task_id,
                    "d": draft_id,
                    "g": gate_report_id,
                    "h": content_hash,
                    "st": status,
                },
            ).first()[0]
        )
    )


# ---------------------------------------------------------------------------
# DB inspection helpers (read-only test)
# ---------------------------------------------------------------------------
_COUNTABLE_TABLES = frozenset(
    {
        "audit_records",
        "claim_ledger_entries",
        "source_quality_assessments",
        "claim_entailment_checks",
        "final_gate_reports",
        "coverage_gap_statements",
        "published_answers",
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
def test_get_anti_hallucination_report_returns_404_for_missing_task() -> None:
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
# 2 — Task exists but empty: not_ready, claims/evidence empty, zero counters
# ===========================================================================
def test_get_anti_hallucination_report_returns_not_ready_for_empty_task() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level identity.
    assert body["task_id"] == str(task_id)
    assert body["project_id"] == str(project_id)
    assert body["tenant_id"] == str(tenant_id)

    # task metadata.
    assert body["task"]["status"] == "created"
    assert body["task"]["mode"] == "closed_corpus"

    # No draft -> no gate -> no published -> "not_ready".
    assert body["publication"]["status"] == "not_ready"
    assert body["publication"]["published_answer_id"] is None
    assert body["publication"]["published_answer_status"] is None
    assert body["publication"]["final_gate_report_id"] is None

    assert body["gate"]["decision"] is None
    assert body["gate"]["reason_code"] is None
    assert body["gate"]["payload"] == {}
    assert body["gate"]["coverage_gaps"] == []

    # CODE-A: claims/evidence empty by design.
    assert body["claims"] == []
    assert body["evidence"] == []

    # axis_summary: final_gate derived from gaps (zero), others zeroed.
    fg = body["axis_summary"]["final_gate"]
    assert fg["has_blocking_gaps"] is False
    assert fg["has_warnings"] is False
    assert fg["blocking_gap_count"] == 0
    assert fg["warning_gap_count"] == 0

    cve = body["axis_summary"]["cve_lite"]
    assert cve == {
        "verified_claims_count": 0,
        "unverified_claims_count": 0,
        "inconclusive_count": 0,
    }
    sq = body["axis_summary"]["source_quality"]
    assert sq == {
        "strong_count": 0,
        "adequate_count": 0,
        "weak_count": 0,
        "unsuitable_count": 0,
        "unknown_count": 0,
        "missing_count": 0,
    }
    ce = body["axis_summary"]["claim_entailment"]
    assert ce == {
        "entailed_count": 0,
        "partially_supported_count": 0,
        "not_supported_count": 0,
        "contradicted_count": 0,
        "uncertain_count": 0,
        "missing_count": 0,
    }

    # mock_indicators always present.
    mi = body["mock_indicators"]
    assert mi["uses_mock_source_quality"] is True
    assert mi["uses_mock_claim_entailment"] is True
    assert mi["uses_mock_compiler"] is True  # fallback when no draft
    assert mi["uses_mock_cve_lite"] is True
    assert isinstance(mi["notes"], list) and len(mi["notes"]) >= 4

    # limitations always present.
    assert isinstance(body["limitations"], list)
    assert len(body["limitations"]) >= 4


# ===========================================================================
# 3 — Gate rejected with gaps + no published -> publication_held
# ===========================================================================
def test_get_anti_hallucination_report_includes_gate_gaps_and_publication_held() -> None:
    _skip_if_db_unreachable()

    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        draft_id = _seed_draft(conn, task_id=task_id, summary_text="")
        gate_id = _seed_gate_report(
            conn,
            task_id=task_id,
            draft_id=draft_id,
            decision="rejected",
            reason_code="no_verified_claims",
        )
        # Two gaps so we can count both blocking and warning buckets.
        gap_block_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
        )
        gap_warn_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="source_quality_warning",
            severity="warn",
            gap_key=f"span:{uuid.uuid4()}:source_quality_warning",
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # publication_held: no published_answers + gate rejected.
    assert body["publication"]["status"] == "publication_held"
    assert body["publication"]["published_answer_id"] is None

    # gate echoes the rejected decision and the seeded reason_code.
    assert body["gate"]["decision"] == "rejected"
    assert body["gate"]["reason_code"] == "no_verified_claims"

    gaps = body["gate"]["coverage_gaps"]
    assert isinstance(gaps, list) and len(gaps) == 2

    ids_by_kind = {g["kind"]: g["id"] for g in gaps}
    assert ids_by_kind["missing_evidence"] == str(gap_block_id)
    assert ids_by_kind["source_quality_warning"] == str(gap_warn_id)

    # axis decoration is applied per gap.
    axis_by_kind = {g["kind"]: g["axis"] for g in gaps}
    assert axis_by_kind["missing_evidence"] == "coverage"
    assert axis_by_kind["source_quality_warning"] == "source_quality"

    # Severity-first ordering: block before warn.
    assert gaps[0]["severity"] == "block"
    assert gaps[1]["severity"] == "warn"

    fg = body["axis_summary"]["final_gate"]
    assert fg["blocking_gap_count"] == 1
    assert fg["warning_gap_count"] == 1
    assert fg["has_blocking_gaps"] is True
    assert fg["has_warnings"] is True

    # final_gate_report_id is referenced in publication only when a
    # published_answer exists (it points to the published's
    # final_gate_report_id). With no published_answer, it is None.
    assert body["publication"]["final_gate_report_id"] is None

    # mock_indicators: draft uses the mock compiler -> True.
    assert body["mock_indicators"]["uses_mock_compiler"] is True


# ===========================================================================
# 4 — published with status='withdrawn' / 'superseded' are NOT flattened
# ===========================================================================
def _seed_approved_published_with_status(status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed approved draft + gate + published_answer with the given status.

    The DB CHECK on ``published_answers.status`` accepts 'published',
    'withdrawn', 'superseded'. We seed each row directly without
    routing through the lifecycle service, since the test only
    exercises the report's derivation logic.

    Returns (task_id, published_answer_id).
    """
    summary_text = f"smoke-{uuid.uuid4()}"
    engine = get_engine()
    with engine.begin() as conn:
        tenant_id, user_id = _seed_tenant_user(conn)
        _project_id, task_id = _seed_project_and_task(
            conn, tenant_id=tenant_id, user_id=user_id
        )
        draft_id = _seed_draft(
            conn, task_id=task_id, summary_text=summary_text
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
            status=status,
        )
    return task_id, pa_id


def test_get_anti_hallucination_report_distinguishes_withdrawn_and_superseded() -> None:
    _skip_if_db_unreachable()

    client = TestClient(app)

    # withdrawn case.
    task_w, pa_w = _seed_approved_published_with_status("withdrawn")
    resp_w = client.get(_endpoint(task_w))
    assert resp_w.status_code == 200, resp_w.text
    body_w = resp_w.json()
    assert body_w["publication"]["status"] == "withdrawn"
    assert body_w["publication"]["published_answer_status"] == "withdrawn"
    assert body_w["publication"]["published_answer_id"] == str(pa_w)
    # Crucially: NOT flattened to "published".
    assert body_w["publication"]["status"] != "published"

    # superseded case.
    task_s, pa_s = _seed_approved_published_with_status("superseded")
    resp_s = client.get(_endpoint(task_s))
    assert resp_s.status_code == 200, resp_s.text
    body_s = resp_s.json()
    assert body_s["publication"]["status"] == "superseded"
    assert body_s["publication"]["published_answer_status"] == "superseded"
    assert body_s["publication"]["published_answer_id"] == str(pa_s)
    assert body_s["publication"]["status"] != "published"


# ===========================================================================
# 5 — read-only: count snapshot invariant
# ===========================================================================
def test_get_anti_hallucination_report_is_read_only() -> None:
    """The endpoint MUST NOT mutate any DB row. Snapshot counts on all
    append-only / report-relevant tables AFTER seeding, hit the
    endpoint several times (happy path + 404 + repeated call), assert
    the snapshot is identical.
    """
    _skip_if_db_unreachable()

    # Seed a task with a draft + rejected gate + one block gap so the
    # endpoint exercises the heaviest read path it has in CODE-A.
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
        _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
        )

    # Snapshot AFTER all seed transactions commit.
    with engine.connect() as conn:
        before = _snapshot_all_counts(conn)

    client = TestClient(app)

    # Happy path.
    r_ok = client.get(_endpoint(task_id))
    assert r_ok.status_code == 200, r_ok.text

    # 404 path: must also be free of side effects.
    r_404 = client.get(_endpoint(uuid.uuid4()))
    assert r_404.status_code == 404, r_404.text

    # Repeat the happy path to make sure the second GET does not
    # produce idempotent inserts behind the scenes.
    r_ok2 = client.get(_endpoint(task_id))
    assert r_ok2.status_code == 200, r_ok2.text

    with engine.connect() as conn:
        after = _snapshot_all_counts(conn)

    assert after == before, (
        "row counts drifted after read-only GETs; "
        f"before={before!r}, after={after!r}"
    )


# ===========================================================================
# 6 — optional: severity-first ordering of coverage_gaps
# ===========================================================================
def test_get_anti_hallucination_report_orders_coverage_gaps_severity_first() -> None:
    """Insert three gaps in a non-severity-first creation order
    (warn, block, then a second warn with an EARLIER created_at) and
    verify the endpoint returns them ordered:
      - block first;
      - then warn rows, in created_at ASC, then id ASC.

    We only seed severities 'block' and 'warn' because the CHECK
    constraint accepts {info, warn, block}; 'info' is not produced
    by today's gate and is not required by the block prompt to be
    exercised.
    """
    _skip_if_db_unreachable()

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

        # Seeded in a deliberately non-severity-first creation order.
        # created_at values are explicitly set via trusted SQL
        # constants (see ``_seed_coverage_gap``'s SECURITY note).
        warn_late_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="source_quality_warning",
            severity="warn",
            gap_key=f"span:{uuid.uuid4()}:source_quality_warning",
            created_at_sql="NOW() - interval '30 minutes'",
        )
        block_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="missing_evidence",
            severity="block",
            gap_key="no_verified_claims",
            created_at_sql="NOW() - interval '15 minutes'",
        )
        warn_early_id = _seed_coverage_gap(
            conn,
            draft_id=draft_id,
            kind="entailment_warning",
            severity="warn",
            gap_key=f"span:{uuid.uuid4()}:entailment_warning",
            # earlier than warn_late_id so it should sort first among
            # the warn rows after the block.
            created_at_sql="NOW() - interval '2 hours'",
        )

    client = TestClient(app)
    resp = client.get(_endpoint(task_id))
    assert resp.status_code == 200, resp.text
    gaps = resp.json()["gate"]["coverage_gaps"]
    assert len(gaps) == 3

    # block first.
    assert gaps[0]["severity"] == "block"
    assert gaps[0]["id"] == str(block_id)

    # then warn rows in created_at ASC order: warn_early (-2h) before
    # warn_late (-30m).
    assert gaps[1]["severity"] == "warn"
    assert gaps[2]["severity"] == "warn"
    assert gaps[1]["id"] == str(warn_early_id)
    assert gaps[2]["id"] == str(warn_late_id)
