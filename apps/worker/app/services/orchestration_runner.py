"""Single-agent mock orchestration runner (Phase ORCH-RUNNER-A).

This module is the first real code of the orchestration runner designed in
PHASE_ORCH_RUNNER_PRE.md. It is a worker-level service that runs ONE
single-agent, single-pass, mock-only orchestration run end-to-end against the
tables introduced by ORCH-SCHEMA-A (migration 0011_orchestration_schema.sql),
composing the MockProviderAdapter and the pure mapping functions of
ORCH-PROVIDER-A (apps/worker/app/services/orchestration_provider.py).

Strict scope (PHASE_ORCH_RUNNER_PRE.md §1, §4, §26):

  - single-agent, single-pass, mock-only (provider 'mock', model 'mock-model').
  - DB-backed and auditable, deterministic where possible, idempotent.
  - It writes to the DB through a caller-owned SQLAlchemy Connection, using
    sqlalchemy.text. It does NOT open its own connection, does NOT commit and
    does NOT rollback when handed a Connection: the caller owns the transaction.
  - It performs NO network I/O, uses NO Redis, imports NO FastAPI, imports NO
    provider SDK, integrates NO real provider and NO local LLM.
  - It does NOT use the ORM; it uses sqlalchemy.text and a Connection only.
  - It does NOT call Claim Extraction, Evidence Binding, Source Resolution,
    Source Verification, Candidate Synthesis or the Final Answer Gate. It does
    NOT create final_gate_reports, published_answers, candidate_syntheses,
    source_resolutions or source_verifications.

Semantic invariants (PHASE_ORCH_RUNNER_PRE.md §3, §6, §15, §21):

  - provider output is a candidate output, NOT a publishable answer.
  - a source candidate is an unverified candidate (status 'proposed'), NOT
    evidence; it is never given an evidence span by the runner.
  - provider invocation 'succeeded' does NOT mean a claim is supported; a run
    'completed' does NOT mean publication is allowed. publication is NOT
    evaluated by the runner (publication_status='not_evaluated',
    final_gate_report_id stays NULL).
  - the Final Answer Gate remains the only publication authority; the runner
    does not invoke it.
  - request_hash / response_hash serve audit, debugging and idempotency; they
    do not prove the content. The technical record this runner persists is for
    audit/debugging and does not guarantee factual truth.
  - mock token usage is not a real cost; it is marked is_mock=True.

Public API:

    OrchestrationRunnerRequest   - frozen input dataclass.
    OrchestrationRunnerResult    - frozen output dataclass.
    run_single_agent_mock_orchestration(conn, request) -> OrchestrationRunnerResult
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services import orchestration_provider as op
from app.services.orchestration_provider import (
    MockProviderAdapter,
    ProviderMessage,
    ProviderRedactionPolicy,
    ProviderRequest,
    ProviderRetryPolicy,
    ProviderTimeoutPolicy,
)


# ===========================================================================
# Runner identity and constants (PHASE_ORCH_RUNNER_PRE.md §8)
# ===========================================================================
RUNNER_NAME = "mvp0_mock_orchestration_runner"
RUNNER_VERSION = "0.1.0"

# Synthetic result-status values surfaced by the runner output contract (§6).
RESULT_STATUS_SUCCEEDED = "succeeded"
RESULT_STATUS_FAILED = "failed"

# publication is never evaluated by the runner (§21).
PUBLICATION_STATUS_NOT_EVALUATED = "not_evaluated"

# orchestration_runs.status values used by the runner (subset of the 0011
# codomain). pending / running are transient; completed / failed are terminal.
RUN_STATUS_PENDING = "pending"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"

# orchestration_runs.mode codomain (0011).
RUN_MODE_VALUES: tuple[str, ...] = (
    "multi_ai_orchestration",
    "local_evidence",
    "hybrid",
)

# orchestration_runs.execution_mode: the runner only supports 'independent'.
EXECUTION_MODE_INDEPENDENT = "independent"

# Multi-agent bound (ORCH-MULTI-A design §7). A run is bounded: a request with
# more than MAX_AGENTS agent_config_ids is rejected by a later micro-patch.
MAX_AGENTS = 8

# orchestration_agent_runs.status values written by the runner (final only).
AGENT_RUN_STATUS_SUCCEEDED = "succeeded"
AGENT_RUN_STATUS_FAILED = "failed"

# orchestration_agent_outputs.output_kind for the mock candidate text (§14).
MOCK_OUTPUT_KIND = "mock_candidate_text"

# token_usage_records.pass_kind for the single independent answer pass (§13).
PASS_KIND_INDEPENDENT_ANSWER = "independent_answer"

# orchestration_events.event_type values used by the runner. ALL of these are
# in the closed 0011 codomain; the runner never invents an event_type (§7.1,
# §20).
EVENT_RUN_CREATED = "run_created"
EVENT_AGENT_RUN_STARTED = "agent_run_started"
EVENT_AGENT_RUN_COMPLETED = "agent_run_completed"
EVENT_AGENT_RUN_FAILED = "agent_run_failed"
EVENT_SOURCE_CANDIDATE_CREATED = "source_candidate_created"
EVENT_TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
EVENT_RUN_FAILED = "run_failed"

# Related-entity type tags written on orchestration_events.related_entity_type.
RELATED_AGENT_RUN = "orchestration_agent_run"
RELATED_SOURCE_CANDIDATE = "source_candidate"


# ===========================================================================
# Input / output contracts (PHASE_ORCH_RUNNER_PRE.md §5, §6)
# ===========================================================================


@dataclass(frozen=True)
class OrchestrationRunnerRequest:
    """Logical request for a single-agent mock orchestration run.

    No secret travels in this contract: no API key, no authentication token,
    no credential, no Authorization header. The provider is mock; no key is
    needed or expected.
    """

    tenant_id: str
    project_id: str | None
    master_prompt_version_id: str
    agent_config_id: str
    idempotency_key: str
    mode: str = "multi_ai_orchestration"
    execution_mode: str = "independent"
    token_budget_id: str | None = None
    mock_source_candidates: tuple[dict[str, Any], ...] = ()
    mock_error_code: str | None = None
    mock_error_message: str | None = None
    created_by: str | None = None


@dataclass(frozen=True)
class OrchestrationRunnerResult:
    """Logical result describing the final state of a run and the persisted
    fact ids.

    ``status`` is 'succeeded' or 'failed' for a freshly executed run. For an
    idempotent replay it mirrors the existing run: a completed run replays as
    'succeeded', a failed run as 'failed', and a still-running / pending run
    surfaces its raw status (documented choice, see §16).
    """

    status: str
    orchestration_run_id: str | None
    agent_run_id: str | None
    provider_invocation_id: str | None
    agent_output_id: str | None
    token_usage_record_ids: tuple[str, ...]
    agent_message_ids: tuple[str, ...]
    source_candidate_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    error_code: str | None
    error_message: str | None
    is_mock: bool
    publication_status: str
    gate_report_id: str | None


@dataclass(frozen=True)
class MultiAgentMockOrchestrationRequest:
    """Logical request for a multi-agent mock orchestration run (ORCH-MULTI-A).

    Extends OrchestrationRunnerRequest by replacing the single
    ``agent_config_id`` with an ordered tuple of ``agent_config_ids``. Request
    order is the deterministic execution order. No secret travels in this
    contract: no API key, no authentication token, no credential, no
    Authorization header. The provider is mock; no key is needed or expected.
    """

    tenant_id: str
    project_id: str | None
    master_prompt_version_id: str
    agent_config_ids: tuple[str, ...]
    idempotency_key: str
    mode: str = "multi_ai_orchestration"
    execution_mode: str = "independent"
    token_budget_id: str | None = None
    mock_source_candidates_by_agent: Mapping[
        str, tuple[dict[str, Any], ...] | list[dict[str, Any]]
    ] = field(default_factory=dict)
    mock_error_by_agent: Mapping[str, dict[str, str]] = field(default_factory=dict)
    created_by: str | None = None


@dataclass(frozen=True)
class MultiAgentMockOrchestrationResult:
    """Logical result for a multi-agent run, aggregated across all agents.

    Pluralizes the agent-scoped references of OrchestrationRunnerResult.
    ``publication_status`` is always 'not_evaluated' and ``gate_report_id`` is
    always None: the Final Answer Gate is not integrated by the runner.
    """

    status: str
    orchestration_run_id: str | None
    agent_run_ids: tuple[str, ...]
    provider_invocation_ids: tuple[str, ...]
    agent_output_ids: tuple[str, ...]
    token_usage_record_ids: tuple[str, ...]
    agent_message_ids: tuple[str, ...]
    source_candidate_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    failed_agent_config_ids: tuple[str, ...]
    error_code: str | None
    error_message: str | None
    is_mock: bool
    publication_status: str
    gate_report_id: str | None


_MULTI_NOT_IMPLEMENTED_MESSAGE = (
    "run_multi_agent_mock_orchestration is not implemented in patch 1; the "
    "full multi-agent transaction is added in a later micro-patch"
)


def _failed_multi_result_no_db(
    error_code: str, error_message: str
) -> MultiAgentMockOrchestrationResult:
    """Build a failed multi-agent result before any DB write."""
    return MultiAgentMockOrchestrationResult(
        status=RESULT_STATUS_FAILED,
        orchestration_run_id=None,
        agent_run_ids=(),
        failed_agent_config_ids=(),
        provider_invocation_ids=(),
        agent_output_ids=(),
        token_usage_record_ids=(),
        agent_message_ids=(),
        source_candidate_ids=(),
        event_ids=(),
        error_code=error_code,
        error_message=_redact(error_message),
        is_mock=True,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )


def run_multi_agent_mock_orchestration(
    conn: Connection,
    request: MultiAgentMockOrchestrationRequest,
) -> MultiAgentMockOrchestrationResult:
    """Run a multi-agent, single-pass, mock-only orchestration run.

    MICRO-PATCH 2 ONLY. This function now performs request-shape validation and
    DB preload/validation for all requested agent_configs. It still performs no
    DB writes, does not invoke the provider, and does not touch the Final Answer
    Gate. The full multi-agent transaction is added in a later micro-patch.
    """
    try:
        _validate_multi_request_shape(request)
    except _RunnerValidationError as exc:
        return _failed_multi_result_no_db(exc.error_code, exc.error_message)

    existing = _select_existing_run(conn, request.tenant_id, request.idempotency_key)
    if existing is not None:
        return _build_multi_replay_result(conn, existing)

    try:
        ctx_list, budget_limit, overflow_policy = _load_and_validate_multi(
            conn, request
        )
    except _RunnerValidationError as exc:
        return _failed_multi_result_no_db(exc.error_code, exc.error_message)

    return _execute_multi_provider_pass(
        conn,
        request,
        ctx_list,
        budget_limit,
        overflow_policy,
    )



# ===========================================================================
# Internal helpers
# ===========================================================================


class _RunnerValidationError(Exception):
    """A controlled validation failure raised before any DB write.

    When raised before the run row is created, the runner returns a failed
    result WITHOUT touching the database (PHASE_ORCH_RUNNER_PRE.md §5, §18).
    """

    def __init__(self, error_code: str, error_message: str) -> None:
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(error_message)


class _SeqCounter:
    """A monotonically increasing sequence_no counter for a run's events."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        value = self._n
        self._n += 1
        return value


