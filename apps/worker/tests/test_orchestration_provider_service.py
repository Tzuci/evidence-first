"""Worker-level tests for
apps/worker/app/services/orchestration_provider.py
(Phase ORCH-PROVIDER-A).

Coverage map (13 scenarios required by the phase prompt §13):

   1. test_default_registry_contains_only_mock_provider
   2. test_mock_provider_success_is_deterministic_and_marked_mock
   3. test_request_and_response_hashes_are_stable_and_canonical
   4. test_redaction_hash_only_does_not_expose_secrets
   5. test_redacted_payload_mode_recursively_masks_sensitive_fields
   6. test_error_normalization_retryable_and_non_retryable
   7. test_mock_provider_error_injection_returns_failed_result
   8. test_provider_invocation_record_mapping_matches_schema_shape_and_has_no_secrets
   9. test_token_usage_record_mapping_supports_nullable_provider_invocation_id
  10. test_source_candidates_are_unverified_candidates_not_evidence
  11. test_budget_preflight_blocks_over_budget_without_invoking_real_provider
  12. test_module_uses_no_network_or_provider_sdk_imports
  13. test_mock_payload_contains_semantic_warning

Design notes:

  - This file lives under apps/worker/tests/. The Python package
    ``app`` resolves to apps/worker/app, so the service module is
    importable directly without any sys.path tweaking.

  - These tests are PURE and worker-level: NO database, NO Redis, NO
    FastAPI, NO network. The module under test imports none of those,
    so no monkeypatching of the network is needed. The tests run
    fully offline and deterministically.

  - All helpers are LOCAL to this file. No imports from other test
    files.

  - The forbidden-import inspection test (scenario 12) builds the list
    of banned tokens with character-class fragments so that a naive
    ``grep`` of this test file for those tokens does not self-match
    the literal list. See _banned_import_fragments().
"""
from __future__ import annotations

import importlib
import inspect
import uuid
from decimal import Decimal

import pytest

from app.services import orchestration_provider as op
from app.services.orchestration_provider import (
    CANDIDATE_STATUS_PROPOSED,
    CANDIDATE_TYPE_AGENT_CITED,
    DEFAULT_REDACTION_STRATEGY,
    ERROR_AUTHENTICATION_FAILED,
    ERROR_BUDGET_EXCEEDED,
    ERROR_INVALID_REQUEST,
    ERROR_RATE_LIMITED,
    ERROR_TIMEOUT,
    ERROR_UNKNOWN,
    MOCK_MODEL_NAME,
    MOCK_PROVIDER_NAME,
    PROVIDER_STATUS_FAILED,
    PROVIDER_STATUS_SUCCEEDED,
    REDACTION_MODE_HASH_ONLY,
    REDACTION_MODE_REDACTED_PAYLOAD,
    REDACTED_PLACEHOLDER,
    MockProviderAdapter,
    ProviderCapability,
    ProviderMessage,
    ProviderRedactionPolicy,
    ProviderRequest,
    ProviderRetryPolicy,
    ProviderTimeoutPolicy,
    build_request_hash,
    build_response_hash,
    canonical_json,
    count_request_input_tokens,
    default_registry,
    enforce_mock_budget,
    normalize_error,
    redact_payload,
    source_candidates_to_records,
    stable_hash,
    to_provider_invocation_record,
    to_token_usage_record,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------
def _build_request(
    *,
    constraints: dict | None = None,
    source_policy: dict | None = None,
    redaction_policy: ProviderRedactionPolicy | None = None,
    system_instructions: str = "system instructions for the agent",
    task_instructions: str = "task instructions for the agent",
    messages: tuple[ProviderMessage, ...] | None = None,
    idempotency_key: str | None = None,
) -> ProviderRequest:
    """Build a ProviderRequest with sensible deterministic defaults.

    Identifiers are uuid-derived per call unless explicitly pinned, so
    two calls with the same logical content but a fresh idempotency
    key still differ where they should.
    """
    if messages is None:
        messages = (
            ProviderMessage(role="user", content="please answer this"),
        )
    return ProviderRequest(
        tenant_id="tenant-1",
        project_id="project-1",
        orchestration_run_id="run-1",
        orchestration_agent_run_id="agent-run-1",
        agent_config_snapshot_id="snapshot-1",
        provider_name=MOCK_PROVIDER_NAME,
        model=MOCK_MODEL_NAME,
        messages=messages,
        system_instructions=system_instructions,
        task_instructions=task_instructions,
        output_contract={"kind": "free_text"},
        constraints=constraints if constraints is not None else {},
        source_policy=source_policy if source_policy is not None else {},
        max_tokens=512,
        temperature_like_config={"temperature": 0.0},
        timeout_policy=ProviderTimeoutPolicy(),
        retry_policy=ProviderRetryPolicy(),
        redaction_policy=(
            redaction_policy
            if redaction_policy is not None
            else ProviderRedactionPolicy()
        ),
        idempotency_key=(
            idempotency_key
            if idempotency_key is not None
            else f"idem-{uuid.uuid4()}"
        ),
        is_mock_expected=True,
    )


# Schema columns expected on a provider_invocations record dict
# (migration 0011_orchestration_schema.sql / phase prompt §11).
# tenant_id is included: provider_invocations.tenant_id is NOT NULL.
_PROVIDER_INVOCATION_KEYS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "orchestration_run_id",
        "agent_run_id",
        "provider_name",
        "model",
        "request_hash",
        "response_hash",
        "status",
        "error_code",
        "error_message",
        "tokens_input",
        "tokens_output",
        "cost_estimate",
        "latency_ms",
        "attempt_no",
        "is_mock",
        "redaction_strategy",
        "idempotency_key",
    }
)

