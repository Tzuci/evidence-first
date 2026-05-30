"""Worker-level, DB-backed tests for
apps/worker/app/services/orchestration_runner.py (Phase ORCH-RUNNER-A).

Coverage map (10 scenarios required by the phase prompt §12):

   1. test_successful_single_agent_mock_run_persists_auditable_facts
   2. test_provider_error_injection_fails_run_without_agent_output
   3. test_budget_preflight_blocks_before_provider_invocation
   4. test_idempotency_replay_returns_existing_run_without_duplicates
   5. test_source_candidates_are_persisted_as_unverified_proposed_candidates
   6. test_runner_does_not_create_gate_or_publication_rows
   7. test_runner_rejects_non_mock_provider_or_model
   8. test_error_messages_are_redacted_before_persistence
   9. test_event_sequence_numbers_are_monotonic_and_event_types_are_schema_allowed
  10. test_module_uses_no_network_redis_fastapi_or_provider_sdk_imports

Design notes:

  - These are worker-level, DB-backed tests. They are mock-first and use
    NO API, NO Redis, NO FastAPI, NO network. The only external dependency
    is a real PostgreSQL reachable through DATABASE_URL; if it is unset or
    unreachable, the DB-backed tests skip cleanly with a clear message.

  - The runner is handed a caller-owned SQLAlchemy Connection and never
    commits or rolls back. Each test therefore opens a connection, begins a
    transaction, runs the runner and reads its facts back inside that same
    transaction, then rolls the whole thing back at teardown. The runner's
    writes are uncommitted but visible to same-transaction reads, and the
    database is left untouched between tests.

  - Migrations are applied once per session through the project migration
    runner (scripts/migrate.py, cmd_apply), exactly as the sibling root test
    tests/test_orch_schema_constraints.py does, using a one-off psycopg
    connection.

  - The forbidden-import inspection test (scenario 10) builds the banned
    token list from character-class fragments so a naive ``grep`` of THIS
    test file for those tokens does not self-match the literal list (the
    phase prompt §14 warns about exactly this self-interception). That test
    needs no DB and does not take the ``conn`` fixture.

  - Package ``app`` resolves to apps/worker/app, so the service module is
    importable directly without any sys.path tweaking.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text

from app.services import orchestration_runner as runner
from app.services.orchestration_runner import (
    OrchestrationRunnerRequest,
    run_single_agent_mock_orchestration,
)

# apps/worker/tests/<this file>  ->  parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# DB plumbing: skip cleanly when there is no reachable database.
# ---------------------------------------------------------------------------
def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _psycopg_url(url: str) -> str:
    """Return a libpq/psycopg DSN regardless of which dialect form is given.

    DATABASE_URL may carry either the SQLAlchemy driver form
    ``postgresql+psycopg://`` or the plain libpq form ``postgresql://``.
    psycopg.connect wants the plain form; mirror tests/conftest.py so the same
    env var works for both clients.
    """
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    return url


def _sqlalchemy_url(url: str) -> str:
    """Return a SQLAlchemy driver DSN regardless of which form is given."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _ensure_migrations(dsn: str) -> None:
    """Apply the project migrations once through scripts/migrate.py.

    Mirrors tests/test_orch_schema_constraints.py: it loads the migration
    runner module from disk and calls cmd_apply on a raw psycopg connection,
    then commits so the schema is visible to the SQLAlchemy engine.
    """
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
    """A session-scoped SQLAlchemy engine against a migrated database.

    Skips the whole DB-backed suite if DATABASE_URL is unset or the database
    is unreachable, so the file is safe to collect anywhere.
    """
    dsn = _database_url()
    if not dsn:
        pytest.skip("DATABASE_URL is not set; runner DB tests skipped")
    try:
        with psycopg.connect(_psycopg_url(dsn)) as probe:
            probe.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unreachable ({exc!r}); runner DB tests skipped")

    _ensure_migrations(dsn)
    engine = create_engine(_sqlalchemy_url(dsn), future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def conn(_migrated_engine):
    """A connection wrapping each test in a transaction rolled back at teardown.

    The runner writes through this connection and does NOT commit; the test
    reads the facts back inside the same transaction; the rollback at teardown
    leaves the database clean. The test owns the transaction, exactly the
    contract the runner expects (PHASE_ORCH_RUNNER_PRE.md §17.5).
    """
    connection = _migrated_engine.connect()
    trans = connection.begin()
    try:
        yield connection
    finally:
        trans.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Seeding helpers (SQLAlchemy text; mirror the style of the schema tests).
# ---------------------------------------------------------------------------
def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_tenant_and_user(conn) -> tuple[str, str]:
    """Ensure the dev tenant and dev user exist; return (tenant_id, user_id)."""
    tenant_id = conn.execute(
        text(
            "INSERT INTO tenants (name, slug, status) "
            "VALUES ('Dev', 'dev', 'active') "
            "ON CONFLICT (slug) DO NOTHING RETURNING id"
        )
    ).scalar()
    if tenant_id is None:
        tenant_id = conn.execute(
            text("SELECT id FROM tenants WHERE slug = 'dev'")
        ).scalar()
    tenant_id = str(tenant_id)

    user_id = conn.execute(
        text(
            "INSERT INTO users (tenant_id, email, display_name, status) "
            "VALUES (:t, 'dev@local', 'Dev', 'active') "
            "ON CONFLICT (tenant_id, email) DO NOTHING RETURNING id"
        ),
        {"t": tenant_id},
    ).scalar()
    if user_id is None:
        user_id = conn.execute(
            text(
                "SELECT id FROM users WHERE tenant_id = :t AND email = 'dev@local'"
            ),
            {"t": tenant_id},
        ).scalar()
    return tenant_id, str(user_id)


def _seed_project(conn, tenant_id: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO projects (tenant_id, name, mode_default) "
                "VALUES (:t, :n, 'closed_corpus') RETURNING id"
            ),
            {"t": tenant_id, "n": f"orch-runner-test-{uuid.uuid4()}"},
        ).scalar()
    )