@dataclass(frozen=True)
class _RunContext:
    """Validated, frozen inputs read from the DB for one run."""

    agent_config_id: str
    name: str
    master_prompt_id: str
    output_contract: dict[str, Any]
    constraints: dict[str, Any]
    temperature_config: dict[str, Any]
    retry_policy: dict[str, Any]
    source_access: dict[str, Any]
    reviewer_flag: bool
    synthesizer_flag: bool
    order_index: int
    task_summary: str | None
    prompt_text: str
    prompt_text_hash: str
    role_system_prompt: str
    role_task_prompt: str
    role_category: str
    role_version_no: int
    role_prompt_text_hash: str
    budget_limit: int | None
    overflow_policy: str | None
    max_tokens: int | None


def _new_id() -> str:
    """Return a fresh UUID string, preallocated in memory."""
    return str(uuid.uuid4())


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a JSONB-backed value into a plain dict, defensively.

    A psycopg JSONB column is typically returned as a dict already; this guards
    against a None or a string (some drivers) without raising.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _redact(message: object | None) -> str:
    """Redact and bound an error message before persistence.

    Delegates to the provider module's _safe_error_message so secrets of the
    form name=value / name: value are masked and the message is truncated. This
    is the single redaction seam for every error string the runner persists
    (PHASE_ORCH_RUNNER_PRE.md §18, §25).
    """
    return op._safe_error_message(message)


# ===========================================================================
# Validation and DB reads (PHASE_ORCH_RUNNER_PRE.md §5, §7)
# ===========================================================================


def _validate_request_shape(request: OrchestrationRunnerRequest) -> None:
    """Validate the request fields that need no DB access.

    Raises _RunnerValidationError on the first problem, so the runner can fail
    before any DB write.
    """
    if not request.idempotency_key or not str(request.idempotency_key).strip():
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST, "idempotency_key must be a non-empty string"
        )
    if request.mode not in RUN_MODE_VALUES:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"mode must be one of {RUN_MODE_VALUES}, got {request.mode!r}",
        )
    if request.execution_mode != EXECUTION_MODE_INDEPENDENT:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "execution_mode must be 'independent' in ORCH-RUNNER-A, "
            f"got {request.execution_mode!r}",
        )


def _select_existing_run(
    conn: Connection, tenant_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    """Return the existing run row for (tenant_id, idempotency_key), or None.

    Backs the idempotency / replay path (PHASE_ORCH_RUNNER_PRE.md §16): the
    UNIQUE (tenant_id, idempotency_key) means a re-presented key must return the
    existing run, never create a second one.
    """
    row = (
        conn.execute(
            text(
                "SELECT id, status, failure_reason "
                "FROM orchestration_runs "
                "WHERE tenant_id = :tenant_id AND idempotency_key = :idem"
            ),
            {"tenant_id": tenant_id, "idem": idempotency_key},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _load_and_validate(
    conn: Connection, request: OrchestrationRunnerRequest
) -> _RunContext:
    """Read and validate the run inputs from the DB.

    Reads agent_configs, master_prompt_versions, agent_role_prompts and, when
    requested, token_budgets. Enforces the validations of §7: tenant match,
    master_prompt link, provider 'mock', model 'mock-model', budget
    compatibility. Raises _RunnerValidationError on any failure so the runner
    can fail before creating the run.
    """
    cfg = (
        conn.execute(
            text(
                """
                SELECT id, tenant_id, master_prompt_id, agent_role_prompt_id,
                       name, provider, model, task_summary, output_contract,
                       constraints, temperature_config, retry_policy,
                       source_access, reviewer_flag, synthesizer_flag,
                       order_index
                FROM agent_configs
                WHERE id = :id
                """
            ),
            {"id": request.agent_config_id},
        )
        .mappings()
        .first()
    )
    if cfg is None:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"agent_config not found: {request.agent_config_id}",
        )
    if str(cfg["tenant_id"]) != str(request.tenant_id):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "tenant_id of request does not match agent_config.tenant_id",
        )
    if cfg["provider"] != op.MOCK_PROVIDER_NAME:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"provider must be {op.MOCK_PROVIDER_NAME!r} in ORCH-RUNNER-A, "
            f"got {cfg['provider']!r}",
        )
    if cfg["model"] != op.MOCK_MODEL_NAME:
        raise _RunnerValidationError(
            op.ERROR_INVALID_MODEL,
            f"model must be {op.MOCK_MODEL_NAME!r} in ORCH-RUNNER-A, "
            f"got {cfg['model']!r}",
        )

    mpv = (
        conn.execute(
            text(
                """
                SELECT id, master_prompt_id, prompt_text, prompt_text_hash
                FROM master_prompt_versions
                WHERE id = :id
                """
            ),
            {"id": request.master_prompt_version_id},
        )
        .mappings()
        .first()
    )
    if mpv is None:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"master_prompt_version not found: {request.master_prompt_version_id}",
        )
    if str(mpv["master_prompt_id"]) != str(cfg["master_prompt_id"]):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "master_prompt_version.master_prompt_id does not match "
            "agent_config.master_prompt_id",
        )

    role = (
        conn.execute(
            text(
                """
                SELECT system_prompt_text, task_prompt_text, role_category,
                       version_no
                FROM agent_role_prompts
                WHERE id = :id
                """
            ),
            {"id": cfg["agent_role_prompt_id"]},
        )
        .mappings()
        .first()
    )
    if role is None:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"agent_role_prompt not found: {cfg['agent_role_prompt_id']}",
        )

    budget_limit, overflow_policy = _load_budget(conn, request, cfg)

    constraints = _as_dict(cfg["constraints"])
    max_tokens = constraints.get("max_tokens")
    if not isinstance(max_tokens, int):
        max_tokens = None

    return _RunContext(
        agent_config_id=str(cfg["id"]),
        name=cfg["name"],
        master_prompt_id=str(cfg["master_prompt_id"]),
        output_contract=_as_dict(cfg["output_contract"]),
        constraints=constraints,
        temperature_config=_as_dict(cfg["temperature_config"]),
        retry_policy=_as_dict(cfg["retry_policy"]),
        source_access=_as_dict(cfg["source_access"]),
        reviewer_flag=bool(cfg["reviewer_flag"]),
        synthesizer_flag=bool(cfg["synthesizer_flag"]),
        order_index=int(cfg["order_index"]),
        task_summary=cfg["task_summary"],
        prompt_text=mpv["prompt_text"],
        prompt_text_hash=mpv["prompt_text_hash"],
        role_system_prompt=role["system_prompt_text"],
        role_task_prompt=role["task_prompt_text"],
        role_category=role["role_category"],
        role_version_no=int(role["version_no"]),
        role_prompt_text_hash=op.stable_hash(
            {
                "system_prompt_text": role["system_prompt_text"],
                "task_prompt_text": role["task_prompt_text"],
                "role_category": role["role_category"],
                "version_no": int(role["version_no"]),
            }
        ),
        budget_limit=budget_limit,
        overflow_policy=overflow_policy,
        max_tokens=max_tokens,
    )