# Schema columns expected on a token_usage_records record dict.
# tenant_id is included: token_usage_records.tenant_id is NOT NULL.
_TOKEN_USAGE_KEYS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "orchestration_run_id",
        "agent_run_id",
        "provider_invocation_id",
        "pass_kind",
        "tokens_input",
        "tokens_output",
        "cost_estimate",
        "attempt_no",
        "is_mock",
        "idempotency_key",
    }
)

# Substrings that must never appear as a KEY in a record dict meant to
# be persisted. Each fragment is assembled from pieces so a naive grep
# of this test file does not flag the literal forbidden word.
_SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "api" + "_key",
    "secret",
    "password",
    "credential",
    "authorization",
    "access" + "_token",
    "bearer" + "_token",
)


# ===========================================================================
# 1) default registry contains only the mock provider
# ===========================================================================
def test_default_registry_contains_only_mock_provider():
    """default_registry() must expose exactly the mock provider and no
    real provider, and must fail in a controlled way for an unknown
    provider name.
    """
    registry = default_registry()

    # The mock provider resolves.
    adapter = registry.get(MOCK_PROVIDER_NAME)
    assert isinstance(adapter, MockProviderAdapter)
    assert adapter.provider_name() == MOCK_PROVIDER_NAME

    # Only the mock is registered.
    assert registry.provider_names() == (MOCK_PROVIDER_NAME,)
    assert registry.has(MOCK_PROVIDER_NAME) is True

    # No real provider is registered.
    for real in ("openai", "anthropic", "gemini", "local"):
        assert registry.has(real) is False

    # An unknown provider raises a controlled, testable ValueError.
    with pytest.raises(ValueError):
        registry.get("definitely-not-a-provider")


# ===========================================================================
# 2) mock provider success is deterministic and marked mock
# ===========================================================================
def test_mock_provider_success_is_deterministic_and_marked_mock():
    """Two invocations of the same request must yield identical
    results; the result must be a succeeded, mock-marked, deterministic
    ProviderResult.
    """
    adapter = MockProviderAdapter()
    # Pin the idempotency key so the two requests are truly identical.
    request = _build_request(idempotency_key="idem-fixed-deterministic")

    result_a = adapter.invoke(request)
    result_b = adapter.invoke(request)

    assert result_a.status == PROVIDER_STATUS_SUCCEEDED
    assert result_a.is_mock is True
    assert result_a.error is None

    # Determinism: every observable field matches across invocations.
    assert result_a.content_text == result_b.content_text
    assert result_a.response_hash == result_b.response_hash
    assert result_a.structured_payload == result_b.structured_payload
    assert result_a.usage == result_b.usage
    assert result_a.latency_ms == result_b.latency_ms

    # Content is deterministic and non-empty.
    assert result_a.content_text
    assert isinstance(result_a.content_text, str)

    # Usage is deterministic and explicitly mock.
    assert result_a.usage.is_mock is True
    assert result_a.usage.cost_estimate == Decimal("0")
    assert result_a.usage.tokens_input >= 1
    assert result_a.usage.tokens_output >= 1

    # latency must not depend on wall-clock time: it is the fixed mock
    # value, so it is identical and non-negative.
    assert result_a.latency_ms >= 0