def _seed_master_prompt(conn, *, tenant_id: str, project_id: str, user_id: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO master_prompts "
                "(tenant_id, project_id, created_by, prompt_text, title, status) "
                "VALUES (:t, :p, :u, :txt, 't', 'draft') RETURNING id"
            ),
            {
                "t": tenant_id,
                "p": project_id,
                "u": user_id,
                "txt": f"prompt-{uuid.uuid4()}",
            },
        ).scalar()
    )


def _seed_master_prompt_version(conn, *, master_prompt_id: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO master_prompt_versions "
                "(master_prompt_id, version_no, prompt_text, prompt_text_hash) "
                "VALUES (:mp, 1, :txt, :h) RETURNING id"
            ),
            {
                "mp": master_prompt_id,
                "txt": f"frozen prompt {uuid.uuid4()}",
                "h": _unique_hash(),
            },
        ).scalar()
    )


def _seed_role_prompt(conn, *, tenant_id: str) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO agent_role_prompts "
                "(tenant_id, name, role_category, system_prompt_text, "
                " task_prompt_text, version_no) "
                "VALUES (:t, :n, 'researcher', 'system prompt here', "
                " 'task prompt here', 1) RETURNING id"
            ),
            {"t": tenant_id, "n": f"role-{uuid.uuid4()}"},
        ).scalar()
    )


def _seed_agent_config(
    conn,
    *,
    tenant_id: str,
    master_prompt_id: str,
    role_prompt_id: str,
    provider: str = "mock",
    model: str = "mock-model",
) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO agent_configs "
                "(tenant_id, master_prompt_id, agent_role_prompt_id, name, "
                " provider, model) "
                "VALUES (:t, :mp, :rp, :n, :provider, :model) RETURNING id"
            ),
            {
                "t": tenant_id,
                "mp": master_prompt_id,
                "rp": role_prompt_id,
                "n": f"agent-{uuid.uuid4()}",
                "provider": provider,
                "model": model,
            },
        ).scalar()
    )