def _load_budget(
    conn: Connection,
    request: OrchestrationRunnerRequest,
    cfg: Any,
) -> tuple[int | None, str | None]:
    """Read and validate the optional token budget.

    Returns (budget_limit, overflow_policy). When no token_budget_id is given,
    returns (None, None) and the preflight check is a no-op (always within
    budget).
    """
    if request.token_budget_id is None:
        return None, None
    budget = (
        conn.execute(
            text(
                """
                SELECT tenant_id, master_prompt_id, agent_config_id,
                       token_limit, overflow_policy, budget_level
                FROM token_budgets
                WHERE id = :id
                """
            ),
            {"id": request.token_budget_id},
        )
        .mappings()
        .first()
    )
    if budget is None:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"token_budget not found: {request.token_budget_id}",
        )
    if str(budget["tenant_id"]) != str(request.tenant_id):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "token_budget tenant_id does not match the request tenant_id",
        )
    if budget["agent_config_id"] is not None and str(
        budget["agent_config_id"]
    ) != str(request.agent_config_id):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "token_budget agent_config_id does not match the request agent_config_id",
        )
    if budget["master_prompt_id"] is not None and str(
        budget["master_prompt_id"]
    ) != str(cfg["master_prompt_id"]):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "token_budget master_prompt_id does not match agent_config.master_prompt_id",
        )
    return int(budget["token_limit"]), budget["overflow_policy"]


# ===========================================================================
# Multi-agent validation and DB reads (ORCH-MULTI-A)
# ===========================================================================


def _validate_multi_request_shape(
    request: MultiAgentMockOrchestrationRequest,
) -> None:
    """Validate the multi-agent request fields that need no DB access."""
    if not request.idempotency_key or not str(request.idempotency_key).strip():
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST, "idempotency_key must be a non-empty string"
        )
    if request.mode not in RUN_MODE_VALUES:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"mode must be one of {RUN_MODE_VALUES}, got {request.mode!r}",
        )
    if request.execution_mode != EXECUTION_MODE_INDEPENDENT:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "execution_mode must be 'independent' in ORCH-MULTI-A, "
            f"got {request.execution_mode!r}",
        )

    agent_ids = tuple(str(agent_id) for agent_id in request.agent_config_ids)
    if not agent_ids:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "agent_config_ids must contain at least one agent_config",
        )
    if len(set(agent_ids)) != len(agent_ids):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "agent_config_ids must not contain duplicates",
        )
    if len(agent_ids) > MAX_AGENTS:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"agent_config_ids exceeds MAX_AGENTS={MAX_AGENTS}",
        )

    requested = set(agent_ids)
    for agent_config_id in request.mock_error_by_agent:
        if str(agent_config_id) not in requested:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                "mock_error_by_agent references an agent_config_id not in the request",
            )
    for agent_config_id in request.mock_source_candidates_by_agent:
        if str(agent_config_id) not in requested:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                "mock_source_candidates_by_agent references an agent_config_id "
                "not in the request",
            )


def _build_multi_run_context(*, cfg: Any, mpv: Any, role: Any) -> _RunContext:
    """Build one _RunContext for a validated multi-agent agent_config."""
    constraints = _as_dict(cfg["constraints"])
    max_tokens = constraints.get("max_tokens")
    if not isinstance(max_tokens, int):
        max_tokens = None

    return _RunContext(
        agent_config_id=str(cfg["id"]),
        name=cfg["name"],
        master_prompt_id=str(cfg["master_prompt_id"]),
        output_contract=_as_dict(cfg["output_contract"]),
        constraints=constraints,
        temperature_config=_as_dict(cfg["temperature_config"]),
        retry_policy=_as_dict(cfg["retry_policy"]),
        source_access=_as_dict(cfg["source_access"]),
        reviewer_flag=bool(cfg["reviewer_flag"]),
        synthesizer_flag=bool(cfg["synthesizer_flag"]),
        order_index=int(cfg["order_index"]),
        task_summary=cfg["task_summary"],
        prompt_text=mpv["prompt_text"],
        prompt_text_hash=mpv["prompt_text_hash"],
        role_system_prompt=role["system_prompt_text"],
        role_task_prompt=role["task_prompt_text"],
        role_category=role["role_category"],
        role_version_no=int(role["version_no"]),
        role_prompt_text_hash=op.stable_hash(
            {
                "system_prompt_text": role["system_prompt_text"],
                "task_prompt_text": role["task_prompt_text"],
                "role_category": role["role_category"],
                "version_no": int(role["version_no"]),
            }
        ),
        budget_limit=None,
        overflow_policy=None,
        max_tokens=max_tokens,
    )


def _load_and_validate_multi(
    conn: Connection,
    request: MultiAgentMockOrchestrationRequest,
) -> tuple[list[_RunContext], int | None, str | None]:
    """Read and validate all multi-agent inputs from the DB, preserving request order."""
    mpv = (
        conn.execute(
            text(
                """
                SELECT id, master_prompt_id, prompt_text, prompt_text_hash
                FROM master_prompt_versions
                WHERE id = :id
                """
            ),
            {"id": request.master_prompt_version_id},
        )
        .mappings()
        .first()
    )
    if mpv is None:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"master_prompt_version not found: {request.master_prompt_version_id}",
        )

    expected_master_prompt_id = str(mpv["master_prompt_id"])
    shared_master_prompt_id: str | None = None
    ctx_list: list[_RunContext] = []

    for agent_config_id in request.agent_config_ids:
        cfg = (
            conn.execute(
                text(
                    """
                    SELECT id, tenant_id, master_prompt_id, agent_role_prompt_id,
                           name, provider, model, task_summary, output_contract,
                           constraints, temperature_config, retry_policy,
                           source_access, reviewer_flag, synthesizer_flag,
                           order_index
                    FROM agent_configs
                    WHERE id = :id
                    """
                ),
                {"id": agent_config_id},
            )
            .mappings()
            .first()
        )
        if cfg is None:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                f"agent_config not found: {agent_config_id}",
            )
        if str(cfg["tenant_id"]) != str(request.tenant_id):
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                "tenant_id of request does not match agent_config.tenant_id",
            )
        if cfg["provider"] != op.MOCK_PROVIDER_NAME:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                f"provider must be {op.MOCK_PROVIDER_NAME!r} in ORCH-MULTI-A, "
                f"got {cfg['provider']!r}",
            )
        if cfg["model"] != op.MOCK_MODEL_NAME:
            raise _RunnerValidationError(
                op.ERROR_INVALID_MODEL,
                f"model must be {op.MOCK_MODEL_NAME!r} in ORCH-MULTI-A, "
                f"got {cfg['model']!r}",
            )

        cfg_master_prompt_id = str(cfg["master_prompt_id"])
        if shared_master_prompt_id is None:
            shared_master_prompt_id = cfg_master_prompt_id
        elif cfg_master_prompt_id != shared_master_prompt_id:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                "all agent_configs must share the same master_prompt_id",
            )

        if cfg_master_prompt_id != expected_master_prompt_id:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                "master_prompt_version.master_prompt_id does not match "
                "agent_config.master_prompt_id",
            )

        role = (
            conn.execute(
                text(
                    """
                    SELECT system_prompt_text, task_prompt_text, role_category,
                           version_no
                    FROM agent_role_prompts
                    WHERE id = :id
                    """
                ),
                {"id": cfg["agent_role_prompt_id"]},
            )
            .mappings()
            .first()
        )
        if role is None:
            raise _RunnerValidationError(
                op.ERROR_INVALID_REQUEST,
                f"agent_role_prompt not found: {cfg['agent_role_prompt_id']}",
            )

        ctx_list.append(_build_multi_run_context(cfg=cfg, mpv=mpv, role=role))

    budget_limit, overflow_policy = _load_budget_multi(
        conn, request, expected_master_prompt_id
    )
    return ctx_list, budget_limit, overflow_policy


def _load_budget_multi(
    conn: Connection,
    request: MultiAgentMockOrchestrationRequest,
    master_prompt_id: str,
) -> tuple[int | None, str | None]:
    """Read and validate the optional multi-agent token budget."""
    if request.token_budget_id is None:
        return None, None

    budget = (
        conn.execute(
            text(
                """
                SELECT tenant_id, master_prompt_id, agent_config_id,
                       token_limit, overflow_policy, budget_level
                FROM token_budgets
                WHERE id = :id
                """
            ),
            {"id": request.token_budget_id},
        )
        .mappings()
        .first()
    )
    if budget is None:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            f"token_budget not found: {request.token_budget_id}",
        )
    if str(budget["tenant_id"]) != str(request.tenant_id):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "token_budget tenant_id does not match the request tenant_id",
        )
    if budget["master_prompt_id"] is not None and str(
        budget["master_prompt_id"]
    ) != str(master_prompt_id):
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "token_budget master_prompt_id does not match the run master_prompt_id",
        )
    if budget["agent_config_id"] is not None and str(
        budget["agent_config_id"]
    ) not in {str(agent_id) for agent_id in request.agent_config_ids}:
        raise _RunnerValidationError(
            op.ERROR_INVALID_REQUEST,
            "token_budget agent_config_id is not one of the requested agent_config_ids",
        )

    return int(budget["token_limit"]), budget["overflow_policy"]


# ===========================================================================
# Provider request construction (PHASE_ORCH_RUNNER_PRE.md §10)
# ===========================================================================