# ===========================================================================
# 3) request and response hashes are stable and canonical
# ===========================================================================
def test_request_and_response_hashes_are_stable_and_canonical():
    """stable_hash must be insensitive to dict key order;
    build_request_hash must be equal for equivalent requests;
    build_response_hash must be stable.
    """
    # canonical_json / stable_hash are order-insensitive.
    payload_one = {"b": 2, "a": 1, "nested": {"y": [3, 4], "x": 1}}
    payload_two = {"nested": {"x": 1, "y": [3, 4]}, "a": 1, "b": 2}
    assert canonical_json(payload_one) == canonical_json(payload_two)
    assert stable_hash(payload_one) == stable_hash(payload_two)

    # Two requests with the same logical content (built with the same
    # pinned idempotency key) hash identically, even though the dict
    # fields were constructed in a different insertion order.
    req_a = _build_request(
        idempotency_key="idem-hash-test",
        constraints={"max_len": 10, "format": "text"},
    )
    req_b = _build_request(
        idempotency_key="idem-hash-test",
        constraints={"format": "text", "max_len": 10},
    )
    assert build_request_hash(req_a) == build_request_hash(req_b)

    # A response hash is stable across repeated calls on equal data.
    structured = {"k": "v", "n": 7, "list": [1, 2, 3]}
    assert build_response_hash(structured) == build_response_hash(
        {"list": [1, 2, 3], "n": 7, "k": "v"}
    )

    # A different request hashes differently.
    req_c = _build_request(
        idempotency_key="idem-hash-test-different",
        constraints={"format": "text", "max_len": 10},
    )
    assert build_request_hash(req_c) != build_request_hash(req_a)


# ===========================================================================
# 4) hash_only redaction does not expose secrets
# ===========================================================================
def test_redaction_hash_only_does_not_expose_secrets():
    """hash_only redaction must yield only a payload hash and the mode,
    never the secret values; legitimate token-count fields must not be
    affected (they are not present in the output at all under
    hash_only, but the hash must still be computable over them).
    """
    payload = {
        "api_key": "sk-supersecret-aaaa",
        "authorization": "Bearer top-secret-bbbb",
        "password": "hunter2-cccc",
        "access_token": "at-secret-dddd",
        # Legitimate fields that must NOT be treated as secrets.
        "max_tokens": 1024,
        "tokens_input": 50,
        "tokens_output": 75,
        "prompt": "an ordinary prompt",
    }
    policy = ProviderRedactionPolicy(mode=REDACTION_MODE_HASH_ONLY)
    out = redact_payload(payload, policy)

    # Output shape: a hash and the mode, nothing else.
    assert set(out.keys()) == {"payload_hash", "redaction_mode"}
    assert out["redaction_mode"] == REDACTION_MODE_HASH_ONLY
    assert isinstance(out["payload_hash"], str)
    assert len(out["payload_hash"]) == 64  # sha256 hex

    # No secret value leaks through anywhere in the serialized output.
    serialized = canonical_json(out)
    for secret in (
        "sk-supersecret-aaaa",
        "top-secret-bbbb",
        "hunter2-cccc",
        "at-secret-dddd",
    ):
        assert secret not in serialized

    # The hash is deterministic for the same payload.
    assert redact_payload(payload, policy)["payload_hash"] == out["payload_hash"]