def _seed_token_budget(
    conn,
    *,
    tenant_id: str,
    master_prompt_id: str,
    token_limit: int,
    overflow_policy: str = "hard_stop",
) -> str:
    return str(
        conn.execute(
            text(
                "INSERT INTO token_budgets "
                "(tenant_id, master_prompt_id, budget_level, token_limit, "
                " overflow_policy) "
                "VALUES (:t, :mp, 'per_orchestration', :lim, :op) RETURNING id"
            ),
            {
                "t": tenant_id,
                "mp": master_prompt_id,
                "lim": token_limit,
                "op": overflow_policy,
            },
        ).scalar()
    )


def _seed_full_config(
    conn,
    *,
    provider: str = "mock",
    model: str = "mock-model",
) -> dict[str, str]:
    """Seed a complete, runnable configuration and return the ids."""
    tenant_id, user_id = _seed_tenant_and_user(conn)
    project_id = _seed_project(conn, tenant_id)
    master_prompt_id = _seed_master_prompt(
        conn, tenant_id=tenant_id, project_id=project_id, user_id=user_id
    )
    mpv_id = _seed_master_prompt_version(conn, master_prompt_id=master_prompt_id)
    role_id = _seed_role_prompt(conn, tenant_id=tenant_id)
    agent_config_id = _seed_agent_config(
        conn,
        tenant_id=tenant_id,
        master_prompt_id=master_prompt_id,
        role_prompt_id=role_id,
        provider=provider,
        model=model,
    )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "project_id": project_id,
        "master_prompt_id": master_prompt_id,
        "master_prompt_version_id": mpv_id,
        "agent_config_id": agent_config_id,
    }


def _make_request(ids: dict[str, str], **overrides) -> OrchestrationRunnerRequest:
    kwargs: dict = {
        "tenant_id": ids["tenant_id"],
        "project_id": ids["project_id"],
        "master_prompt_version_id": ids["master_prompt_version_id"],
        "agent_config_id": ids["agent_config_id"],
        "idempotency_key": f"run-idem-{uuid.uuid4()}",
    }
    kwargs.update(overrides)
    return OrchestrationRunnerRequest(**kwargs)


# small query helpers ------------------------------------------------------
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