def _build_provider_request(
    *,
    request: OrchestrationRunnerRequest,
    ctx: _RunContext,
    run_id: str,
    agent_run_id: str,
    snapshot_id: str,
) -> ProviderRequest:
    """Build the ProviderRequest using the ORCH-PROVIDER-A contracts.

    The orchestration_agent_run_id carries the preallocated agent_run_id so
    request_hash is already consistent with the fact row written later. No
    secret is placed in the request.
    """
    constraints = dict(ctx.constraints)
    if request.mock_error_code is not None:
        constraints["mock_error_code"] = request.mock_error_code
    if request.mock_error_message is not None:
        # The runner also carries the injected message into constraints so the
        # request identity is complete; the persisted error message is built
        # and redacted by the runner itself (see _execute_run).
        constraints["mock_error_message"] = request.mock_error_message

    source_policy = dict(ctx.source_access)
    if request.mock_source_candidates:
        source_policy["mock_source_candidates"] = [
            dict(candidate) for candidate in request.mock_source_candidates
        ]

    return ProviderRequest(
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        orchestration_run_id=run_id,
        orchestration_agent_run_id=agent_run_id,
        agent_config_snapshot_id=snapshot_id,
        provider_name=op.MOCK_PROVIDER_NAME,
        model=op.MOCK_MODEL_NAME,
        messages=(ProviderMessage(role="user", content=ctx.prompt_text),),
        system_instructions=ctx.role_system_prompt,
        task_instructions=ctx.role_task_prompt,
        output_contract=dict(ctx.output_contract),
        constraints=constraints,
        source_policy=source_policy,
        max_tokens=ctx.max_tokens,
        temperature_like_config=dict(ctx.temperature_config),
        timeout_policy=ProviderTimeoutPolicy(),
        retry_policy=ProviderRetryPolicy(),
        redaction_policy=ProviderRedactionPolicy(),
        idempotency_key=request.idempotency_key,
        is_mock_expected=True,
    )


# ===========================================================================
# DB insert helpers (PHASE_ORCH_RUNNER_PRE.md §8, §17)
# ===========================================================================


def _insert_orchestration_run(
    conn: Connection,
    *,
    run_id: str,
    request: OrchestrationRunnerRequest,
    ctx: _RunContext,
) -> None:
    bounding = {
        "max_agents": 1,
        "pass_kinds": [PASS_KIND_INDEPENDENT_ANSWER],
        "runner_name": RUNNER_NAME,
        "runner_version": RUNNER_VERSION,
    }
    if ctx.budget_limit is not None:
        bounding["token_budget"] = {
            "token_limit": ctx.budget_limit,
            "overflow_policy": ctx.overflow_policy,
        }
    conn.execute(
        text(
            """
            INSERT INTO orchestration_runs
                (id, tenant_id, project_id, master_prompt_version_id, mode,
                 execution_mode, status, master_prompt_text_hash,
                 bounding_parameters, idempotency_key, policy_name,
                 policy_version, is_mock)
            VALUES
                (:id, :tenant_id, :project_id, :mpv_id, :mode, :execution_mode,
                 :status, :hash, CAST(:bounding AS JSONB), :idem, :policy_name,
                 :policy_version, TRUE)
            """
        ),
        {
            "id": run_id,
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
            "mpv_id": request.master_prompt_version_id,
            "mode": request.mode,
            "execution_mode": request.execution_mode,
            "status": RUN_STATUS_PENDING,
            "hash": ctx.prompt_text_hash,
            "bounding": json.dumps(bounding),
            "idem": request.idempotency_key,
            "policy_name": RUNNER_NAME,
            "policy_version": RUNNER_VERSION,
        },
    )


def _insert_multi_orchestration_run(
    conn: Connection,
    *,
    run_id: str,
    request: MultiAgentMockOrchestrationRequest,
    ctx_list: list[_RunContext],
    budget_limit: int | None,
    overflow_policy: str | None,
) -> None:
    """Insert the orchestration_runs row for a multi-agent scaffold run."""
    first_ctx = ctx_list[0]
    bounding = {
        "max_agents": len(ctx_list),
        "pass_kinds": [PASS_KIND_INDEPENDENT_ANSWER],
        "runner_name": RUNNER_NAME,
        "runner_version": RUNNER_VERSION,
    }
    if budget_limit is not None:
        bounding["token_budget"] = {
            "token_limit": budget_limit,
            "overflow_policy": overflow_policy,
        }

    conn.execute(
        text(
            """
            INSERT INTO orchestration_runs
                (id, tenant_id, project_id, master_prompt_version_id, mode,
                 execution_mode, status, master_prompt_text_hash,
                 bounding_parameters, idempotency_key, policy_name,
                 policy_version, is_mock)
            VALUES
                (:id, :tenant_id, :project_id, :mpv_id, :mode, :execution_mode,
                 :status, :hash, CAST(:bounding AS JSONB), :idem, :policy_name,
                 :policy_version, TRUE)
            """
        ),
        {
            "id": run_id,
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
            "mpv_id": request.master_prompt_version_id,
            "mode": request.mode,
            "execution_mode": request.execution_mode,
            "status": RUN_STATUS_PENDING,
            "hash": first_ctx.prompt_text_hash,
            "bounding": json.dumps(bounding),
            "idem": request.idempotency_key,
            "policy_name": RUNNER_NAME,
            "policy_version": RUNNER_VERSION,
        },
    )


def _set_run_running(conn: Connection, run_id: str) -> None:
    conn.execute(
        text(
            "UPDATE orchestration_runs "
            "SET status = :status, started_at = NOW() "
            "WHERE id = :id"
        ),
        {"status": RUN_STATUS_RUNNING, "id": run_id},
    )


def _set_run_terminal(
    conn: Connection,
    *,
    run_id: str,
    status: str,
    failure_reason: str | None,
) -> None:
    conn.execute(
        text(
            "UPDATE orchestration_runs "
            "SET status = :status, completed_at = NOW(), failure_reason = :fr "
            "WHERE id = :id"
        ),
        {"status": status, "fr": failure_reason, "id": run_id},
    )