# ===========================================================================
# 5) redacted_payload mode recursively masks sensitive fields
# ===========================================================================
def test_redacted_payload_mode_recursively_masks_sensitive_fields():
    """redacted_payload mode must recursively mask sensitive fields,
    including inside nested dicts and lists, while preserving
    non-sensitive fields such as max_tokens / tokens_input.
    """
    payload = {
        "api_key": "sk-outer-secret",
        "max_tokens": 256,
        "nested": {
            "secret": "nested-secret-value",
            "credential": "nested-credential-value",
            "tokens_input": 12,
            "deeper": {
                "refresh_token": "rt-deep-secret",
                "ordinary": "keep-me",
            },
        },
        "items": [
            {"password": "list-item-secret", "label": "keep-this-label"},
            {"authorization": "Bearer list-secret"},
        ],
    }
    policy = ProviderRedactionPolicy(mode=REDACTION_MODE_REDACTED_PAYLOAD)
    out = redact_payload(payload, policy)

    assert out["redaction_mode"] == REDACTION_MODE_REDACTED_PAYLOAD
    redacted = out["payload"]

    # Top-level sensitive field masked; legitimate field preserved.
    assert redacted["api_key"] == REDACTED_PLACEHOLDER
    assert redacted["max_tokens"] == 256

    # Nested sensitive fields masked; nested legitimate field kept.
    assert redacted["nested"]["secret"] == REDACTED_PLACEHOLDER
    assert redacted["nested"]["credential"] == REDACTED_PLACEHOLDER
    assert redacted["nested"]["tokens_input"] == 12

    # Deeper nesting is reached.
    assert redacted["nested"]["deeper"]["refresh_token"] == REDACTED_PLACEHOLDER
    assert redacted["nested"]["deeper"]["ordinary"] == "keep-me"

    # Sensitive fields inside list elements are masked; siblings kept.
    assert redacted["items"][0]["password"] == REDACTED_PLACEHOLDER
    assert redacted["items"][0]["label"] == "keep-this-label"
    assert redacted["items"][1]["authorization"] == REDACTED_PLACEHOLDER

    # No raw secret value survives anywhere in the serialized output.
    serialized = canonical_json(out)
    for secret in (
        "sk-outer-secret",
        "nested-secret-value",
        "nested-credential-value",
        "rt-deep-secret",
        "list-item-secret",
        "Bearer list-secret",
    ):
        assert secret not in serialized


# ===========================================================================
# 6) error normalization: retryable and non-retryable
# ===========================================================================
def test_error_normalization_retryable_and_non_retryable():
    """normalize_error must classify retryability per the codomain and
    map an unknown code to unknown_error (non-retryable).
    """
    timeout = normalize_error(ERROR_TIMEOUT, "timed out")
    assert timeout.error_code == ERROR_TIMEOUT
    assert timeout.retryable is True

    rate_limited = normalize_error(ERROR_RATE_LIMITED)
    assert rate_limited.error_code == ERROR_RATE_LIMITED
    assert rate_limited.retryable is True

    auth_failed = normalize_error(ERROR_AUTHENTICATION_FAILED, "bad key")
    assert auth_failed.error_code == ERROR_AUTHENTICATION_FAILED
    assert auth_failed.retryable is False

    invalid = normalize_error(ERROR_INVALID_REQUEST, "malformed")
    assert invalid.error_code == ERROR_INVALID_REQUEST
    assert invalid.retryable is False

    # An unrecognized code collapses to unknown_error, non-retryable.
    unknown = normalize_error("something-not-in-codomain", "weird")
    assert unknown.error_code == ERROR_UNKNOWN
    assert unknown.retryable is False

    # error_message is always a string (None becomes empty string).
    assert normalize_error(ERROR_TIMEOUT).error_message == ""

    # Secrets embedded in a free-text error_message must be masked.
    # The field names may remain; the values must become [REDACTED].
    redacted = normalize_error(
        ERROR_INVALID_REQUEST,
        "api_key=sk-test authorization=Bearer abc password=hunter2",
    )
    assert redacted.error_code == ERROR_INVALID_REQUEST
    assert redacted.retryable is False
    assert "sk-test" not in redacted.error_message
    assert "Bearer abc" not in redacted.error_message
    assert "hunter2" not in redacted.error_message
    assert "[REDACTED]" in redacted.error_message
    # The field names are preserved so the message is still readable.
    assert "api_key" in redacted.error_message
    assert "authorization" in redacted.error_message
    assert "password" in redacted.error_message

    # The ':' separator form is masked too.
    redacted_colon = normalize_error(
        ERROR_TIMEOUT, "call failed: authorization: Bearer secret-xyz"
    )
    assert "secret-xyz" not in redacted_colon.error_message
    assert "[REDACTED]" in redacted_colon.error_message


