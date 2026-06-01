# ORCH-MULTI-A Implementation Report

## Status

Implemented and locally validated.

This phase adds the first bounded multi-agent mock orchestration path on top of the orchestration schema and provider abstraction already present in the project.

## Implemented files

- `apps/worker/app/services/orchestration_runner.py`
- `apps/worker/tests/test_orchestration_runner_service.py`
- `ORCH_MULTI_A_IMPLEMENTATION_REPORT.md`

## What was implemented

The runner now supports deterministic multi-agent mock orchestration through `run_multi_agent_mock_orchestration`.

Implemented capabilities:

- bounded list of agent configuration IDs;
- tenant, project, master prompt version and agent config validation;
- one orchestration run per idempotency key;
- one mock provider request per agent;
- persisted append-only facts for executed agents;
- provider invocations and token usage records;
- agent messages and outputs;
- provider-proposed source candidates only on successful agent outputs;
- partial failure with one successful agent and one failed agent;
- idempotency replay for completed, failed and budget-preflight runs;
- hard-stop token budget preflight before agent/provider facts are created.

## Persisted facts

On successful provider execution the runner persists orchestration runs, snapshots, events, agent runs, messages, provider invocations, token usage, outputs and source candidates.

On budget preflight failure the runner persists only orchestration runs and orchestration events.

No agent run, provider invocation, token usage, message, output or source candidate is created when the hard-stop budget check fails before provider execution.

## Idempotency behavior

A replay with the same tenant and idempotency key returns the already persisted run facts without inserting duplicates.

Covered replay cases: completed multi-agent run, failed multi-agent run with one failed agent, and budget preflight failure.

## Source candidate semantics

Source candidates created by the provider adapter remain proposed candidates.
They are not evidence spans.
They are not claim evidence links.
They do not affect publication status.
They do not affect any final gate decision in this phase.

A provider-proposed source only becomes useful downstream after source resolution, retrieval, evidence span extraction, source verification, claim binding and gate evaluation.

## Provider output semantics

Provider output is persisted as an auditable agent output.
It is not a published answer.
It is not a final answer.
It does not bypass the existing downstream gate path.

## Gate and publication status

This phase does not run the Final Answer Gate.
This phase does not create final gate reports.
This phase does not create published answers.

Every multi-agent result produced by this phase keeps:

- `publication_status = "not_evaluated"`
- `gate_report_id = None`

## Explicit non-goals

This phase does not implement real provider calls, network I/O, Redis orchestration, FastAPI routes, UI surfaces, source resolution, retrieval, evidence span extraction, source verification, claim extraction, claim-to-evidence binding, candidate synthesis, Final Answer Gate execution, answer publication, reviewer/critic/synthesizer semantics, retry orchestration, async orchestration, queue dispatch or local LLM execution.

## Test coverage

The runner test suite covers existing single-agent behavior, multi-agent scaffold persistence, provider execution, completed-run replay, source candidate persistence, partial failure, failed-run replay, hard-stop budget preflight failure, budget replay and replay result reconstruction cleanup.

Final local validation at phase close:

```text
17 passed
```

## Verification commands

```bash
python3 -m py_compile apps/worker/app/services/orchestration_runner.py
python3 -m py_compile apps/worker/tests/test_orchestration_runner_service.py
PYTHONPATH=apps/worker python -m pytest apps/worker/tests/test_orchestration_runner_service.py -q
git diff --check
```

## Final state

ORCH-MULTI-A is ready for commit after the final verification commands pass.
