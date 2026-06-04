"""Unit + DB-backed tests for
apps/worker/app/services/orchestration_source_resolution.py
(Phase ORCH-MULTI-B1 pure logic + ORCH-MULTI-B2 DB-backed pass).

The pure tests exercise ONLY the deterministic pure-logic slice: the
request/result contracts and the pure functions. They need NO database, NO
DATABASE_URL, NO Redis, NO FastAPI, NO network and invoke NO provider.

The DB-backed tests (ORCH-MULTI-B2) exercise ``run_source_resolution_pass``
against a real PostgreSQL reached through DATABASE_URL. They follow the pattern
of apps/worker/tests/test_orchestration_runner_service.py: migrations applied
once per session, a caller-owned connection wrapped in a transaction rolled
back at teardown, and a clean skip when DATABASE_URL is unset/unreachable. They
use NO API, NO Redis, NO FastAPI, NO network, NO real provider, and seed the
schema directly so the resolution pass is the only thing under test.

Package ``app`` resolves to apps/worker/app, so the service module is
importable directly with PYTHONPATH=apps/worker.

Pure coverage map:
   1. derive state without resolution -> proposed
   2. derive state resolved -> resolved
   3. derive state insufficient_metadata -> insufficient_metadata
   4. derive state unreachable/failed/not_found/partial/unknown -> resolution_failed
   5. classify external http/https URL -> unreachable, never resolved
   6. classify empty metadata -> insufficient_metadata
   7. classify uploaded/internal local marker -> resolved w/ correct target kind
   8. stable sort key deterministic
   9. candidate-scoped idempotency key includes base key, kind, candidate id
  10. counters increment correctly
  11. dataclasses preserve publication_status not_evaluated and gate_report_id None
  + run_source_resolution_pass on an invalid request returns failed w/o DB

DB-backed coverage map (ORCH-MULTI-B2):
   1. test_db_pass_creates_source_resolutions_for_candidates
   2. test_db_pass_does_not_mutate_source_candidates
   3. test_db_pass_idempotent_replay_does_not_duplicate
   4. test_db_pass_emits_only_resolution_events
   5. test_db_pass_does_not_create_verifications_evidence_gate_or_publication
   6. test_db_external_url_is_unreachable_not_resolved
   7. test_db_insufficient_metadata_candidate
   8. test_db_bounded_max_candidates
   9. test_db_tenant_isolation
  10. test_db_per_agent_output_scope
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text

from app.services.orchestration_source_resolution import (
    CandidateResolutionDecision,
    SourceResolutionPassRequest,
    SourceResolutionPassResult,
    MAX_CANDIDATES_DEFAULT,
    PUBLICATION_STATUS_NOT_EVALUATED,
    RESOLUTION_OUTCOME_FAILED,
    RESOLUTION_OUTCOME_INSUFFICIENT_METADATA,
    RESOLUTION_OUTCOME_NOT_FOUND,
    RESOLUTION_OUTCOME_PARTIAL,
    RESOLUTION_OUTCOME_RESOLVED,
    RESOLUTION_OUTCOME_UNREACHABLE,
    RESOLUTION_OUTCOME_VALUES,
    RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT,
    RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT,
    RESOLUTION_TARGET_KIND_URL,
    SELECTION_SCOPE_PER_AGENT_OUTPUT,
    SOURCE_CANDIDATE_STATUS_INSUFFICIENT_METADATA,
    SOURCE_CANDIDATE_STATUS_PROPOSED,
    SOURCE_CANDIDATE_STATUS_RESOLUTION_FAILED,
    SOURCE_CANDIDATE_STATUS_RESOLVED,
    _build_candidate_scoped_idempotency_key,
    _build_initial_counters,
    _classify_candidate,
    _derive_candidate_state,
    _derive_current_candidate_state,
    _increment_counters_for_outcome,
    _stable_candidate_sort_key,
    run_source_resolution_pass,
)

# apps/worker/tests/<this file>  ->  parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


# ===========================================================================
# Pure tests (ORCH-MULTI-B1) — unchanged logic, no DB
# ===========================================================================
# 1-4) _derive_candidate_state
def test_derive_state_without_resolution_is_proposed():
    assert _derive_candidate_state(None) == SOURCE_CANDIDATE_STATUS_PROPOSED


def test_derive_state_resolved():
    latest = {"outcome": RESOLUTION_OUTCOME_RESOLVED}
    assert _derive_candidate_state(latest) == SOURCE_CANDIDATE_STATUS_RESOLVED


def test_derive_state_insufficient_metadata():
    latest = {"outcome": RESOLUTION_OUTCOME_INSUFFICIENT_METADATA}
    assert (
        _derive_candidate_state(latest)
        == SOURCE_CANDIDATE_STATUS_INSUFFICIENT_METADATA
    )


@pytest.mark.parametrize(
    "outcome",
    [
        RESOLUTION_OUTCOME_FAILED,
        RESOLUTION_OUTCOME_UNREACHABLE,
        RESOLUTION_OUTCOME_NOT_FOUND,
        RESOLUTION_OUTCOME_PARTIAL,
        "some_unexpected_outcome",
    ],
)
def test_derive_state_non_success_maps_to_resolution_failed(outcome):
    latest = {"outcome": outcome}
    assert (
        _derive_candidate_state(latest)
        == SOURCE_CANDIDATE_STATUS_RESOLUTION_FAILED
    )


# 4b) _derive_current_candidate_state honours the candidate's initial status
def test_derive_current_state_no_resolution_uses_candidate_status():
    # Initial status is rejected and no resolution exists -> stays rejected,
    # NOT silently promoted to proposed.
    assert (
        _derive_current_candidate_state({"status": "rejected"}, None)
        == "rejected"
    )


def test_derive_current_state_no_resolution_proposed_stays_proposed():
    assert (
        _derive_current_candidate_state(
            {"status": SOURCE_CANDIDATE_STATUS_PROPOSED}, None
        )
        == SOURCE_CANDIDATE_STATUS_PROPOSED
    )


def test_derive_current_state_missing_status_defaults_to_proposed():
    # No usable status on the row -> defensive fallback to proposed.
    assert (
        _derive_current_candidate_state({}, None)
        == SOURCE_CANDIDATE_STATUS_PROPOSED
    )
    assert (
        _derive_current_candidate_state({"status": None}, None)
        == SOURCE_CANDIDATE_STATUS_PROPOSED
    )


def test_derive_current_state_resolution_outcome_wins_over_initial_status():
    # A resolution exists: it is authoritative and overrides the initial status.
    candidate = {"status": "rejected"}
    latest = {"outcome": RESOLUTION_OUTCOME_RESOLVED}
    assert (
        _derive_current_candidate_state(candidate, latest)
        == SOURCE_CANDIDATE_STATUS_RESOLVED
    )


# 5) classify external URL -> unreachable, never resolved
@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/a",
        "https://example.test/b",
        "HTTPS://Example.test/UPPER",
        "  https://example.test/whitespace  ",
    ],
)
def test_classify_external_http_url_is_unreachable_never_resolved(url):
    decision = _classify_candidate({"url": url})
    assert isinstance(decision, CandidateResolutionDecision)
    assert decision.outcome == RESOLUTION_OUTCOME_UNREACHABLE
    assert decision.outcome != RESOLUTION_OUTCOME_RESOLVED
    assert decision.resolution_target_kind == RESOLUTION_TARGET_KIND_URL


# 6) classify empty metadata -> insufficient_metadata
def test_classify_empty_metadata_is_insufficient():
    decision = _classify_candidate({})
    assert decision.outcome == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA
    assert decision.resolution_target_kind == RESOLUTION_TARGET_KIND_URL
    assert decision.failure_reason  # bounded, non-empty reason


def test_classify_empty_url_and_payload_is_insufficient():
    decision = _classify_candidate(
        {"url": "", "raw_citation_payload": {}, "provenance": {}}
    )
    assert decision.outcome == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA


def test_classify_unsupported_non_http_locator_is_insufficient():
    decision = _classify_candidate({"url": "ftp://example.test/x"})
    assert decision.outcome == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA
    assert decision.outcome != RESOLUTION_OUTCOME_RESOLVED


# 7) classify local marker -> resolved with correct target kind
def test_classify_uploaded_document_marker_is_resolved():
    decision = _classify_candidate(
        {"raw_citation_payload": {"uploaded_document_id": "doc-123"}}
    )
    assert decision.outcome == RESOLUTION_OUTCOME_RESOLVED
    assert decision.resolution_target_kind == RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT
    assert decision.failure_reason is None


def test_classify_document_id_marker_is_resolved_uploaded():
    decision = _classify_candidate({"provenance": {"document_id": "doc-xyz"}})
    assert decision.outcome == RESOLUTION_OUTCOME_RESOLVED
    assert decision.resolution_target_kind == RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT


def test_classify_internal_document_marker_is_resolved_internal():
    decision = _classify_candidate(
        {"provenance": {"internal_document_id": "int-1"}}
    )
    assert decision.outcome == RESOLUTION_OUTCOME_RESOLVED
    assert decision.resolution_target_kind == RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT


def test_classify_local_marker_wins_over_external_url():
    # Even with an external URL present, an explicit local marker resolves
    # locally; the URL does not force an unreachable outcome.
    decision = _classify_candidate(
        {
            "url": "https://example.test/page",
            "raw_citation_payload": {"internal_document_id": "int-9"},
        }
    )
    assert decision.outcome == RESOLUTION_OUTCOME_RESOLVED
    assert decision.resolution_target_kind == RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT


def test_classify_empty_marker_value_is_not_a_marker():
    # An empty marker value must not masquerade as a real local marker.
    decision = _classify_candidate(
        {"url": "https://example.test/p", "raw_citation_payload": {"document_id": ""}}
    )
    assert decision.outcome == RESOLUTION_OUTCOME_UNREACHABLE


# 8) stable sort key deterministic
def test_stable_sort_key_is_all_strings_with_fallback():
    key = _stable_candidate_sort_key({"id": 5, "agent_output_id": None})
    assert key == ("", "", "5")
    assert all(isinstance(part, str) for part in key)


def test_stable_sort_key_orders_deterministically():
    candidates = [
        {"agent_output_id": "b", "created_at": "2024-01-02", "id": "2"},
        {"agent_output_id": "a", "created_at": "2024-01-03", "id": "9"},
        {"agent_output_id": "a", "created_at": "2024-01-01", "id": "1"},
        {"agent_output_id": "a", "created_at": "2024-01-01", "id": "0"},
    ]
    ordered = sorted(candidates, key=_stable_candidate_sort_key)
    assert [c["id"] for c in ordered] == ["0", "1", "9", "2"]
    # Sorting is stable and repeatable.
    assert sorted(candidates, key=_stable_candidate_sort_key) == ordered


# 9) candidate-scoped idempotency key
def test_candidate_scoped_idempotency_key_includes_all_parts():
    key = _build_candidate_scoped_idempotency_key(
        "pass-idem-1", "source_resolution_started", "cand-42"
    )
    assert "pass-idem-1" in key
    assert "source_resolution_started" in key
    assert "cand-42" in key


def test_candidate_scoped_idempotency_key_distinct_per_candidate_and_kind():
    started_a = _build_candidate_scoped_idempotency_key(
        "p", "source_resolution_started", "a"
    )
    completed_a = _build_candidate_scoped_idempotency_key(
        "p", "source_resolution_completed", "a"
    )
    started_b = _build_candidate_scoped_idempotency_key(
        "p", "source_resolution_started", "b"
    )
    assert len({started_a, completed_a, started_b}) == 3


# 10) counters increment correctly
def test_initial_counters_are_zeroed_with_expected_keys():
    counters = _build_initial_counters()
    assert counters == {
        "candidates_seen": 0,
        "candidates_attempted": 0,
        "resolved_count": 0,
        "failed_count": 0,
        "insufficient_metadata_count": 0,
        "skipped_count": 0,
    }


def test_counters_increment_for_each_outcome():
    counters = _build_initial_counters()
    _increment_counters_for_outcome(counters, RESOLUTION_OUTCOME_RESOLVED)
    _increment_counters_for_outcome(counters, RESOLUTION_OUTCOME_INSUFFICIENT_METADATA)
    _increment_counters_for_outcome(counters, RESOLUTION_OUTCOME_FAILED)
    _increment_counters_for_outcome(counters, RESOLUTION_OUTCOME_UNREACHABLE)
    _increment_counters_for_outcome(counters, RESOLUTION_OUTCOME_NOT_FOUND)
    _increment_counters_for_outcome(counters, RESOLUTION_OUTCOME_PARTIAL)

    assert counters["resolved_count"] == 1
    assert counters["insufficient_metadata_count"] == 1
    # failed + unreachable + not_found + partial all bucket into failed_count.
    assert counters["failed_count"] == 4
    # Loop-managed counters are untouched by outcome bucketing.
    assert counters["candidates_seen"] == 0
    assert counters["candidates_attempted"] == 0
    assert counters["skipped_count"] == 0


def test_counters_ignore_unknown_outcome():
    counters = _build_initial_counters()
    _increment_counters_for_outcome(counters, "totally_unknown")
    assert counters == _build_initial_counters()


# 11) dataclasses preserve the invariants
def test_request_defaults_and_is_frozen():
    request = SourceResolutionPassRequest(
        tenant_id="t",
        orchestration_run_id="run-1",
        idempotency_key="idem-1",
    )
    assert request.max_candidates == MAX_CANDIDATES_DEFAULT
    assert request.candidate_selection_scope == "per_run"
    assert request.agent_output_id is None
    assert request.eligible_states == (SOURCE_CANDIDATE_STATUS_PROPOSED,)
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.tenant_id = "other"  # type: ignore[misc]


def test_result_defaults_publication_not_evaluated_and_gate_none():
    result = SourceResolutionPassResult(
        status="succeeded",
        orchestration_run_id="run-1",
        source_resolution_ids=(),
        per_candidate_outcomes={},
        event_ids=(),
        counters=_build_initial_counters(),
    )
    assert result.publication_status == PUBLICATION_STATUS_NOT_EVALUATED
    assert result.gate_report_id is None


def test_result_is_frozen():
    result = SourceResolutionPassResult(
        status="succeeded",
        orchestration_run_id=None,
        source_resolution_ids=(),
        per_candidate_outcomes={},
        event_ids=(),
        counters=_build_initial_counters(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]


# A controlled validation failure returns failed and touches NO connection
# (conn=None proves the DB is never reached on the validation path).
def test_run_source_resolution_pass_invalid_request_returns_failed_without_db():
    request = SourceResolutionPassRequest(
        tenant_id="t",
        orchestration_run_id="run-1",
        idempotency_key="idem-1",
        max_candidates=0,  # invalid -> validation fails before any DB access
    )
    result = run_source_resolution_pass(conn=None, request=request)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert result.source_resolution_ids == ()
    assert result.event_ids == ()
    assert result.per_candidate_outcomes == {}
    assert result.publication_status == PUBLICATION_STATUS_NOT_EVALUATED
    assert result.gate_report_id is None


def test_run_source_resolution_pass_per_agent_output_without_id_returns_failed():
    request = SourceResolutionPassRequest(
        tenant_id="t",
        orchestration_run_id="run-1",
        idempotency_key="idem-1",
        candidate_selection_scope=SELECTION_SCOPE_PER_AGENT_OUTPUT,
        agent_output_id=None,  # required for this scope -> validation fails
    )
    result = run_source_resolution_pass(conn=None, request=request)  # type: ignore[arg-type]
    assert result.status == "failed"
    assert result.event_ids == ()


# ===========================================================================
# DB plumbing (ORCH-MULTI-B2) — mirrors test_orchestration_runner_service.py
# ===========================================================================
def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _psycopg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    return url


def _sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _ensure_migrations(dsn: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "migrate_module", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    with psycopg.connect(_psycopg_url(dsn)) as pg_conn:
        rc = module.cmd_apply(pg_conn, target=None, dry_run=False)
        assert rc == 0
        pg_conn.commit()


@pytest.fixture(scope="session")
def _migrated_engine():
    dsn = _database_url()
    if not dsn:
        pytest.skip("DATABASE_URL is not set; source resolution DB tests skipped")
    try:
        with psycopg.connect(_psycopg_url(dsn)) as probe:
            probe.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"database unreachable ({exc!r}); source resolution DB tests skipped"
        )

    _ensure_migrations(dsn)
    engine = create_engine(_sqlalchemy_url(dsn), future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def conn(_migrated_engine):
    """A connection wrapping each test in a transaction rolled back at teardown.

    The pass writes through this connection and does NOT commit; the test reads
    facts back inside the same transaction; the rollback at teardown leaves the
    database clean. The test owns the transaction, exactly the contract the
    pass expects.
    """
    connection = _migrated_engine.connect()
    trans = connection.begin()
    try:
        yield connection
    finally:
        trans.rollback()
        connection.close()


# ===========================================================================
# Direct seeding helpers (SQLAlchemy text; only what the pass needs as input)
# ===========================================================================
def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _count(conn, sql: str, params: dict) -> int:
    return int(conn.execute(text(sql), params).scalar() or 0)


def _events_for_run(conn, run_id: str) -> list[tuple[int, str]]:
    rows = conn.execute(
        text(
            "SELECT sequence_no, event_type FROM orchestration_events "
            "WHERE orchestration_run_id = :r ORDER BY sequence_no"
        ),
        {"r": run_id},
    ).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


def _seed_tenant(conn) -> str:
    slug = f"srp-{uuid.uuid4().hex[:18]}"
    tid = conn.execute(
        text(
            "INSERT INTO tenants (name, slug, status) "
            "VALUES ('SRP Test', :slug, 'active') RETURNING id"
        ),
        {"slug": slug},
    ).scalar()
    return str(tid)


def _seed_master_prompt(conn, tenant_id: str) -> str:
    mp = conn.execute(
        text(
            "INSERT INTO master_prompts (tenant_id, prompt_text, title, status) "
            "VALUES (:t, :txt, 't', 'draft') RETURNING id"
        ),
        {"t": tenant_id, "txt": f"prompt-{uuid.uuid4()}"},
    ).scalar()
    return str(mp)


def _seed_master_prompt_version(conn, master_prompt_id: str) -> str:
    mpv = conn.execute(
        text(
            "INSERT INTO master_prompt_versions "
            "(master_prompt_id, version_no, prompt_text, prompt_text_hash) "
            "VALUES (:mp, 1, :txt, :h) RETURNING id"
        ),
        {"mp": master_prompt_id, "txt": f"frozen {uuid.uuid4()}", "h": _unique_hash()},
    ).scalar()
    return str(mpv)


def _seed_run(conn, *, tenant_id: str, mpv_id: str, status: str = "completed") -> str:
    rid = conn.execute(
        text(
            "INSERT INTO orchestration_runs "
            "(tenant_id, master_prompt_version_id, mode, execution_mode, status, "
            " master_prompt_text_hash, idempotency_key, policy_name, policy_version) "
            "VALUES (:t, :mpv, 'multi_ai_orchestration', 'independent', :st, "
            " :h, :idem, 'srp-test', '0') RETURNING id"
        ),
        {
            "t": tenant_id,
            "mpv": mpv_id,
            "st": status,
            "h": _unique_hash(),
            "idem": f"run-idem-{uuid.uuid4()}",
        },
    ).scalar()
    return str(rid)


def _seed_minimal_run(conn) -> dict[str, str]:
    """Seed a fresh tenant + master prompt + version + run; return the ids."""
    tenant_id = _seed_tenant(conn)
    master_prompt_id = _seed_master_prompt(conn, tenant_id)
    mpv_id = _seed_master_prompt_version(conn, master_prompt_id)
    run_id = _seed_run(conn, tenant_id=tenant_id, mpv_id=mpv_id)
    return {
        "tenant_id": tenant_id,
        "master_prompt_id": master_prompt_id,
        "master_prompt_version_id": mpv_id,
        "run_id": run_id,
    }


def _seed_candidate(
    conn,
    *,
    tenant_id: str,
    run_id: str,
    agent_output_id: str | None = None,
    url: str | None = None,
    provenance: dict | None = None,
    raw: dict | None = None,
    candidate_type: str = "agent_cited",
    status: str = "proposed",
) -> str:
    cid = conn.execute(
        text(
            "INSERT INTO source_candidates "
            "(tenant_id, orchestration_run_id, agent_output_id, candidate_type, "
            " status, url, provenance, raw_citation_payload) "
            "VALUES (:t, :r, :ao, :ct, :st, :url, CAST(:prov AS JSONB), "
            " CAST(:raw AS JSONB)) RETURNING id"
        ),
        {
            "t": tenant_id,
            "r": run_id,
            "ao": agent_output_id,
            "ct": candidate_type,
            "st": status,
            "url": url,
            "prov": json.dumps(provenance or {}),
            "raw": json.dumps(raw or {}),
        },
    ).scalar()
    return str(cid)


def _seed_agent_output(conn, *, tenant_id: str, master_prompt_id: str, run_id: str) -> str:
    """Build the minimal chain needed for a real orchestration_agent_outputs id."""
    role_id = conn.execute(
        text(
            "INSERT INTO agent_role_prompts "
            "(tenant_id, name, role_category, system_prompt_text, "
            " task_prompt_text, version_no) "
            "VALUES (:t, :n, 'researcher', 's', 'k', 1) RETURNING id"
        ),
        {"t": tenant_id, "n": f"role-{uuid.uuid4()}"},
    ).scalar()
    cfg_id = conn.execute(
        text(
            "INSERT INTO agent_configs "
            "(tenant_id, master_prompt_id, agent_role_prompt_id, name, provider, model) "
            "VALUES (:t, :mp, :rp, :n, 'mock', 'mock-model') RETURNING id"
        ),
        {"t": tenant_id, "mp": master_prompt_id, "rp": role_id, "n": f"agent-{uuid.uuid4()}"},
    ).scalar()
    snap_id = conn.execute(
        text(
            "INSERT INTO agent_config_snapshots "
            "(orchestration_run_id, agent_config_id, snapshot_payload, "
            " agent_role_prompt_text_hash) "
            "VALUES (:r, :c, CAST('{}' AS JSONB), :h) RETURNING id"
        ),
        {"r": run_id, "c": cfg_id, "h": _unique_hash()},
    ).scalar()
    agent_run_id = conn.execute(
        text(
            "INSERT INTO orchestration_agent_runs "
            "(orchestration_run_id, agent_config_snapshot_id, status, attempt_no, is_mock) "
            "VALUES (:r, :s, 'succeeded', 1, TRUE) RETURNING id"
        ),
        {"r": run_id, "s": snap_id},
    ).scalar()
    output_id = conn.execute(
        text(
            "INSERT INTO orchestration_agent_outputs "
            "(agent_run_id, output_kind, sequence_no) "
            "VALUES (:a, 'mock_candidate_text', 0) RETURNING id"
        ),
        {"a": agent_run_id},
    ).scalar()
    return str(output_id)


def _pass_request(tenant_id: str, run_id: str, **overrides) -> SourceResolutionPassRequest:
    kwargs: dict = {
        "tenant_id": tenant_id,
        "orchestration_run_id": run_id,
        "idempotency_key": f"srp-idem-{uuid.uuid4()}",
    }
    kwargs.update(overrides)
    return SourceResolutionPassRequest(**kwargs)


# ===========================================================================
# DB-backed tests (ORCH-MULTI-B2)
# ===========================================================================
# 1) creates source_resolutions for candidates
def test_db_pass_creates_source_resolutions_for_candidates(conn):
    ids = _seed_minimal_run(conn)
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url="https://example.invalid/a",
    )
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        raw={"uploaded_document_id": "doc-1"},
    )

    request = _pass_request(ids["tenant_id"], ids["run_id"])
    result = run_source_resolution_pass(conn, request)

    assert result.status == "succeeded"
    assert result.orchestration_run_id == ids["run_id"]
    assert result.publication_status == "not_evaluated"
    assert result.gate_report_id is None

    sr_count = _count(
        conn,
        "SELECT count(*) FROM source_resolutions WHERE orchestration_run_id = :r",
        {"r": ids["run_id"]},
    )
    assert sr_count > 0
    assert sr_count == len(result.source_resolution_ids)
    assert result.counters["candidates_seen"] == 2
    assert result.counters["candidates_attempted"] == 2

    for outcome in result.per_candidate_outcomes.values():
        assert outcome in RESOLUTION_OUTCOME_VALUES

    # The persisted rows carry codomain outcome and target kind values.
    rows = conn.execute(
        text(
            "SELECT outcome, resolution_target_kind "
            "FROM source_resolutions WHERE orchestration_run_id = :r"
        ),
        {"r": ids["run_id"]},
    ).mappings().all()
    for row in rows:
        assert row["outcome"] in RESOLUTION_OUTCOME_VALUES


# 2) does not mutate source_candidates
def test_db_pass_does_not_mutate_source_candidates(conn):
    ids = _seed_minimal_run(conn)
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url="https://example.invalid/a",
    )
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        raw={"internal_document_id": "int-1"},
    )

    select_sql = (
        "SELECT id, status, url, provenance, raw_citation_payload, created_at "
        "FROM source_candidates WHERE orchestration_run_id = :r ORDER BY id"
    )
    before = [
        dict(r)
        for r in conn.execute(text(select_sql), {"r": ids["run_id"]}).mappings().all()
    ]

    run_source_resolution_pass(conn, _pass_request(ids["tenant_id"], ids["run_id"]))

    after = [
        dict(r)
        for r in conn.execute(text(select_sql), {"r": ids["run_id"]}).mappings().all()
    ]
    # No UPDATE/DELETE: the candidate rows are byte-for-byte unchanged, and no
    # new candidate row was created.
    assert after == before
    assert len(after) == 2


# 3) idempotent replay does not duplicate
def test_db_pass_idempotent_replay_does_not_duplicate(conn):
    ids = _seed_minimal_run(conn)
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url="https://example.invalid/a",
    )
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        raw={"uploaded_document_id": "doc-7"},
    )

    request = _pass_request(ids["tenant_id"], ids["run_id"])

    first = run_source_resolution_pass(conn, request)
    sr_after_first = _count(
        conn, "SELECT count(*) FROM source_resolutions WHERE orchestration_run_id = :r",
        {"r": ids["run_id"]},
    )
    ev_after_first = _count(
        conn, "SELECT count(*) FROM orchestration_events WHERE orchestration_run_id = :r",
        {"r": ids["run_id"]},
    )

    second = run_source_resolution_pass(conn, request)
    sr_after_second = _count(
        conn, "SELECT count(*) FROM source_resolutions WHERE orchestration_run_id = :r",
        {"r": ids["run_id"]},
    )
    ev_after_second = _count(
        conn, "SELECT count(*) FROM orchestration_events WHERE orchestration_run_id = :r",
        {"r": ids["run_id"]},
    )

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    # No duplication on the second pass.
    assert sr_after_second == sr_after_first
    assert ev_after_second == ev_after_first
    # The replay reconstructs the same persisted fact ids.
    assert set(second.source_resolution_ids) == set(first.source_resolution_ids)
    assert set(second.event_ids) == set(first.event_ids)
    assert second.per_candidate_outcomes == first.per_candidate_outcomes


# 4) emits only resolution events
def test_db_pass_emits_only_resolution_events(conn):
    ids = _seed_minimal_run(conn)
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url="https://example.invalid/a",
    )
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        raw={"document_id": "doc-9"},
    )

    run_source_resolution_pass(conn, _pass_request(ids["tenant_id"], ids["run_id"]))

    types = {et for _seq, et in _events_for_run(conn, ids["run_id"])}
    assert types  # the pass emitted at least one event
    assert types <= {"source_resolution_started", "source_resolution_completed"}
    # In particular it never emits a verification or any invented event type.
    assert "source_verification_completed" not in types


# 5) does not create verifications / evidence / gate / publication
def test_db_pass_does_not_create_verifications_evidence_gate_or_publication(conn):
    ids = _seed_minimal_run(conn)
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url="https://example.invalid/a",
    )
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        raw={"uploaded_document_id": "doc-2"},
    )

    before = {
        "source_verifications": _count(
            conn, "SELECT count(*) FROM source_verifications", {}
        ),
        "evidence_spans": _count(conn, "SELECT count(*) FROM evidence_spans", {}),
        "claim_evidence_links": _count(
            conn, "SELECT count(*) FROM claim_evidence_links", {}
        ),
        "final_gate_reports": _count(
            conn, "SELECT count(*) FROM final_gate_reports", {}
        ),
        "published_answers": _count(
            conn, "SELECT count(*) FROM published_answers", {}
        ),
    }

    result = run_source_resolution_pass(
        conn, _pass_request(ids["tenant_id"], ids["run_id"])
    )
    assert result.status == "succeeded"

    after = {
        "source_verifications": _count(
            conn, "SELECT count(*) FROM source_verifications", {}
        ),
        "evidence_spans": _count(conn, "SELECT count(*) FROM evidence_spans", {}),
        "claim_evidence_links": _count(
            conn, "SELECT count(*) FROM claim_evidence_links", {}
        ),
        "final_gate_reports": _count(
            conn, "SELECT count(*) FROM final_gate_reports", {}
        ),
        "published_answers": _count(
            conn, "SELECT count(*) FROM published_answers", {}
        ),
    }
    assert after == before

    # The run itself never links a gate report.
    assert conn.execute(
        text("SELECT final_gate_report_id FROM orchestration_runs WHERE id = :r"),
        {"r": ids["run_id"]},
    ).scalar() is None


# 6) external url is unreachable, never resolved
def test_db_external_url_is_unreachable_not_resolved(conn):
    ids = _seed_minimal_run(conn)
    cid = _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url="https://example.invalid/external",
    )

    result = run_source_resolution_pass(
        conn, _pass_request(ids["tenant_id"], ids["run_id"])
    )

    outcome = result.per_candidate_outcomes[cid]
    assert outcome in {RESOLUTION_OUTCOME_UNREACHABLE, RESOLUTION_OUTCOME_INSUFFICIENT_METADATA}
    assert outcome != RESOLUTION_OUTCOME_RESOLVED

    persisted = conn.execute(
        text("SELECT outcome FROM source_resolutions WHERE source_candidate_id = :c"),
        {"c": cid},
    ).scalar()
    assert persisted in {
        RESOLUTION_OUTCOME_UNREACHABLE,
        RESOLUTION_OUTCOME_INSUFFICIENT_METADATA,
    }
    assert persisted != RESOLUTION_OUTCOME_RESOLVED


# 7) insufficient metadata candidate
def test_db_insufficient_metadata_candidate(conn):
    ids = _seed_minimal_run(conn)
    cid = _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        url=None, provenance={}, raw={},
    )

    result = run_source_resolution_pass(
        conn, _pass_request(ids["tenant_id"], ids["run_id"])
    )

    assert result.per_candidate_outcomes[cid] == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA
    assert result.counters["insufficient_metadata_count"] == 1
    persisted = conn.execute(
        text("SELECT outcome FROM source_resolutions WHERE source_candidate_id = :c"),
        {"c": cid},
    ).scalar()
    assert persisted == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA


# 8) bounded max_candidates
def test_db_bounded_max_candidates(conn):
    ids = _seed_minimal_run(conn)
    for i in range(3):
        _seed_candidate(
            conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
            url=f"https://example.invalid/{i}",
        )

    request = _pass_request(ids["tenant_id"], ids["run_id"], max_candidates=1)
    result = run_source_resolution_pass(conn, request)

    assert result.status == "succeeded"
    assert result.counters["candidates_seen"] >= 2
    assert result.counters["candidates_attempted"] == 1
    assert result.counters["skipped_count"] >= 1
    # Exactly one resolution was persisted for the bounded pass.
    assert _count(
        conn,
        "SELECT count(*) FROM source_resolutions WHERE orchestration_run_id = :r",
        {"r": ids["run_id"]},
    ) == 1


# 9) tenant isolation
def test_db_tenant_isolation(conn):
    a = _seed_minimal_run(conn)
    b = _seed_minimal_run(conn)  # different tenant + different run

    cand_a = _seed_candidate(
        conn, tenant_id=a["tenant_id"], run_id=a["run_id"],
        url="https://example.invalid/a",
    )
    # A candidate belonging to a different tenant/run must not be touched.
    _seed_candidate(
        conn, tenant_id=b["tenant_id"], run_id=b["run_id"],
        url="https://example.invalid/b",
    )

    result = run_source_resolution_pass(conn, _pass_request(a["tenant_id"], a["run_id"]))

    assert set(result.per_candidate_outcomes.keys()) == {cand_a}
    assert result.counters["candidates_seen"] == 1
    # No resolution and no event was written against the other tenant's run.
    assert _count(
        conn,
        "SELECT count(*) FROM source_resolutions WHERE orchestration_run_id = :r",
        {"r": b["run_id"]},
    ) == 0
    assert _count(
        conn,
        "SELECT count(*) FROM orchestration_events WHERE orchestration_run_id = :r",
        {"r": b["run_id"]},
    ) == 0


# 10) per_agent_output scope
def test_db_per_agent_output_scope(conn):
    ids = _seed_minimal_run(conn)
    out1 = _seed_agent_output(
        conn, tenant_id=ids["tenant_id"], master_prompt_id=ids["master_prompt_id"],
        run_id=ids["run_id"],
    )
    out2 = _seed_agent_output(
        conn, tenant_id=ids["tenant_id"], master_prompt_id=ids["master_prompt_id"],
        run_id=ids["run_id"],
    )
    cand1 = _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        agent_output_id=out1, url="https://example.invalid/1",
    )
    _seed_candidate(
        conn, tenant_id=ids["tenant_id"], run_id=ids["run_id"],
        agent_output_id=out2, url="https://example.invalid/2",
    )

    request = _pass_request(
        ids["tenant_id"], ids["run_id"],
        candidate_selection_scope=SELECTION_SCOPE_PER_AGENT_OUTPUT,
        agent_output_id=out1,
    )
    result = run_source_resolution_pass(conn, request)

    assert set(result.per_candidate_outcomes.keys()) == {cand1}
    assert result.counters["candidates_seen"] == 1
    # Only the in-scope candidate produced a resolution.
    resolved_candidate_ids = {
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT source_candidate_id FROM source_resolutions "
                "WHERE orchestration_run_id = :r"
            ),
            {"r": ids["run_id"]},
        )
    }
    assert resolved_candidate_ids == {cand1}

# 11) a candidate whose INITIAL status is not eligible and has no resolution is
#     skipped, never treated as proposed (ORCH-MULTI-B2A)
def test_db_pass_skips_initial_non_proposed_candidate_without_resolution(conn):
    ids = _seed_minimal_run(conn)
    cid = _seed_candidate(
        conn,
        tenant_id=ids["tenant_id"],
        run_id=ids["run_id"],
        url="https://example.invalid/x",
        status="rejected",  # initial status, never mutated, no resolution exists
    )

    # eligible_states defaults to ("proposed",)
    result = run_source_resolution_pass(
        conn, _pass_request(ids["tenant_id"], ids["run_id"])
    )

    assert result.status == "succeeded"
    assert result.counters["candidates_seen"] == 1
    assert result.counters["candidates_attempted"] == 0
    assert result.counters["skipped_count"] == 1
    assert cid not in result.per_candidate_outcomes

    # No resolution row and no resolution event was written for the candidate.
    assert _count(
        conn,
        "SELECT count(*) FROM source_resolutions WHERE source_candidate_id = :c",
        {"c": cid},
    ) == 0
    assert _events_for_run(conn, ids["run_id"]) == []