# ===========================================================================
# 7) mock provider error injection returns a failed result
# ===========================================================================
def test_mock_provider_error_injection_returns_failed_result():
    """A constraints['mock_error_code'] must drive invoke() into a
    failed ProviderResult with the normalized error code, no content,
    is_mock True and no verified source candidates.
    """
    adapter = MockProviderAdapter()
    request = _build_request(constraints={"mock_error_code": ERROR_TIMEOUT})

    result = adapter.invoke(request)

    assert result.status == PROVIDER_STATUS_FAILED
    assert result.error is not None
    assert result.error.error_code == ERROR_TIMEOUT
    assert result.error.retryable is True  # timeout is retryable
    assert result.content_text is None
    assert result.is_mock is True

    # No source candidates, and certainly none verified.
    assert result.source_candidates == ()
    assert all(not c.is_verified for c in result.source_candidates)

    # A failed result still has a stable, deterministic response hash.
    assert result.response_hash
    assert result.response_hash == adapter.invoke(request).response_hash

    # A non-retryable injected error is reflected correctly too.
    request_auth = _build_request(
        constraints={"mock_error_code": ERROR_AUTHENTICATION_FAILED}
    )
    result_auth = adapter.invoke(request_auth)
    assert result_auth.status == PROVIDER_STATUS_FAILED
    assert result_auth.error.error_code == ERROR_AUTHENTICATION_FAILED
    assert result_auth.error.retryable is False


# ===========================================================================
# 8) provider_invocation record mapping: schema shape, no secrets
# ===========================================================================
def test_provider_invocation_record_mapping_matches_schema_shape_and_has_no_secrets():
    """to_provider_invocation_record must produce exactly the schema
    keys, carry is_mock True / provider_name mock / a coherent status,
    include request_hash and response_hash, and contain NO secret
    field.
    """
    adapter = MockProviderAdapter()

    # Success case.
    request = _build_request()
    result = adapter.invoke(request)
    record = to_provider_invocation_record(request, result, attempt_no=1)

    assert set(record.keys()) == _PROVIDER_INVOCATION_KEYS
    assert record["is_mock"] is True
    assert record["provider_name"] == MOCK_PROVIDER_NAME
    assert record["status"] == PROVIDER_STATUS_SUCCEEDED
    assert record["request_hash"]
    assert record["response_hash"]
    assert record["error_code"] is None
    assert record["attempt_no"] == 1
    assert record["redaction_strategy"] == DEFAULT_REDACTION_STRATEGY
    # tenant_id is carried from the request (NOT NULL in schema 0011).
    assert record["tenant_id"] == request.tenant_id

    # No secret-looking key is present.
    for key in record:
        lowered = key.lower()
        for frag in _SECRET_KEY_FRAGMENTS:
            assert frag not in lowered, f"record key {key!r} looks like a secret"

    # Failure case keeps the shape and carries a coherent status.
    request_fail = _build_request(
        constraints={"mock_error_code": ERROR_RATE_LIMITED}
    )
    result_fail = adapter.invoke(request_fail)
    record_fail = to_provider_invocation_record(request_fail, result_fail)
    assert set(record_fail.keys()) == _PROVIDER_INVOCATION_KEYS
    assert record_fail["status"] == PROVIDER_STATUS_FAILED
    assert record_fail["error_code"] == ERROR_RATE_LIMITED
    assert record_fail["is_mock"] is True