def _insert_event(
    conn: Connection,
    *,
    run_id: str,
    event_type: str,
    sequence_no: int,
    idempotency_key: str,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    event_id = _new_id()
    conn.execute(
        text(
            """
            INSERT INTO orchestration_events
                (id, orchestration_run_id, event_type, sequence_no,
                 related_entity_type, related_entity_id, event_payload,
                 idempotency_key)
            VALUES
                (:id, :run_id, :event_type, :seq, :ret, :rei,
                 CAST(:payload AS JSONB), :idem)
            """
        ),
        {
            "id": event_id,
            "run_id": run_id,
            "event_type": event_type,
            "seq": sequence_no,
            "ret": related_entity_type,
            "rei": related_entity_id,
            "payload": json.dumps(payload or {}),
            "idem": idempotency_key,
        },
    )
    return event_id


def _insert_agent_config_snapshot(
    conn: Connection,
    *,
    snapshot_id: str,
    run_id: str,
    ctx: _RunContext,
) -> None:
    snapshot_payload = {
        "provider": op.MOCK_PROVIDER_NAME,
        "model": op.MOCK_MODEL_NAME,
        "agent_config_id": ctx.agent_config_id,
        "name": ctx.name,
        "role_category": ctx.role_category,
        "role_version_no": ctx.role_version_no,
        "system_prompt_text": ctx.role_system_prompt,
        "task_prompt_text": ctx.role_task_prompt,
        "output_contract": ctx.output_contract,
        "constraints": ctx.constraints,
        "temperature_config": ctx.temperature_config,
        "retry_policy": ctx.retry_policy,
        "source_access": ctx.source_access,
        "reviewer_flag": ctx.reviewer_flag,
        "synthesizer_flag": ctx.synthesizer_flag,
        "order_index": ctx.order_index,
        "task_summary": ctx.task_summary,
        "is_mock": True,
        "token_budget": (
            {
                "token_limit": ctx.budget_limit,
                "overflow_policy": ctx.overflow_policy,
            }
            if ctx.budget_limit is not None
            else None
        ),
    }
    conn.execute(
        text(
            """
            INSERT INTO agent_config_snapshots
                (id, orchestration_run_id, agent_config_id, snapshot_payload,
                 agent_role_prompt_text_hash)
            VALUES
                (:id, :run_id, :agent_config_id, CAST(:payload AS JSONB), :hash)
            """
        ),
        {
            "id": snapshot_id,
            "run_id": run_id,
            "agent_config_id": ctx.agent_config_id,
            "payload": json.dumps(snapshot_payload),
            "hash": ctx.role_prompt_text_hash,
        },
    )


def _insert_agent_run(
    conn: Connection,
    *,
    agent_run_id: str,
    run_id: str,
    snapshot_id: str,
    status: str,
    error_code: str | None,
    failure_reason: str | None,
) -> None:
    """Insert the orchestration_agent_runs row ONCE, with the final status.

    The table is append-only: there is never an interim 'running' row followed
    by an UPDATE (PHASE_ORCH_RUNNER_PRE.md §10). started_at / completed_at are
    both valued in this single INSERT.
    """
    conn.execute(
        text(
            """
            INSERT INTO orchestration_agent_runs
                (id, orchestration_run_id, agent_config_snapshot_id, status,
                 attempt_no, is_mock, error_code, failure_reason, started_at,
                 completed_at)
            VALUES
                (:id, :run_id, :snapshot_id, :status, 1, TRUE, :error_code,
                 :failure_reason, NOW(), NOW())
            """
        ),
        {
            "id": agent_run_id,
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "status": status,
            "error_code": error_code,
            "failure_reason": failure_reason,
        },
    )


def _insert_message(
    conn: Connection,
    *,
    agent_run_id: str,
    run_id: str,
    role: str,
    content: str,
    sequence_no: int,
) -> str:
    message_id = _new_id()
    body = content or ""
    conn.execute(
        text(
            """
            INSERT INTO orchestration_agent_messages
                (id, agent_run_id, orchestration_run_id, message_role,
                 content_text, content_hash, sequence_no, tokens)
            VALUES
                (:id, :agent_run_id, :run_id, :role, :content, :chash, :seq,
                 :tokens)
            """
        ),
        {
            "id": message_id,
            "agent_run_id": agent_run_id,
            "run_id": run_id,
            "role": role,
            "content": content,
            "chash": op.stable_hash(body),
            "seq": sequence_no,
            "tokens": op.estimate_mock_tokens(body),
        },
    )
    return message_id


def _insert_provider_invocation(
    conn: Connection, *, pi_id: str, record: dict[str, Any]
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO provider_invocations
                (id, tenant_id, agent_run_id, orchestration_run_id,
                 provider_name, model, request_hash, response_hash, status,
                 error_code, error_message, tokens_input, tokens_output,
                 cost_estimate, latency_ms, attempt_no, is_mock,
                 redaction_strategy, idempotency_key)
            VALUES
                (:id, :tenant_id, :agent_run_id, :run_id, :provider_name,
                 :model, :request_hash, :response_hash, :status, :error_code,
                 :error_message, :tokens_input, :tokens_output, :cost_estimate,
                 :latency_ms, :attempt_no, :is_mock, :redaction_strategy,
                 :idempotency_key)
            """
        ),
        {
            "id": pi_id,
            "tenant_id": record["tenant_id"],
            "agent_run_id": record["agent_run_id"],
            "run_id": record["orchestration_run_id"],
            "provider_name": record["provider_name"],
            "model": record["model"],
            "request_hash": record["request_hash"],
            "response_hash": record["response_hash"],
            "status": record["status"],
            "error_code": record["error_code"],
            "error_message": record["error_message"],
            "tokens_input": record["tokens_input"],
            "tokens_output": record["tokens_output"],
            "cost_estimate": float(record["cost_estimate"]),
            "latency_ms": record["latency_ms"],
            "attempt_no": record["attempt_no"],
            "is_mock": record["is_mock"],
            "redaction_strategy": record["redaction_strategy"],
            "idempotency_key": record["idempotency_key"],
        },
    )


def _insert_agent_output(
    conn: Connection, *, output_id: str, agent_run_id: str, result: Any
) -> None:
    structured = result.structured_payload or {}
    conn.execute(
        text(
            """
            INSERT INTO orchestration_agent_outputs
                (id, agent_run_id, output_kind, content_text, content_hash,
                 structured_payload, tokens, sequence_no)
            VALUES
                (:id, :agent_run_id, :output_kind, :content, :chash,
                 CAST(:structured AS JSONB), :tokens, 0)
            """
        ),
        {
            "id": output_id,
            "agent_run_id": agent_run_id,
            "output_kind": MOCK_OUTPUT_KIND,
            "content": result.content_text,
            "chash": result.response_hash,
            "structured": json.dumps(structured),
            "tokens": result.usage.tokens_output,
        },
    )


def _insert_token_usage(
    conn: Connection, *, tu_id: str, record: dict[str, Any]
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO token_usage_records
                (id, tenant_id, orchestration_run_id, agent_run_id,
                 provider_invocation_id, pass_kind, tokens_input,
                 tokens_output, cost_estimate, attempt_no, is_mock,
                 idempotency_key)
            VALUES
                (:id, :tenant_id, :run_id, :agent_run_id, :pi_id, :pass_kind,
                 :tokens_input, :tokens_output, :cost_estimate, :attempt_no,
                 :is_mock, :idempotency_key)
            """
        ),
        {
            "id": tu_id,
            "tenant_id": record["tenant_id"],
            "run_id": record["orchestration_run_id"],
            "agent_run_id": record["agent_run_id"],
            "pi_id": record["provider_invocation_id"],
            "pass_kind": record["pass_kind"],
            "tokens_input": record["tokens_input"],
            "tokens_output": record["tokens_output"],
            "cost_estimate": float(record["cost_estimate"]),
            "attempt_no": record["attempt_no"],
            "is_mock": record["is_mock"],
            "idempotency_key": record["idempotency_key"],
        },
    )