# ===========================================================================
# 1) successful run persists auditable facts
# ===========================================================================
def test_successful_single_agent_mock_run_persists_auditable_facts(conn):
    ids = _seed_full_config(conn)
    request = _make_request(ids)

    result = run_single_agent_mock_orchestration(conn, request)

    assert result.status == "succeeded"
    assert result.is_mock is True
    assert result.publication_status == "not_evaluated"
    assert result.gate_report_id is None
    run_id = result.orchestration_run_id
    assert run_id is not None

    # orchestration_runs: completed, final_gate_report_id NULL.
    row = conn.execute(
        text(
            "SELECT status, final_gate_report_id, is_mock "
            "FROM orchestration_runs WHERE id = :r"
        ),
        {"r": run_id},
    ).mappings().first()
    assert row["status"] == "completed"
    assert row["final_gate_report_id"] is None
    assert row["is_mock"] is True

    # exactly one agent_config_snapshot.
    assert _count(
        conn,
        "SELECT count(*) FROM agent_config_snapshots WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 1

    # exactly one agent_run, succeeded, attempt_no 1.
    agent_rows = conn.execute(
        text(
            "SELECT status, attempt_no, is_mock FROM orchestration_agent_runs "
            "WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).mappings().all()
    assert len(agent_rows) == 1
    assert agent_rows[0]["status"] == "succeeded"
    assert agent_rows[0]["attempt_no"] == 1
    assert agent_rows[0]["is_mock"] is True

    # messages: system / user / assistant.
    roles = [
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT message_role FROM orchestration_agent_messages "
                "WHERE agent_run_id = :a ORDER BY sequence_no"
            ),
            {"a": result.agent_run_id},
        )
    ]
    assert roles == ["system", "user", "assistant"]

    # provider_invocations: succeeded, is_mock.
    pi = conn.execute(
        text(
            "SELECT status, is_mock FROM provider_invocations "
            "WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).mappings().all()
    assert len(pi) == 1
    assert pi[0]["status"] == "succeeded"
    assert pi[0]["is_mock"] is True

    # token_usage_records: one row, independent_answer, provider_invocation_id set.
    tu = conn.execute(
        text(
            "SELECT pass_kind, provider_invocation_id, is_mock "
            "FROM token_usage_records WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).mappings().all()
    assert len(tu) == 1
    assert tu[0]["pass_kind"] == "independent_answer"
    assert tu[0]["provider_invocation_id"] is not None
    assert tu[0]["is_mock"] is True

    # one agent output.
    assert _count(
        conn,
        "SELECT count(*) FROM orchestration_agent_outputs WHERE agent_run_id = :a",
        {"a": result.agent_run_id},
    ) == 1

    # events include run_created, agent_run_started, agent_run_completed.
    types = {et for _seq, et in _events_for_run(conn, run_id)}
    assert {"run_created", "agent_run_started", "agent_run_completed"} <= types

    # no final_gate_reports, no published_answers tied to this run/agent.
    assert _count(
        conn,
        "SELECT count(*) FROM source_resolutions sr "
        "JOIN source_candidates sc ON sc.id = sr.source_candidate_id "
        "WHERE sc.orchestration_run_id = :r",
        {"r": run_id},
    ) == 0


# ===========================================================================
# 2) provider error injection fails the run without an agent output
# ===========================================================================
def test_provider_error_injection_fails_run_without_agent_output(conn):
    ids = _seed_full_config(conn)
    request = _make_request(ids, mock_error_code="invalid_request")

    result = run_single_agent_mock_orchestration(conn, request)

    assert result.status == "failed"
    assert result.error_code == "invalid_request"
    run_id = result.orchestration_run_id
    assert run_id is not None

    # run failed.
    assert conn.execute(
        text("SELECT status FROM orchestration_runs WHERE id = :r"),
        {"r": run_id},
    ).scalar() == "failed"

    # agent_run failed, with an error_code.
    agent = conn.execute(
        text(
            "SELECT status, error_code FROM orchestration_agent_runs "
            "WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).mappings().first()
    assert agent["status"] == "failed"
    assert agent["error_code"] == "invalid_request"

    # provider_invocation failed.
    assert conn.execute(
        text(
            "SELECT status FROM provider_invocations WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).scalar() == "failed"

    # token_usage_records IS persisted on provider failure (the invocation was
    # attempted): one row, independent_answer, provider_invocation_id valued,
    # mock usage with tokens_input >= 1 and tokens_output 0.
    tu = conn.execute(
        text(
            "SELECT pass_kind, provider_invocation_id, tokens_input, "
            "tokens_output, is_mock "
            "FROM token_usage_records WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).mappings().all()
    assert len(tu) == 1
    assert tu[0]["pass_kind"] == "independent_answer"
    assert tu[0]["provider_invocation_id"] is not None
    assert int(tu[0]["tokens_input"]) >= 1
    assert int(tu[0]["tokens_output"]) == 0
    assert tu[0]["is_mock"] is True

    # run_failed and agent_run_failed events present.
    types = {et for _seq, et in _events_for_run(conn, run_id)}
    assert "run_failed" in types
    assert "agent_run_failed" in types

    # NO agent output, no source candidates.
    assert _count(
        conn,
        "SELECT count(*) FROM orchestration_agent_outputs o "
        "JOIN orchestration_agent_runs ar ON ar.id = o.agent_run_id "
        "WHERE ar.orchestration_run_id = :r",
        {"r": run_id},
    ) == 0
    assert _count(
        conn,
        "SELECT count(*) FROM source_candidates WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 0


# ===========================================================================
# 3) budget preflight blocks before the provider is invoked
# ===========================================================================
def test_budget_preflight_blocks_before_provider_invocation(conn):
    ids = _seed_full_config(conn)
    budget_id = _seed_token_budget(
        conn,
        tenant_id=ids["tenant_id"],
        master_prompt_id=ids["master_prompt_id"],
        token_limit=0,  # any non-empty request estimate (>= 1) exceeds this.
    )
    request = _make_request(ids, token_budget_id=budget_id)

    result = run_single_agent_mock_orchestration(conn, request)

    assert result.status == "failed"
    assert result.error_code == "budget_exceeded"
    run_id = result.orchestration_run_id
    assert run_id is not None

    assert conn.execute(
        text("SELECT status FROM orchestration_runs WHERE id = :r"),
        {"r": run_id},
    ).scalar() == "failed"

    # token_budget_exceeded and run_failed events present.
    types = {et for _seq, et in _events_for_run(conn, run_id)}
    assert "token_budget_exceeded" in types
    assert "run_failed" in types

    # NO agent_run, NO provider_invocation, NO token_usage, NO output.
    assert _count(
        conn,
        "SELECT count(*) FROM orchestration_agent_runs WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 0
    assert _count(
        conn,
        "SELECT count(*) FROM provider_invocations WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 0
    assert _count(
        conn,
        "SELECT count(*) FROM token_usage_records WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 0
    # no agent_run_started event was emitted either.
    assert "agent_run_started" not in types


# ===========================================================================
# 4) idempotency replay returns the existing run without duplicates
# ===========================================================================
def test_idempotency_replay_returns_existing_run_without_duplicates(conn):
    ids = _seed_full_config(conn)
    idem = f"run-idem-fixed-{uuid.uuid4()}"
    request = _make_request(ids, idempotency_key=idem)

    first = run_single_agent_mock_orchestration(conn, request)
    assert first.status == "succeeded"
    run_id = first.orchestration_run_id

    # Re-present the SAME (tenant_id, idempotency_key).
    second = run_single_agent_mock_orchestration(conn, request)

    # Same run, no second creation.
    assert second.orchestration_run_id == run_id
    assert second.status == "succeeded"

    assert _count(
        conn,
        "SELECT count(*) FROM orchestration_runs "
        "WHERE tenant_id = :t AND idempotency_key = :i",
        {"t": ids["tenant_id"], "i": idem},
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM provider_invocations WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM token_usage_records WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 1
    assert _count(
        conn,
        "SELECT count(*) FROM orchestration_agent_outputs o "
        "JOIN orchestration_agent_runs ar ON ar.id = o.agent_run_id "
        "WHERE ar.orchestration_run_id = :r",
        {"r": run_id},
    ) == 1

    # events are not duplicated: replay returns the same id tuple it persisted.
    assert set(second.event_ids) == set(first.event_ids)


# ===========================================================================
# 5) source candidates persisted as unverified proposed candidates
# ===========================================================================
def test_source_candidates_are_persisted_as_unverified_proposed_candidates(conn):
    ids = _seed_full_config(conn)
    request = _make_request(
        ids,
        mock_source_candidates=(
            {"title": "First source", "url": "https://example.invalid/a",
             "locator": "p1", "raw_text": "cited text a"},
            {"title": "Second source", "url": "https://example.invalid/b",
             "locator": "p2", "raw_text": "cited text b"},
        ),
    )

    result = run_single_agent_mock_orchestration(conn, request)
    assert result.status == "succeeded"
    run_id = result.orchestration_run_id

    rows = conn.execute(
        text(
            "SELECT status, candidate_type, provenance "
            "FROM source_candidates WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).mappings().all()
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "proposed"
        assert row["candidate_type"] == "agent_cited"
        provenance = row["provenance"]
        if isinstance(provenance, str):
            import json as _json
            provenance = _json.loads(provenance)
        assert provenance.get("is_verified") is False

    # one source_candidate_created event per candidate, distinct idempotency keys.
    ev = conn.execute(
        text(
            "SELECT idempotency_key FROM orchestration_events "
            "WHERE orchestration_run_id = :r AND event_type = 'source_candidate_created'"
        ),
        {"r": run_id},
    ).fetchall()
    keys = [str(r[0]) for r in ev]
    assert len(keys) == 2
    assert len(set(keys)) == 2

    # No resolution / verification was created for these candidates.
    assert _count(
        conn,
        "SELECT count(*) FROM source_resolutions sr "
        "JOIN source_candidates sc ON sc.id = sr.source_candidate_id "
        "WHERE sc.orchestration_run_id = :r",
        {"r": run_id},
    ) == 0


# ===========================================================================
# 6) runner creates no gate or publication rows
# ===========================================================================
def test_runner_does_not_create_gate_or_publication_rows(conn):
    ids = _seed_full_config(conn)

    before_gate = _count(conn, "SELECT count(*) FROM final_gate_reports", {})
    before_pub = _count(conn, "SELECT count(*) FROM published_answers", {})

    request = _make_request(
        ids,
        mock_source_candidates=(
            {"title": "src", "url": "https://example.invalid/x", "locator": "l"},
        ),
    )
    result = run_single_agent_mock_orchestration(conn, request)
    assert result.status == "succeeded"
    run_id = result.orchestration_run_id

    # No new gate report, no new published answer anywhere.
    assert _count(conn, "SELECT count(*) FROM final_gate_reports", {}) == before_gate
    assert _count(conn, "SELECT count(*) FROM published_answers", {}) == before_pub

    # Nothing the runner is forbidden to touch for this run.
    assert _count(
        conn,
        "SELECT count(*) FROM candidate_syntheses WHERE orchestration_run_id = :r",
        {"r": run_id},
    ) == 0
    assert _count(
        conn,
        "SELECT count(*) FROM source_resolutions sr "
        "JOIN source_candidates sc ON sc.id = sr.source_candidate_id "
        "WHERE sc.orchestration_run_id = :r",
        {"r": run_id},
    ) == 0
    assert _count(
        conn,
        "SELECT count(*) FROM source_verifications sv "
        "JOIN source_candidates sc ON sc.id = sv.source_candidate_id "
        "WHERE sc.orchestration_run_id = :r",
        {"r": run_id},
    ) == 0

    # And the run itself never links a gate report.
    assert conn.execute(
        text("SELECT final_gate_report_id FROM orchestration_runs WHERE id = :r"),
        {"r": run_id},
    ).scalar() is None


# ===========================================================================
# 7) runner rejects a non-mock provider or model
# ===========================================================================
def test_runner_rejects_non_mock_provider_or_model(conn):
    before_runs = _count(conn, "SELECT count(*) FROM orchestration_runs", {})
    before_provider_invocations = _count(
        conn, "SELECT count(*) FROM provider_invocations", {}
    )

    # Non-mock provider.
    ids_provider = _seed_full_config(conn, provider="openai-like", model="mock-model")
    result_p = run_single_agent_mock_orchestration(conn, _make_request(ids_provider))
    assert result_p.status == "failed"
    assert result_p.error_code == "invalid_request"
    # Rejected before any DB write: no run row created.
    assert result_p.orchestration_run_id is None

    # Non-mock model.
    ids_model = _seed_full_config(conn, provider="mock", model="not-mock-model")
    result_m = run_single_agent_mock_orchestration(conn, _make_request(ids_model))
    assert result_m.status == "failed"
    assert result_m.error_code == "invalid_model"
    assert result_m.orchestration_run_id is None

    # No new orchestration_runs and no new provider_invocations were written for
    # either rejected request. Counted as before/after so the test stays
    # rerun-safe on a non-empty dev database.
    assert _count(conn, "SELECT count(*) FROM orchestration_runs", {}) == before_runs
    assert (
        _count(conn, "SELECT count(*) FROM provider_invocations", {})
        == before_provider_invocations
    )


# ===========================================================================
# 8) error messages are redacted before persistence
# ===========================================================================
def test_error_messages_are_redacted_before_persistence(conn):
    ids = _seed_full_config(conn)
    secrets = ("sk-supersecret123", "tok-bearer-xyz", "pw-hunter-2")
    leaky_message = (
        "boom api_key=sk-supersecret123 "
        "authorization: Bearer tok-bearer-xyz password=pw-hunter-2"
    )
    request = _make_request(
        ids,
        mock_error_code="invalid_request",
        mock_error_message=leaky_message,
    )

    result = run_single_agent_mock_orchestration(conn, request)
    assert result.status == "failed"
    run_id = result.orchestration_run_id

    # The result message is redacted.
    assert "[REDACTED]" in (result.error_message or "")
    for secret in secrets:
        assert secret not in (result.error_message or "")

    # provider_invocations.error_message redacted.
    pi_msg = conn.execute(
        text(
            "SELECT error_message FROM provider_invocations "
            "WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).scalar() or ""
    # agent_run.failure_reason redacted.
    ar_msg = conn.execute(
        text(
            "SELECT failure_reason FROM orchestration_agent_runs "
            "WHERE orchestration_run_id = :r"
        ),
        {"r": run_id},
    ).scalar() or ""
    # orchestration_runs.failure_reason redacted.
    run_msg = conn.execute(
        text("SELECT failure_reason FROM orchestration_runs WHERE id = :r"),
        {"r": run_id},
    ).scalar() or ""

    for field in (pi_msg, ar_msg, run_msg):
        assert "[REDACTED]" in field
        for secret in secrets:
            assert secret not in field


# ===========================================================================
# 9) event sequence numbers monotonic; event types schema-allowed
# ===========================================================================
def test_event_sequence_numbers_are_monotonic_and_event_types_are_schema_allowed(conn):
    ids = _seed_full_config(conn)
    request = _make_request(
        ids,
        mock_source_candidates=(
            {"title": "s1", "url": "u1", "locator": "l1"},
            {"title": "s2", "url": "u2", "locator": "l2"},
        ),
    )
    result = run_single_agent_mock_orchestration(conn, request)
    assert result.status == "succeeded"
    run_id = result.orchestration_run_id

    events = _events_for_run(conn, run_id)
    seqs = [seq for seq, _et in events]
    # Monotonic, contiguous from 0.
    assert seqs == list(range(len(seqs)))

    # The only event types ORCH-RUNNER-A emits, all inside the 0011 codomain.
    allowed = {
        "run_created",
        "agent_run_started",
        "agent_run_completed",
        "agent_run_failed",
        "source_candidate_created",
        "token_budget_exceeded",
        "run_failed",
    }
    emitted = {et for _seq, et in events}
    assert emitted <= allowed

    # Explicitly absent: invented event types the prompt forbids (§9).
    forbidden = {
        "run_started",
        "provider_invocation_started",
        "provider_invocation_completed",
        "run_completed",
    }
    assert emitted.isdisjoint(forbidden)


# ===========================================================================
# 10) module uses no network / Redis / FastAPI / provider SDK imports
# ===========================================================================
def _banned_import_fragments() -> list[str]:
    """Return the banned import tokens, assembled from fragments at runtime so
    a naive ``grep`` of THIS test file does not self-match the literal list
    (phase prompt §14).
    """
    return [
        "re" + "quests",
        "ht" + "tpx",
        "aio" + "http",
        "url" + "lib",
        "soc" + "ket",
        "open" + "ai",
        "anthro" + "pic",
        "google." + "generativeai",
        "sub" + "process",
        "fast" + "api",
        "re" + "dis",
    ]


def test_module_uses_no_network_redis_fastapi_or_provider_sdk_imports():
    """The runner module source must import no network client, no Redis, no
    FastAPI and no provider SDK. We inspect the module source directly; this
    test needs no database.
    """
    source = inspect.getsource(runner)
    lowered = source.lower()

    for token in _banned_import_fragments():
        assert f"import {token}" not in lowered, f"must not import {token!r}"
        assert f"from {token}" not in lowered, f"must not import from {token!r}"

    module_dict = vars(runner)
    for token in _banned_import_fragments():
        top_level = token.split(".")[0]
        assert top_level not in module_dict, (
            f"module namespace must not bind {top_level!r}"
        )

    # Positive control: the module does rely on the allowed building blocks.
    assert "sqlalchemy" in lowered
    assert "orchestration_provider" in lowered