# ===========================================================================
# 9) token_usage record mapping supports nullable provider_invocation_id
# ===========================================================================
def test_token_usage_record_mapping_supports_nullable_provider_invocation_id():
    """to_token_usage_record must accept a None provider_invocation_id,
    carry the idempotency key and is_mock True, and report
    non-negative token counts.
    """
    adapter = MockProviderAdapter()
    request = _build_request()
    result = adapter.invoke(request)

    # provider_invocation_id None is accepted (schema allows it).
    record_null = to_token_usage_record(
        request, result, provider_invocation_id=None
    )
    assert set(record_null.keys()) == _TOKEN_USAGE_KEYS
    assert record_null["provider_invocation_id"] is None
    assert record_null["idempotency_key"] == request.idempotency_key
    assert record_null["is_mock"] is True
    assert record_null["tokens_input"] >= 0
    assert record_null["tokens_output"] >= 0
    # tenant_id is carried from the request (NOT NULL in schema 0011).
    assert record_null["tenant_id"] == request.tenant_id

    # provider_invocation_id provided is carried verbatim.
    record_with_id = to_token_usage_record(
        request,
        result,
        provider_invocation_id="invocation-123",
        pass_kind="independent_answer",
    )
    assert record_with_id["provider_invocation_id"] == "invocation-123"
    assert record_with_id["pass_kind"] == "independent_answer"

    # An out-of-codomain pass_kind raises rather than producing a row
    # the DB would reject.
    with pytest.raises(ValueError):
        to_token_usage_record(request, result, pass_kind="not-a-pass-kind")


# ===========================================================================
# 10) source candidates are unverified candidates, not evidence
# ===========================================================================
def test_source_candidates_are_unverified_candidates_not_evidence():
    """Injected mock source candidates must surface as proposed,
    unverified, agent_cited candidates, and their record dicts must
    carry no evidence_span_id and no claim/evidence link.
    """
    adapter = MockProviderAdapter()
    request = _build_request(
        source_policy={
            "mock_source_candidates": [
                {
                    "title": "A cited source",
                    "url": "https://example.invalid/doc",
                    "locator": "page 4",
                },
                {
                    "title": "Another cited source",
                    "url": "https://example.invalid/other",
                    "locator": "section 2",
                },
            ]
        }
    )
    result = adapter.invoke(request)

    assert len(result.source_candidates) == 2
    for cand in result.source_candidates:
        assert cand.status == CANDIDATE_STATUS_PROPOSED
        assert cand.is_verified is False
        assert cand.candidate_type == CANDIDATE_TYPE_AGENT_CITED

    records = source_candidates_to_records(
        request, result, agent_output_id="agent-output-1"
    )
    assert len(records) == 2
    for record in records:
        assert record["status"] == CANDIDATE_STATUS_PROPOSED
        assert record["candidate_type"] == CANDIDATE_TYPE_AGENT_CITED
        assert record["agent_output_id"] == "agent-output-1"
        # Scope columns of source_candidates (0011): tenant_id is
        # NOT NULL, orchestration_run_id and master_prompt_id are
        # nullable. The mock-first module has no master_prompt_id.
        assert record["tenant_id"] == request.tenant_id
        assert record["orchestration_run_id"] == request.orchestration_run_id
        assert record["master_prompt_id"] is None
        # No evidence_span_id, no claim link, no evidence FK fields.
        assert "evidence_span_id" not in record
        assert "claim_id" not in record
        assert "logical_claim_id" not in record
        assert "claim_evidence_link_id" not in record
        assert "claim_ledger_entry_id" not in record
        # The record is keyed only with source_candidates columns.
        assert set(record.keys()) == {
            "tenant_id",
            "orchestration_run_id",
            "master_prompt_id",
            "candidate_type",
            "status",
            "agent_output_id",
            "title",
            "url",
            "citation_text",
            "quoted_text",
            "declared_confidence",
            "provenance",
            "created_by",
            "raw_citation_payload",
        }


# ===========================================================================
# 11) budget preflight blocks an over-budget request
# ===========================================================================
def test_budget_preflight_blocks_over_budget_without_invoking_real_provider():
    """enforce_mock_budget must return a non-retryable budget_exceeded
    error when the estimate exceeds the limit, and None otherwise.
    The adapter's enforce_preflight_budget must behave the same. No
    network, no DB write is involved.
    """
    adapter = MockProviderAdapter()
    request = _build_request(
        system_instructions="word " * 50,
        task_instructions="another word " * 50,
    )
    estimated = count_request_input_tokens(request)
    assert estimated > 1

    # A very low budget is exceeded.
    error = enforce_mock_budget(request, budget_limit_tokens=1)
    assert error is not None
    assert error.error_code == ERROR_BUDGET_EXCEEDED
    assert error.retryable is False

    # The adapter method behaves identically.
    error_via_adapter = adapter.enforce_preflight_budget(request, 1)
    assert error_via_adapter is not None
    assert error_via_adapter.error_code == ERROR_BUDGET_EXCEEDED
    assert error_via_adapter.retryable is False

    # A generous budget is fine.
    assert enforce_mock_budget(request, budget_limit_tokens=10_000) is None

    # A None budget limit is always fine.
    assert enforce_mock_budget(request, budget_limit_tokens=None) is None

    # The request object is not mutated by the preflight check.
    assert count_request_input_tokens(request) == estimated


# ===========================================================================
# 12) module uses no network / no provider SDK imports
# ===========================================================================
def _banned_import_fragments() -> list[str]:
    """Return the list of banned import tokens.

    Each token is assembled from fragments at runtime so that a naive
    ``grep`` of THIS test file for the banned words does not match the
    literal list (the phase prompt §15 warns about exactly this
    self-interception).
    """
    return [
        "re" + "quests",
        "ht" + "tpx",
        "aio" + "http",
        "url" + "lib",
        "soc" + "ket",
        "sub" + "process",
        "open" + "ai",
        "anthro" + "pic",
        "gem" + "ini",
        "google." + "generativeai",
    ]


def test_module_uses_no_network_or_provider_sdk_imports():
    """The service module source must not import any network client or
    provider SDK. We inspect the module source directly.
    """
    source = inspect.getsource(op)
    lowered = source.lower()

    for token in _banned_import_fragments():
        # The token must not appear as an import. We check both an
        # ``import X`` and a ``from X import`` shape.
        assert f"import {token}" not in lowered, (
            f"module must not import {token!r}"
        )
        assert f"from {token}" not in lowered, (
            f"module must not import from {token!r}"
        )

    # The module's own namespace must not expose any of these as a
    # bound module object either.
    module_dict = vars(op)
    for token in _banned_import_fragments():
        top_level = token.split(".")[0]
        assert top_level not in module_dict, (
            f"module namespace must not bind {top_level!r}"
        )

    # Positive control: the module DOES rely only on stdlib modules.
    for allowed in ("hashlib", "json", "uuid"):
        assert allowed in lowered


# ===========================================================================
# 13) mock payload contains a semantic warning
# ===========================================================================
def test_mock_payload_contains_semantic_warning():
    """Every mock result must carry the mock flag and a safe semantic
    warning declaring it a candidate, not evidence, not a final
    answer.
    """
    adapter = MockProviderAdapter()
    request = _build_request()
    result = adapter.invoke(request)

    payload = result.structured_payload
    assert payload.get("mock") is True
    assert payload.get("is_publishable_answer") is False
    assert payload.get("is_evidence") is False

    warning = payload.get("semantic_warning")
    assert isinstance(warning, str) and warning
    lowered = warning.lower()
    assert "candidate" in lowered
    assert "not evidence" in lowered
    assert "not a final answer" in lowered

    # An injected source candidate also carries a safe mock metadata
    # warning marking it unverified.
    request_with_sources = _build_request(
        source_policy={
            "mock_source_candidates": [{"title": "S", "url": "u"}]
        }
    )
    result_with_sources = adapter.invoke(request_with_sources)
    cand = result_with_sources.source_candidates[0]
    assert cand.metadata.get("mock") is True
    assert cand.metadata.get("is_evidence") is False
    assert "unverified" in str(cand.metadata.get("semantic_warning", "")).lower()


# ===========================================================================
# Defensive coverage
# ===========================================================================
def test_capabilities_include_required_set():
    """The mock adapter must declare text, structured_output,
    source_candidates and error_injection.
    """
    caps = set(MockProviderAdapter().capabilities())
    assert ProviderCapability.TEXT in caps
    assert ProviderCapability.STRUCTURED_OUTPUT in caps
    assert ProviderCapability.SOURCE_CANDIDATES in caps
    assert ProviderCapability.ERROR_INJECTION in caps


def test_module_is_importable_without_environment():
    """The service module must import cleanly with no environment,
    no DB, no network — a regression guard for the pure-module
    contract.
    """
    reloaded = importlib.reload(op)
    assert reloaded.MOCK_PROVIDER_NAME == "mock"
    assert reloaded.SERVICE_NAME == "mvp0_mock_provider_adapter"