def _insert_source_candidate(
    conn: Connection, *, candidate_id: str, record: dict[str, Any]
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO source_candidates
                (id, tenant_id, orchestration_run_id, master_prompt_id,
                 agent_output_id, candidate_type, status, title, url,
                 citation_text, quoted_text, declared_confidence, provenance,
                 created_by, raw_citation_payload)
            VALUES
                (:id, :tenant_id, :run_id, :master_prompt_id, :agent_output_id,
                 :candidate_type, :status, :title, :url, :citation_text,
                 :quoted_text, :declared_confidence, CAST(:provenance AS JSONB),
                 :created_by, CAST(:raw AS JSONB))
            """
        ),
        {
            "id": candidate_id,
            "tenant_id": record["tenant_id"],
            "run_id": record["orchestration_run_id"],
            "master_prompt_id": record["master_prompt_id"],
            "agent_output_id": record["agent_output_id"],
            "candidate_type": record["candidate_type"],
            "status": record["status"],
            "title": record["title"],
            "url": record["url"],
            "citation_text": record["citation_text"],
            "quoted_text": record["quoted_text"],
            "declared_confidence": record["declared_confidence"],
            "provenance": json.dumps(record["provenance"]),
            "created_by": record["created_by"],
            "raw": json.dumps(record["raw_citation_payload"]),
        },
    )


# ===========================================================================
# Result builders
# ===========================================================================


def _failed_result_no_db(exc: _RunnerValidationError) -> OrchestrationRunnerResult:
    """Build a failed result for a validation failure that occurred BEFORE any
    DB write."""
    return OrchestrationRunnerResult(
        status=RESULT_STATUS_FAILED,
        orchestration_run_id=None,
        agent_run_id=None,
        provider_invocation_id=None,
        agent_output_id=None,
        token_usage_record_ids=(),
        agent_message_ids=(),
        source_candidate_ids=(),
        event_ids=(),
        error_code=exc.error_code,
        error_message=_redact(exc.error_message),
        is_mock=True,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )


def _result_status_for_run(run_status: str) -> str:
    """Map an orchestration_runs.status onto the result status field."""
    if run_status == RUN_STATUS_COMPLETED:
        return RESULT_STATUS_SUCCEEDED
    if run_status == RUN_STATUS_FAILED:
        return RESULT_STATUS_FAILED
    # pending / running are surfaced as-is for an in-flight replay.
    return run_status


def _build_replay_result(
    conn: Connection, existing: dict[str, Any]
) -> OrchestrationRunnerResult:
    """Build a result for an idempotent replay WITHOUT writing anything.

    Re-queries the already-persisted facts of the existing run so the returned
    result is complete and so the caller can confirm no duplication occurred.
    """
    run_id = str(existing["id"])
    run_status = existing["status"]

    agent_run_id = conn.execute(
        text(
            "SELECT id FROM orchestration_agent_runs "
            "WHERE orchestration_run_id = :r "
            "ORDER BY created_at, id LIMIT 1"
        ),
        {"r": run_id},
    ).scalar()

    pi_id = conn.execute(
        text(
            "SELECT id FROM provider_invocations "
            "WHERE orchestration_run_id = :r "
            "ORDER BY created_at, id LIMIT 1"
        ),
        {"r": run_id},
    ).scalar()

    output_id = None
    message_ids: list[str] = []
    error_code = None
    failure_reason = existing.get("failure_reason")
    if agent_run_id is not None:
        output_id = conn.execute(
            text(
                "SELECT id FROM orchestration_agent_outputs "
                "WHERE agent_run_id = :a ORDER BY sequence_no, id LIMIT 1"
            ),
            {"a": agent_run_id},
        ).scalar()
        message_ids = [
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT id FROM orchestration_agent_messages "
                    "WHERE agent_run_id = :a ORDER BY sequence_no"
                ),
                {"a": agent_run_id},
            )
        ]
        error_code = conn.execute(
            text(
                "SELECT error_code FROM orchestration_agent_runs WHERE id = :a"
            ),
            {"a": agent_run_id},
        ).scalar()

    token_usage_ids = [
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM token_usage_records "
                "WHERE orchestration_run_id = :r ORDER BY recorded_at, id"
            ),
            {"r": run_id},
        )
    ]
    source_candidate_ids = [
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM source_candidates "
                "WHERE orchestration_run_id = :r ORDER BY created_at, id"
            ),
            {"r": run_id},
        )
    ]
    event_ids = [
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM orchestration_events "
                "WHERE orchestration_run_id = :r ORDER BY sequence_no"
            ),
            {"r": run_id},
        )
    ]

    return OrchestrationRunnerResult(
        status=_result_status_for_run(run_status),
        orchestration_run_id=run_id,
        agent_run_id=str(agent_run_id) if agent_run_id is not None else None,
        provider_invocation_id=str(pi_id) if pi_id is not None else None,
        agent_output_id=str(output_id) if output_id is not None else None,
        token_usage_record_ids=tuple(token_usage_ids),
        agent_message_ids=tuple(message_ids),
        source_candidate_ids=tuple(source_candidate_ids),
        event_ids=tuple(event_ids),
        error_code=error_code,
        error_message=failure_reason,
        is_mock=True,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )


def _build_multi_replay_result(
    conn: Connection, existing: dict[str, Any]
) -> MultiAgentMockOrchestrationResult:
    """Build a multi-agent result for an idempotent replay without DB writes."""
    run_id = str(existing["id"])
    run_status = str(existing["status"])
    failure_reason = existing.get("failure_reason")

    result_status = (
        "succeeded"
        if run_status == RUN_STATUS_COMPLETED
        else RESULT_STATUS_FAILED
        if run_status == RUN_STATUS_FAILED
        else run_status
    )

    agent_run_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM orchestration_agent_runs "
                "WHERE orchestration_run_id = :r "
                "ORDER BY created_at, id"
            ),
            {"r": run_id},
        )
    )

    provider_invocation_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM provider_invocations "
                "WHERE orchestration_run_id = :r "
                "ORDER BY created_at, id"
            ),
            {"r": run_id},
        )
    )

    agent_output_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM orchestration_agent_outputs "
                "WHERE agent_run_id IN ("
                "  SELECT id FROM orchestration_agent_runs "
                "  WHERE orchestration_run_id = :r"
                ") "
                "ORDER BY sequence_no, id"
            ),
            {"r": run_id},
        )
    )

    token_usage_record_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM token_usage_records "
                "WHERE orchestration_run_id = :r "
                "ORDER BY recorded_at, id"
            ),
            {"r": run_id},
        )
    )

    agent_message_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM orchestration_agent_messages "
                "WHERE orchestration_run_id = :r "
                "ORDER BY sequence_no, id"
            ),
            {"r": run_id},
        )
    )

    source_candidate_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM source_candidates "
                "WHERE orchestration_run_id = :r "
                "ORDER BY created_at, id"
            ),
            {"r": run_id},
        )
    )

    event_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT id FROM orchestration_events "
                "WHERE orchestration_run_id = :r "
                "ORDER BY sequence_no"
            ),
            {"r": run_id},
        )
    )

    error_code = conn.execute(
        text(
            "SELECT error_code FROM orchestration_agent_runs "
            "WHERE orchestration_run_id = :r AND error_code IS NOT NULL "
            "ORDER BY created_at, id LIMIT 1"
        ),
        {"r": run_id},
    ).scalar()
    error_message = str(failure_reason) if failure_reason is not None else None

    failed_agent_config_ids = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT s.agent_config_id "
                "FROM orchestration_agent_runs ar "
                "JOIN agent_config_snapshots s "
                "  ON s.id = ar.agent_config_snapshot_id "
                "WHERE ar.orchestration_run_id = :r "
                "  AND ar.status = :failed "
                "ORDER BY ar.created_at, ar.id"
            ),
            {"r": run_id, "failed": AGENT_RUN_STATUS_FAILED},
        )
        if r[0] is not None
    )

    if error_code is None or error_message is None:
        run_failed_event_for_replay = conn.execute(
            text(
                "SELECT event_payload "
                "FROM orchestration_events "
                "WHERE orchestration_run_id = :r "
                "AND event_type = :event_type "
                "ORDER BY sequence_no DESC LIMIT 1"
            ),
            {"r": run_id, "event_type": EVENT_RUN_FAILED},
        ).mappings().first()
        run_failed_payload_for_replay: dict[str, Any] = {}
        if run_failed_event_for_replay is not None:
            run_failed_payload_for_replay = _as_dict(
                run_failed_event_for_replay["event_payload"]
            )

        if error_code is None:
            payload_error_code = run_failed_payload_for_replay.get("error_code")
            if payload_error_code is not None:
                error_code = str(payload_error_code)

        if error_message is None:
            payload_error_message = run_failed_payload_for_replay.get(
                "error_message"
            )
            if payload_error_message is not None:
                error_message = str(payload_error_message)

    return MultiAgentMockOrchestrationResult(
        status=result_status,
        orchestration_run_id=run_id,
        agent_run_ids=agent_run_ids,
        failed_agent_config_ids=failed_agent_config_ids,
        provider_invocation_ids=provider_invocation_ids,
        agent_output_ids=agent_output_ids,
        token_usage_record_ids=token_usage_record_ids,
        agent_message_ids=agent_message_ids,
        source_candidate_ids=source_candidate_ids,
        event_ids=event_ids,
        error_code=error_code,
        error_message=error_message,
        is_mock=True,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )

def run_single_agent_mock_orchestration(
    conn: Connection,
    request: OrchestrationRunnerRequest,
) -> OrchestrationRunnerResult:
    """Run a single-agent, single-pass, mock-only orchestration run.

    The function persists the whole run on the tables of ORCH-SCHEMA-A through
    the caller-owned ``conn``. It does NOT commit and does NOT rollback: the
    caller (a test or a future consumer) owns the transaction. It is
    idempotent on (tenant_id, idempotency_key), deterministic where the mock
    provider is deterministic, append-only on every fact table, and never
    invokes the Final Answer Gate.
    """
    # 1) Pre-DB validation. A failure here returns failed without any DB write.
    try:
        _validate_request_shape(request)
    except _RunnerValidationError as exc:
        return _failed_result_no_db(exc)

    # 2) Idempotency / replay: a known (tenant_id, idempotency_key) returns the
    #    existing run, never a second one.
    existing = _select_existing_run(conn, request.tenant_id, request.idempotency_key)
    if existing is not None:
        return _build_replay_result(conn, existing)

    # 3) Load + validate the run inputs. A failure here is still pre-creation:
    #    return failed without DB write.
    try:
        ctx = _load_and_validate(conn, request)
    except _RunnerValidationError as exc:
        return _failed_result_no_db(exc)

    # 4) Execute the run within the caller's transaction.
    return _execute_run(conn, request, ctx)


def _execute_multi_provider_pass(
    conn: Connection,
    request: MultiAgentMockOrchestrationRequest,
    ctx_list: list[_RunContext],
    budget_limit: int | None,
    overflow_policy: str | None,
) -> MultiAgentMockOrchestrationResult:
    """Execute one mock provider pass per agent.

    This phase persists provider invocations, token usage, messages and agent
    outputs. Source candidates are intentionally left out for a later
    micro-patch: provider-declared sources are still not evidence.
    """
    run_id = _new_id()
    seq = _SeqCounter()
    event_ids: list[str] = []
    agent_run_ids: list[str] = []
    provider_invocation_ids: list[str] = []
    agent_output_ids: list[str] = []
    token_usage_record_ids: list[str] = []
    agent_message_ids: list[str] = []
    source_candidate_ids: list[str] = []
    failed_agent_config_ids: list[str] = []
    run_idem = request.idempotency_key

    _insert_multi_orchestration_run(
        conn,
        run_id=run_id,
        request=request,
        ctx_list=ctx_list,
        budget_limit=budget_limit,
        overflow_policy=overflow_policy,
    )

    event_ids.append(
        _insert_event(
            conn,
            run_id=run_id,
            event_type=EVENT_RUN_CREATED,
            sequence_no=seq.next(),
            idempotency_key=f"{run_idem}:{EVENT_RUN_CREATED}",
            payload={"agent_count": len(ctx_list)},
        )
    )

    _set_run_running(conn, run_id)

    if budget_limit is not None and (
        overflow_policy is None or overflow_policy == "hard_stop"
    ):
        estimated_input_tokens = 0
        for index, ctx in enumerate(ctx_list):
            preflight_request = OrchestrationRunnerRequest(
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                master_prompt_version_id=request.master_prompt_version_id,
                agent_config_id=ctx.agent_config_id,
                idempotency_key=f"{run_idem}:budget-preflight:{index}",
                mode=request.mode,
                execution_mode=request.execution_mode,
                token_budget_id=request.token_budget_id,
                mock_source_candidates=(),
                mock_error_code=None,
                mock_error_message=None,
                created_by=request.created_by,
            )
            provider_request = _build_provider_request(
                request=preflight_request,
                ctx=ctx,
                run_id=run_id,
                agent_run_id=_new_id(),
                snapshot_id=_new_id(),
            )
            estimated_input_tokens += op.count_request_input_tokens(
                provider_request
            )

        if estimated_input_tokens > budget_limit:
            error_code = op.ERROR_BUDGET_EXCEEDED
            error_message = _redact(
                "multi-agent mock preflight budget check: "
                f"estimated input tokens {estimated_input_tokens} "
                f"exceed budget limit {budget_limit}"
            )
            event_ids.append(
                _insert_event(
                    conn,
                    run_id=run_id,
                    event_type=EVENT_TOKEN_BUDGET_EXCEEDED,
                    sequence_no=seq.next(),
                    idempotency_key=f"{run_idem}:{EVENT_TOKEN_BUDGET_EXCEEDED}",
                    payload={
                        "estimated_input_tokens": estimated_input_tokens,
                        "budget_limit": budget_limit,
                        "overflow_policy": overflow_policy,
                        "agent_count": len(ctx_list),
                    },
                )
            )
            event_ids.append(
                _insert_event(
                    conn,
                    run_id=run_id,
                    event_type=EVENT_RUN_FAILED,
                    sequence_no=seq.next(),
                    idempotency_key=f"{run_idem}:{EVENT_RUN_FAILED}",
                    payload={
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                )
            )
            _set_run_terminal(
                conn,
                run_id=run_id,
                status=RUN_STATUS_FAILED,
                failure_reason=error_message,
            )
            return MultiAgentMockOrchestrationResult(
                status=RESULT_STATUS_FAILED,
                orchestration_run_id=run_id,
                agent_run_ids=(),
                failed_agent_config_ids=(),
                provider_invocation_ids=(),
                agent_output_ids=(),
                token_usage_record_ids=(),
                agent_message_ids=(),
                source_candidate_ids=(),
                event_ids=tuple(event_ids),
                error_code=error_code,
                error_message=error_message,
                is_mock=True,
                publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
                gate_report_id=None,
            )

    first_error_code: str | None = None
    first_error_message: str | None = None

    for index, ctx in enumerate(ctx_list):
        snapshot_id = _new_id()
        agent_run_id = _new_id()

        _insert_agent_config_snapshot(
            conn,
            snapshot_id=snapshot_id,
            run_id=run_id,
            ctx=ctx,
        )

        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_AGENT_RUN_STARTED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_AGENT_RUN_STARTED}:{index}",
                related_entity_type=RELATED_AGENT_RUN,
                related_entity_id=agent_run_id,
                payload={
                    "agent_config_id": ctx.agent_config_id,
                    "order_index": ctx.order_index,
                },
            )
        )

        mock_error_cfg = request.mock_error_by_agent.get(ctx.agent_config_id) or {}
        if not isinstance(mock_error_cfg, dict):
            mock_error_cfg = {}

        raw_source_candidates = (
            request.mock_source_candidates_by_agent.get(ctx.agent_config_id, ())
            or ()
        )
        if isinstance(raw_source_candidates, (list, tuple)):
            mock_source_candidates = tuple(
                dict(candidate)
                for candidate in raw_source_candidates
                if isinstance(candidate, dict)
            )
        else:
            mock_source_candidates = ()

        single_request = OrchestrationRunnerRequest(
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            master_prompt_version_id=request.master_prompt_version_id,
            agent_config_id=ctx.agent_config_id,
            idempotency_key=f"{run_idem}:agent:{index}",
            mode=request.mode,
            execution_mode=request.execution_mode,
            token_budget_id=request.token_budget_id,
            mock_source_candidates=mock_source_candidates,
            mock_error_code=mock_error_cfg.get("error_code"),
            mock_error_message=mock_error_cfg.get("error_message"),
            created_by=request.created_by,
        )

        provider_request = _build_provider_request(
            request=single_request,
            ctx=ctx,
            run_id=run_id,
            agent_run_id=agent_run_id,
            snapshot_id=snapshot_id,
        )

        adapter = MockProviderAdapter()
        provider_result = adapter.invoke(provider_request)
        succeeded = provider_result.status == op.PROVIDER_STATUS_SUCCEEDED

        if succeeded:
            error_code = None
            safe_error_message = None
        else:
            error_code = (
                provider_result.error.error_code
                if provider_result.error
                else op.ERROR_UNKNOWN
            )
            raw_message = (
                single_request.mock_error_message
                if single_request.mock_error_message is not None
                else (
                    provider_result.error.error_message
                    if provider_result.error
                    else None
                )
            )
            safe_error_message = _redact(raw_message)
            failed_agent_config_ids.append(ctx.agent_config_id)
            if first_error_code is None:
                first_error_code = error_code
                first_error_message = safe_error_message

        _insert_agent_run(
            conn,
            agent_run_id=agent_run_id,
            run_id=run_id,
            snapshot_id=snapshot_id,
            status=AGENT_RUN_STATUS_SUCCEEDED
            if succeeded
            else AGENT_RUN_STATUS_FAILED,
            error_code=error_code,
            failure_reason=safe_error_message,
        )
        agent_run_ids.append(agent_run_id)

        agent_message_ids.append(
            _insert_message(
                conn,
                agent_run_id=agent_run_id,
                run_id=run_id,
                role="system",
                content=ctx.role_system_prompt,
                sequence_no=0,
            )
        )
        agent_message_ids.append(
            _insert_message(
                conn,
                agent_run_id=agent_run_id,
                run_id=run_id,
                role="user",
                content=ctx.prompt_text,
                sequence_no=1,
            )
        )
        if succeeded:
            agent_message_ids.append(
                _insert_message(
                    conn,
                    agent_run_id=agent_run_id,
                    run_id=run_id,
                    role="assistant",
                    content=provider_result.content_text or "",
                    sequence_no=2,
                )
            )

        pi_id = _new_id()
        pi_record = op.to_provider_invocation_record(
            provider_request,
            provider_result,
            attempt_no=1,
        )
        if not succeeded:
            pi_record["error_message"] = safe_error_message
        _insert_provider_invocation(conn, pi_id=pi_id, record=pi_record)
        provider_invocation_ids.append(pi_id)

        tu_id = _new_id()
        tu_record = op.to_token_usage_record(
            provider_request,
            provider_result,
            provider_invocation_id=pi_id,
            pass_kind=PASS_KIND_INDEPENDENT_ANSWER,
            attempt_no=1,
        )
        _insert_token_usage(conn, tu_id=tu_id, record=tu_record)
        token_usage_record_ids.append(tu_id)

        if succeeded:
            output_id = _new_id()
            _insert_agent_output(
                conn,
                output_id=output_id,
                agent_run_id=agent_run_id,
                result=provider_result,
            )
            agent_output_ids.append(output_id)

            candidate_records = op.source_candidates_to_records(
                provider_request,
                provider_result,
                agent_output_id=output_id,
            )
            for candidate_index, candidate_record in enumerate(candidate_records):
                candidate_id = _new_id()
                _insert_source_candidate(
                    conn,
                    candidate_id=candidate_id,
                    record=candidate_record,
                )
                source_candidate_ids.append(candidate_id)
                event_ids.append(
                    _insert_event(
                        conn,
                        run_id=run_id,
                        event_type=EVENT_SOURCE_CANDIDATE_CREATED,
                        sequence_no=seq.next(),
                        idempotency_key=(
                            f"{run_idem}:{EVENT_SOURCE_CANDIDATE_CREATED}:"
                            f"{index}:{candidate_index}"
                        ),
                        related_entity_type=RELATED_SOURCE_CANDIDATE,
                        related_entity_id=candidate_id,
                        payload={"agent_config_id": ctx.agent_config_id},
                    )
                )

            event_ids.append(
                _insert_event(
                    conn,
                    run_id=run_id,
                    event_type=EVENT_AGENT_RUN_COMPLETED,
                    sequence_no=seq.next(),
                    idempotency_key=f"{run_idem}:{EVENT_AGENT_RUN_COMPLETED}:{index}",
                    related_entity_type=RELATED_AGENT_RUN,
                    related_entity_id=agent_run_id,
                    payload={"agent_config_id": ctx.agent_config_id},
                )
            )
        else:
            event_ids.append(
                _insert_event(
                    conn,
                    run_id=run_id,
                    event_type=EVENT_AGENT_RUN_FAILED,
                    sequence_no=seq.next(),
                    idempotency_key=f"{run_idem}:{EVENT_AGENT_RUN_FAILED}:{index}",
                    related_entity_type=RELATED_AGENT_RUN,
                    related_entity_id=agent_run_id,
                    payload={
                        "agent_config_id": ctx.agent_config_id,
                        "error_code": error_code,
                        "error_message": safe_error_message,
                    },
                )
            )

    if failed_agent_config_ids:
        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_RUN_FAILED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_RUN_FAILED}",
                payload={
                    "error_code": first_error_code,
                    "error_message": first_error_message,
                    "failed_agent_config_ids": failed_agent_config_ids,
                },
            )
        )
        _set_run_terminal(
            conn,
            run_id=run_id,
            status=RUN_STATUS_FAILED,
            failure_reason=first_error_message,
        )
        result_status = RESULT_STATUS_FAILED
    else:
        _set_run_terminal(
            conn,
            run_id=run_id,
            status=RUN_STATUS_COMPLETED,
            failure_reason=None,
        )
        result_status = RESULT_STATUS_SUCCEEDED

    return MultiAgentMockOrchestrationResult(
        status=result_status,
        orchestration_run_id=run_id,
        agent_run_ids=tuple(agent_run_ids),
        failed_agent_config_ids=tuple(failed_agent_config_ids),
        provider_invocation_ids=tuple(provider_invocation_ids),
        agent_output_ids=tuple(agent_output_ids),
        token_usage_record_ids=tuple(token_usage_record_ids),
        agent_message_ids=tuple(agent_message_ids),
        source_candidate_ids=tuple(source_candidate_ids),
        event_ids=tuple(event_ids),
        error_code=first_error_code,
        error_message=first_error_message,
        is_mock=True,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )

def _execute_run(
    conn: Connection,
    request: OrchestrationRunnerRequest,
    ctx: _RunContext,
) -> OrchestrationRunnerResult:
    """Persist the run end-to-end following the §17 ordered sequence."""
    run_id = _new_id()
    agent_run_id = _new_id()
    snapshot_id = _new_id()
    seq = _SeqCounter()
    event_ids: list[str] = []
    run_idem = request.idempotency_key

    # 2) INSERT orchestration_runs (pending).
    _insert_orchestration_run(conn, run_id=run_id, request=request, ctx=ctx)

    # 3) event run_created.
    event_ids.append(
        _insert_event(
            conn,
            run_id=run_id,
            event_type=EVENT_RUN_CREATED,
            sequence_no=seq.next(),
            idempotency_key=f"{run_idem}:{EVENT_RUN_CREATED}",
        )
    )

    # 4) materialize the pending -> running transition (no dedicated event_type).
    _set_run_running(conn, run_id)

    # 5) immutable snapshot of the agent configuration.
    _insert_agent_config_snapshot(
        conn, snapshot_id=snapshot_id, run_id=run_id, ctx=ctx
    )

    # 6) build the ProviderRequest with the preallocated agent_run_id.
    provider_request = _build_provider_request(
        request=request,
        ctx=ctx,
        run_id=run_id,
        agent_run_id=agent_run_id,
        snapshot_id=snapshot_id,
    )

    # 7) preflight budget check BEFORE invoking the provider.
    budget_error = op.enforce_mock_budget(
        provider_request, budget_limit_tokens=ctx.budget_limit
    )

    # 8) budget exceeded: no agent_run, no provider_invocation, no usage.
    if budget_error is not None:
        safe_msg = _redact(budget_error.error_message)
        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_TOKEN_BUDGET_EXCEEDED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_TOKEN_BUDGET_EXCEEDED}",
                payload={"error_code": budget_error.error_code},
            )
        )
        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_RUN_FAILED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_RUN_FAILED}",
                payload={
                    "error_code": budget_error.error_code,
                    "error_message": safe_msg,
                },
            )
        )
        _set_run_terminal(
            conn, run_id=run_id, status=RUN_STATUS_FAILED, failure_reason=safe_msg
        )
        return OrchestrationRunnerResult(
            status=RESULT_STATUS_FAILED,
            orchestration_run_id=run_id,
            agent_run_id=None,
            provider_invocation_id=None,
            agent_output_id=None,
            token_usage_record_ids=(),
            agent_message_ids=(),
            source_candidate_ids=(),
            event_ids=tuple(event_ids),
            error_code=budget_error.error_code,
            error_message=safe_msg,
            is_mock=True,
            publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
            gate_report_id=None,
        )

    # 9) event agent_run_started, referencing the preallocated agent_run_id.
    event_ids.append(
        _insert_event(
            conn,
            run_id=run_id,
            event_type=EVENT_AGENT_RUN_STARTED,
            sequence_no=seq.next(),
            idempotency_key=f"{run_idem}:{EVENT_AGENT_RUN_STARTED}",
            related_entity_type=RELATED_AGENT_RUN,
            related_entity_id=agent_run_id,
        )
    )

    # 10) invoke the mock provider IN MEMORY (deterministic, no network).
    adapter = MockProviderAdapter()
    result = adapter.invoke(provider_request)
    succeeded = result.status == op.PROVIDER_STATUS_SUCCEEDED

    # Build the persisted error message. On failure the runner prefers the
    # caller-supplied mock_error_message (more specific) and ALWAYS redacts it.
    if succeeded:
        error_code = None
        safe_error_message = None
    else:
        error_code = result.error.error_code if result.error else op.ERROR_UNKNOWN
        raw_message = (
            request.mock_error_message
            if request.mock_error_message is not None
            else (result.error.error_message if result.error else None)
        )
        safe_error_message = _redact(raw_message)

    # 11) INSERT orchestration_agent_runs ONCE with the final status.
    _insert_agent_run(
        conn,
        agent_run_id=agent_run_id,
        run_id=run_id,
        snapshot_id=snapshot_id,
        status=AGENT_RUN_STATUS_SUCCEEDED if succeeded else AGENT_RUN_STATUS_FAILED,
        error_code=error_code,
        failure_reason=safe_error_message,
    )

    # 12) FK-bound rows (only after the agent_run row exists).
    message_ids: list[str] = []
    message_ids.append(
        _insert_message(
            conn,
            agent_run_id=agent_run_id,
            run_id=run_id,
            role="system",
            content=ctx.role_system_prompt,
            sequence_no=0,
        )
    )
    message_ids.append(
        _insert_message(
            conn,
            agent_run_id=agent_run_id,
            run_id=run_id,
            role="user",
            content=ctx.prompt_text,
            sequence_no=1,
        )
    )
    if succeeded:
        message_ids.append(
            _insert_message(
                conn,
                agent_run_id=agent_run_id,
                run_id=run_id,
                role="assistant",
                content=result.content_text or "",
                sequence_no=2,
            )
        )

    # provider_invocations: the auditable fact of the invocation.
    pi_id = _new_id()
    pi_record = op.to_provider_invocation_record(provider_request, result, attempt_no=1)
    if not succeeded:
        # Override with the redacted, possibly caller-supplied message so the
        # persisted error_message never carries a secret.
        pi_record["error_message"] = safe_error_message
    _insert_provider_invocation(conn, pi_id=pi_id, record=pi_record)

    output_id: str | None = None
    token_usage_ids: list[str] = []
    source_candidate_ids: list[str] = []

    # token_usage_records: the invocation was attempted, so the mock token
    # consumption is recorded for audit/debugging on BOTH success and provider
    # failure, with provider_invocation_id valued. On a mock failed result
    # tokens_output is 0 while tokens_input still reflects the request. This is
    # mock usage (is_mock=True), not a real cost. It is NOT recorded on the
    # budget_exceeded preflight path, because there the provider is never
    # invoked (that path returns earlier).
    tu_id = _new_id()
    tu_record = op.to_token_usage_record(
        provider_request,
        result,
        provider_invocation_id=pi_id,
        pass_kind=PASS_KIND_INDEPENDENT_ANSWER,
        attempt_no=1,
    )
    _insert_token_usage(conn, tu_id=tu_id, record=tu_record)
    token_usage_ids.append(tu_id)

    if succeeded:
        # orchestration_agent_outputs: the candidate output (not a final answer).
        output_id = _new_id()
        _insert_agent_output(
            conn, output_id=output_id, agent_run_id=agent_run_id, result=result
        )

        # source_candidates: unverified candidates, never evidence.
        candidate_records = op.source_candidates_to_records(
            provider_request, result, agent_output_id=output_id
        )
        for index, candidate_record in enumerate(candidate_records):
            candidate_id = _new_id()
            _insert_source_candidate(
                conn, candidate_id=candidate_id, record=candidate_record
            )
            source_candidate_ids.append(candidate_id)
            event_ids.append(
                _insert_event(
                    conn,
                    run_id=run_id,
                    event_type=EVENT_SOURCE_CANDIDATE_CREATED,
                    sequence_no=seq.next(),
                    idempotency_key=f"{run_idem}:source_candidate:{index}",
                    related_entity_type=RELATED_SOURCE_CANDIDATE,
                    related_entity_id=candidate_id,
                )
            )

    # 13) terminal agent-run event.
    if succeeded:
        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_AGENT_RUN_COMPLETED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_AGENT_RUN_COMPLETED}",
                related_entity_type=RELATED_AGENT_RUN,
                related_entity_id=agent_run_id,
            )
        )
    else:
        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_AGENT_RUN_FAILED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_AGENT_RUN_FAILED}",
                related_entity_type=RELATED_AGENT_RUN,
                related_entity_id=agent_run_id,
                payload={
                    "error_code": error_code,
                    "error_message": safe_error_message,
                },
            )
        )
        # 14) run_failed.
        event_ids.append(
            _insert_event(
                conn,
                run_id=run_id,
                event_type=EVENT_RUN_FAILED,
                sequence_no=seq.next(),
                idempotency_key=f"{run_idem}:{EVENT_RUN_FAILED}",
                payload={
                    "error_code": error_code,
                    "error_message": safe_error_message,
                },
            )
        )

    # 15) final orchestration_runs.status transition.
    if succeeded:
        _set_run_terminal(
            conn, run_id=run_id, status=RUN_STATUS_COMPLETED, failure_reason=None
        )
    else:
        _set_run_terminal(
            conn,
            run_id=run_id,
            status=RUN_STATUS_FAILED,
            failure_reason=safe_error_message,
        )

    return OrchestrationRunnerResult(
        status=RESULT_STATUS_SUCCEEDED if succeeded else RESULT_STATUS_FAILED,
        orchestration_run_id=run_id,
        agent_run_id=agent_run_id,
        provider_invocation_id=pi_id,
        agent_output_id=output_id,
        token_usage_record_ids=tuple(token_usage_ids),
        agent_message_ids=tuple(message_ids),
        source_candidate_ids=tuple(source_candidate_ids),
        event_ids=tuple(event_ids),
        error_code=error_code,
        error_message=safe_error_message,
        is_mock=True,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )
