"""Mock-first AI provider abstraction (Phase ORCH-PROVIDER-A).

This module is the first code-level block of the multi-AI provider
abstraction designed in PHASE_ORCH_PROVIDER_PRE.md. It implements,
worker-level and mock-first, the logical provider interface, a
deterministic MockProviderAdapter, request/response hashing, payload
redaction, error normalization, mock usage/cost estimation, a mock
preflight budget check, and the pure mapping functions that turn a
provider invocation into dict records shaped for the ORCH-SCHEMA-A
tables ``provider_invocations`` and ``token_usage_records`` (migration
0011_orchestration_schema.sql), plus the extraction of provider source
candidates as *unverified* candidates.

Strict scope (PHASE_ORCH_PROVIDER_A §3, §4):

  - This module is PURE and worker-level. It performs NO network I/O,
    opens NO sockets, imports NO provider SDK, imports NO HTTP client,
    touches NO database, uses NO Redis, imports NO FastAPI.
  - It only produces in-memory objects and plain dicts that a future
    worker could persist. It does NOT write to provider_invocations,
    does NOT write to token_usage_records, does NOT write anywhere.
  - It uses only the Python standard library: abc, dataclasses,
    decimal, enum, hashlib, json, re, typing, uuid.
  - It does NOT introduce a real provider, a real local LLM, real
    source retrieval, real web retrieval, or a parallel gate.

Semantic invariants (PHASE_ORCH_PROVIDER_PRE.md §3, §8, §16, §20;
PHASE_ORCH_PROVIDER_A §5):

  - provider output is NOT truth and is NOT a publishable answer.
  - a provider citation is NOT evidence; a source candidate produced
    by the mock is an unverified candidate (status 'proposed',
    is_verified False) that must be resolved and verified before it
    can contribute to the Final Answer Gate.
  - a candidate synthesis is NOT a published answer; the Final Answer
    Gate remains the only publication authority.
  - request_hash / response_hash serve audit, debug and idempotency;
    they do NOT prove the content.
  - the MockProviderAdapter does NOT produce real intelligence, does
    NOT replace a remote provider, does NOT replace a local LLM. Its
    usage and cost are explicitly marked as mock / simulated.
  - "publication allowed" is not "absolute truth"; "publication held"
    is not "false in the world".

Identity:

    MOCK_PROVIDER_NAME         = "mock"
    MOCK_MODEL_NAME            = "mock-model"
    SERVICE_NAME               = "mvp0_mock_provider_adapter"
    SERVICE_VERSION            = "0.1.0"
    DEFAULT_REDACTION_STRATEGY = "hash_only"
"""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


# ===========================================================================
# Module identity (PHASE_ORCH_PROVIDER_A §7)
# ===========================================================================
MOCK_PROVIDER_NAME = "mock"
MOCK_MODEL_NAME = "mock-model"
SERVICE_NAME = "mvp0_mock_provider_adapter"
SERVICE_VERSION = "0.1.0"
DEFAULT_REDACTION_STRATEGY = "hash_only"


# ===========================================================================
# Constants and codomains (PHASE_ORCH_PROVIDER_A §7)
#
# These mirror the codomains enforced by migration
# 0011_orchestration_schema.sql. They are kept as module constants so
# both this module and its tests can lock them down without grepping
# the SQL.
# ===========================================================================

# provider_invocations.status codomain (0011).
PROVIDER_STATUS_PENDING = "pending"
PROVIDER_STATUS_SUCCEEDED = "succeeded"
PROVIDER_STATUS_FAILED = "failed"
PROVIDER_STATUS_CANCELLED = "cancelled"

PROVIDER_STATUS_VALUES: tuple[str, ...] = (
    PROVIDER_STATUS_PENDING,
    PROVIDER_STATUS_SUCCEEDED,
    PROVIDER_STATUS_FAILED,
    PROVIDER_STATUS_CANCELLED,
)

# Normalized provider error codes (PHASE_ORCH_PROVIDER_PRE.md §9). These
# are NOT schema codomains: provider_invocations.error_code is a free
# TEXT column. They are the normalized set this module emits.
ERROR_TIMEOUT = "timeout"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_AUTHENTICATION_FAILED = "authentication_failed"
ERROR_AUTHORIZATION_FAILED = "authorization_failed"
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERROR_INVALID_REQUEST = "invalid_request"
ERROR_INVALID_MODEL = "invalid_model"
ERROR_CONTENT_FILTER = "content_filter"
ERROR_MALFORMED_RESPONSE = "malformed_response"
ERROR_BUDGET_EXCEEDED = "budget_exceeded"
ERROR_RETRY_EXHAUSTED = "retry_exhausted"
ERROR_NETWORK_ERROR = "network_error"
ERROR_UNKNOWN = "unknown_error"

ERROR_CODE_VALUES: tuple[str, ...] = (
    ERROR_TIMEOUT,
    ERROR_RATE_LIMITED,
    ERROR_AUTHENTICATION_FAILED,
    ERROR_AUTHORIZATION_FAILED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_INVALID_MODEL,
    ERROR_CONTENT_FILTER,
    ERROR_MALFORMED_RESPONSE,
    ERROR_BUDGET_EXCEEDED,
    ERROR_RETRY_EXHAUSTED,
    ERROR_NETWORK_ERROR,
    ERROR_UNKNOWN,
)

# Retryability classification (PHASE_ORCH_PROVIDER_A §7).
RETRYABLE_ERROR_CODES: tuple[str, ...] = (
    ERROR_TIMEOUT,
    ERROR_RATE_LIMITED,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_NETWORK_ERROR,
)

NON_RETRYABLE_ERROR_CODES: tuple[str, ...] = (
    ERROR_AUTHENTICATION_FAILED,
    ERROR_AUTHORIZATION_FAILED,
    ERROR_INVALID_REQUEST,
    ERROR_INVALID_MODEL,
    ERROR_CONTENT_FILTER,
    ERROR_MALFORMED_RESPONSE,
    ERROR_BUDGET_EXCEEDED,
)
# unknown_error and retry_exhausted are non-retryable by default; they
# are deliberately NOT in RETRYABLE_ERROR_CODES.

# source_candidates.candidate_type codomain (0011).
CANDIDATE_TYPE_AGENT_CITED = "agent_cited"
CANDIDATE_TYPE_USER_SUPPLIED = "user_supplied"
CANDIDATE_TYPE_SYSTEM_RETRIEVED = "system_retrieved"
CANDIDATE_TYPE_INTERNAL = "internal"
CANDIDATE_TYPE_FUTURE_WEB = "future_web"

CANDIDATE_TYPE_VALUES: tuple[str, ...] = (
    CANDIDATE_TYPE_AGENT_CITED,
    CANDIDATE_TYPE_USER_SUPPLIED,
    CANDIDATE_TYPE_SYSTEM_RETRIEVED,
    CANDIDATE_TYPE_INTERNAL,
    CANDIDATE_TYPE_FUTURE_WEB,
)

# source_candidates.status codomain (0011).
CANDIDATE_STATUS_PROPOSED = "proposed"
CANDIDATE_STATUS_RESOLUTION_PENDING = "resolution_pending"
CANDIDATE_STATUS_RESOLVED = "resolved"
CANDIDATE_STATUS_RESOLUTION_FAILED = "resolution_failed"
CANDIDATE_STATUS_VERIFICATION_PENDING = "verification_pending"
CANDIDATE_STATUS_VERIFIED_AS_RETRIEVED = "verified_as_retrieved"
CANDIDATE_STATUS_REJECTED = "rejected"
CANDIDATE_STATUS_INSUFFICIENT_METADATA = "insufficient_metadata"

CANDIDATE_STATUS_VALUES: tuple[str, ...] = (
    CANDIDATE_STATUS_PROPOSED,
    CANDIDATE_STATUS_RESOLUTION_PENDING,
    CANDIDATE_STATUS_RESOLVED,
    CANDIDATE_STATUS_RESOLUTION_FAILED,
    CANDIDATE_STATUS_VERIFICATION_PENDING,
    CANDIDATE_STATUS_VERIFIED_AS_RETRIEVED,
    CANDIDATE_STATUS_REJECTED,
    CANDIDATE_STATUS_INSUFFICIENT_METADATA,
)

# Redaction modes (PHASE_ORCH_PROVIDER_PRE.md §15).
REDACTION_MODE_HASH_ONLY = "hash_only"
REDACTION_MODE_REDACTED_PAYLOAD = "redacted_payload"
REDACTION_MODE_NO_RAW_PAYLOAD = "no_raw_payload"

REDACTION_MODE_VALUES: tuple[str, ...] = (
    REDACTION_MODE_HASH_ONLY,
    REDACTION_MODE_REDACTED_PAYLOAD,
    REDACTION_MODE_NO_RAW_PAYLOAD,
)

# Token usage pass kinds (token_usage_records.pass_kind codomain, 0011).
PASS_KIND_VALUES: tuple[str, ...] = (
    "independent_answer",
    "reviewer",
    "critic",
    "synthesis",
    "second_check",
    "source_resolution",
)

# Sensitive field name fragments. A payload key is redacted when its
# lowercased name equals or contains one of these fragments. The list
# is deliberately conservative: legitimate fields such as max_tokens,
# tokens_input, tokens_output are NOT in it and are NOT redacted.
SENSITIVE_FIELD_NAMES: tuple[str, ...] = (
    "api_key",
    "secret",
    "authorization",
    "password",
    "credential",
    "access_token",
    "refresh_token",
    "bearer_token",
    "auth_token",
)

# Redaction placeholder for masked sensitive values.
REDACTED_PLACEHOLDER = "[REDACTED]"

# Stable semantic-warning string embedded into every mock payload and
# every mock source candidate so a downstream reader can tell the
# output is NOT real intelligence and NOT evidence.
MOCK_SEMANTIC_WARNING = (
    "mvp0 mock provider output; candidate only, not evidence, "
    "not a final answer, not absolute truth"
)

# Maximum length of a normalized / redacted error message before it is
# truncated. Keeps an error_message bounded for the audit table.
_ERROR_MESSAGE_MAX_LEN = 500


# ===========================================================================
# Data contracts (PHASE_ORCH_PROVIDER_A §8)
#
# Frozen dataclasses wherever possible. No Pydantic, no SQLAlchemy, no
# shared schemas.
# ===========================================================================


@dataclass(frozen=True)
class ProviderMessage:
    """A single message in a provider request."""

    role: str
    content: str


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Retry configuration for a provider invocation."""

    max_attempts: int = 1
    retryable_error_codes: tuple[str, ...] = RETRYABLE_ERROR_CODES
    backoff_ms: int = 0


@dataclass(frozen=True)
class ProviderTimeoutPolicy:
    """Timeout configuration for a provider invocation."""

    timeout_ms: int = 30000


@dataclass(frozen=True)
class ProviderRedactionPolicy:
    """Redaction configuration for provider payloads.

    ``strategy`` is the identity recorded in
    provider_invocations.redaction_strategy; ``mode`` selects how a
    payload is redacted (see redact_payload).
    """

    strategy: str = DEFAULT_REDACTION_STRATEGY
    mode: str = REDACTION_MODE_HASH_ONLY


@dataclass(frozen=True)
class ProviderUsage:
    """Token/cost consumption reported by a provider invocation.

    ``cost_estimate`` is kept as a Decimal so it is exact and
    JSON-stable (canonical_json renders Decimal as a string).
    ``is_mock`` must be True whenever the usage was simulated.
    """

    tokens_input: int
    tokens_output: int
    cost_estimate: Decimal
    is_mock: bool


@dataclass(frozen=True)
class ProviderError:
    """A normalized provider error."""

    error_code: str
    error_message: str
    retryable: bool


@dataclass(frozen=True)
class ProviderSourceCandidate:
    """A source proposed/cited by a provider.

    It is an UNVERIFIED candidate: ``status`` is 'proposed' and
    ``is_verified`` is False. A provider citation is not evidence.
    """

    candidate_type: str = CANDIDATE_TYPE_AGENT_CITED
    status: str = CANDIDATE_STATUS_PROPOSED
    title: str | None = None
    url: str | None = None
    locator: str | None = None
    raw_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_verified: bool = False


@dataclass(frozen=True)
class ProviderRequest:
    """A structured, redactable, hashable request to a provider adapter."""

    tenant_id: str | None
    project_id: str | None
    orchestration_run_id: str | None
    orchestration_agent_run_id: str | None
    agent_config_snapshot_id: str | None
    provider_name: str
    model: str
    messages: tuple[ProviderMessage, ...]
    system_instructions: str | None
    task_instructions: str | None
    output_contract: dict[str, Any]
    constraints: dict[str, Any]
    source_policy: dict[str, Any]
    max_tokens: int | None
    temperature_like_config: dict[str, Any]
    timeout_policy: ProviderTimeoutPolicy
    retry_policy: ProviderRetryPolicy
    redaction_policy: ProviderRedactionPolicy
    idempotency_key: str
    is_mock_expected: bool


@dataclass(frozen=True)
class ProviderResult:
    """The normalized response of a provider invocation."""

    status: str
    content_text: str | None
    structured_payload: dict[str, Any]
    source_candidates: tuple[ProviderSourceCandidate, ...]
    usage: ProviderUsage
    latency_ms: int
    response_hash: str
    raw_response_redacted: dict[str, Any]
    error: ProviderError | None
    is_mock: bool


# ===========================================================================
# Capabilities enum (PHASE_ORCH_PROVIDER_A §10)
# ===========================================================================


class ProviderCapability(str, enum.Enum):
    """Declared capabilities of a provider adapter."""

    TEXT = "text"
    STRUCTURED_OUTPUT = "structured_output"
    SOURCE_CANDIDATES = "source_candidates"
    ERROR_INJECTION = "error_injection"


# ===========================================================================
# Pure helper functions (PHASE_ORCH_PROVIDER_A §9)
# ===========================================================================


def _to_jsonable(value: Any) -> Any:
    """Recursively convert a value into a JSON-stable structure.

    - Decimal -> string (so the value is exact and order-stable).
    - dataclass instance -> dict of its fields.
    - enum -> its value.
    - tuple/list -> list (handled element-wise).
    - dict -> dict with string keys (handled element-wise).
    - uuid.UUID -> string.
    - other primitives -> unchanged.

    This is the single normalization seam used by canonical_json so
    hashing is stable across dict key order and across dataclass vs
    dict representations of the same logical value.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        # Render Decimal as a string: a float conversion would not be
        # exact and would not be order-stable.
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return _to_jsonable(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    # Fallback: stringify anything exotic so canonical_json never
    # raises. This keeps hashing total without inventing structure.
    return str(value)


def canonical_json(value: Any) -> str:
    """Return a canonical JSON string for ``value``.

    json.dumps with sort_keys=True and the most compact separators, so
    that two structures that are equal as data produce the identical
    string regardless of dict key insertion order. Decimal values are
    converted to strings, dataclasses to dicts, tuples/lists handled
    stably. ensure_ascii=False keeps non-ASCII text readable.
    """
    return json.dumps(
        _to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def stable_hash(value: Any) -> str:
    """Return the sha256 hex digest of canonical_json(value).

    Deterministic and stable across dict key order. The hash is for
    audit, debug and idempotency; it does NOT prove the content.
    """
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_sensitive_field(name: str) -> bool:
    """Return True iff a payload key name looks sensitive.

    A name is sensitive when, lowercased, it equals or contains one of
    SENSITIVE_FIELD_NAMES. Legitimate fields such as max_tokens,
    tokens_input, tokens_output do not match any fragment.
    """
    lowered = str(name).lower()
    return any(frag in lowered for frag in SENSITIVE_FIELD_NAMES)


def _redact_recursive(value: Any) -> Any:
    """Recursively mask sensitive fields inside a structure.

    Used by redact_payload for REDACTION_MODE_REDACTED_PAYLOAD. A dict
    key whose name is sensitive has its value replaced wholesale by
    REDACTED_PLACEHOLDER (the nested content is not walked, so a
    secret nested under a sensitive key cannot leak). Non-sensitive
    keys are preserved and their values walked recursively.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_field(k):
                out[str(k)] = REDACTED_PLACEHOLDER
            else:
                out[str(k)] = _redact_recursive(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_recursive(v) for v in value]
    return value


def redact_payload(payload: Any, policy: ProviderRedactionPolicy) -> Any:
    """Redact ``payload`` according to ``policy.mode``.

    - hash_only: return {"payload_hash": <hash>, "redaction_mode":
      "hash_only"}. No payload content is kept, only its hash.
    - no_raw_payload: return {"redaction_mode": "no_raw_payload"}.
      No payload, no hash.
    - redacted_payload: return the payload with sensitive fields
      recursively masked with REDACTED_PLACEHOLDER, non-sensitive
      fields preserved.

    Any other mode is treated as no_raw_payload (the safest default):
    the module never lets an unrecognized mode leak a raw payload.
    """
    mode = policy.mode
    if mode == REDACTION_MODE_HASH_ONLY:
        return {
            "payload_hash": stable_hash(payload),
            "redaction_mode": REDACTION_MODE_HASH_ONLY,
        }
    if mode == REDACTION_MODE_REDACTED_PAYLOAD:
        return {
            "redaction_mode": REDACTION_MODE_REDACTED_PAYLOAD,
            "payload": _redact_recursive(_to_jsonable(payload)),
        }
    # no_raw_payload, and any unrecognized mode, fall through here.
    return {"redaction_mode": REDACTION_MODE_NO_RAW_PAYLOAD}


def _is_retryable(error_code: str) -> bool:
    """Return the retryability of a normalized error code.

    timeout / rate_limited / provider_unavailable / network_error are
    retryable; everything else (including unknown_error and
    retry_exhausted) is not.
    """
    return error_code in RETRYABLE_ERROR_CODES


# Regex that matches a sensitive ``name<sep>value`` fragment inside free
# text. The sensitive field name is one of SENSITIVE_FIELD_NAMES; the
# separator is '=' or ':' optionally surrounded by spaces; the value is
# the run of non-whitespace characters that follows.
#
# The value optionally absorbs ONE trailing word so a two-token secret
# like ``Bearer abc123`` is masked whole. That trailing word is guarded
# by a negative lookahead: it is NOT absorbed when it is itself the
# start of another sensitive ``name<sep>`` fragment, so adjacent
# sensitive fields are each masked independently rather than one
# swallowing the next.
_SENSITIVE_NAME_ALT = "|".join(
    re.escape(frag) for frag in SENSITIVE_FIELD_NAMES
)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?P<name>" + _SENSITIVE_NAME_ALT + r")"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\S+"
    r"(?:\s+(?!(?:" + _SENSITIVE_NAME_ALT + r")\s*[:=])\S+)?"
    r")",
    re.IGNORECASE,
)


def _redact_sensitive_text(text: str) -> str:
    """Mask sensitive ``name=value`` / ``name: value`` fragments in text.

    The field name and the separator are preserved; the value is
    replaced with REDACTED_PLACEHOLDER. This catches secrets that leak
    into a free-text error_message such as a provider error string.

    The value capture deliberately also absorbs one optional trailing
    word so a two-token secret like ``Bearer abc123`` is masked whole
    (``authorization: Bearer abc123`` -> ``authorization: [REDACTED]``).
    A plain single-token value (``api_key=sk-test``) is masked too.
    Standard library ``re`` only; no network, no SDK.
    """
    if not text:
        return text

    def _sub(match: "re.Match[str]") -> str:
        return f"{match.group('name')}{match.group('sep')}{REDACTED_PLACEHOLDER}"

    return _SENSITIVE_TEXT_RE.sub(_sub, text)


def _safe_error_message(error_message: object | None) -> str:
    """Return a redaction-safe, length-bounded error message.

    Steps, in order:
      - None  -> "".
      - str   -> used as-is.
      - other -> str(value) (defensive: a non-str slipped in).
    The text is then run through _redact_sensitive_text so a secret
    embedded as ``name=value`` / ``name: value`` is masked, stripped of
    surrounding whitespace, and truncated to _ERROR_MESSAGE_MAX_LEN.
    """
    if error_message is None:
        text = ""
    elif isinstance(error_message, str):
        text = error_message
    else:
        # Defensive: a non-str slipped in. Stringify, do not trust.
        text = str(error_message)
    text = _redact_sensitive_text(text)
    text = text.strip()
    if len(text) > _ERROR_MESSAGE_MAX_LEN:
        text = text[:_ERROR_MESSAGE_MAX_LEN] + "...[truncated]"
    return text


def normalize_error(
    error_code: str, error_message: str | None = None
) -> ProviderError:
    """Normalize a raw error into a ProviderError.

    An unrecognized error_code is mapped to unknown_error. The
    error_message is made redaction-safe and length-bounded. The
    retryable flag follows the codomain classification.
    """
    code = error_code if error_code in ERROR_CODE_VALUES else ERROR_UNKNOWN
    message = _safe_error_message(error_message)
    return ProviderError(
        error_code=code,
        error_message=message,
        retryable=_is_retryable(code),
    )


def estimate_mock_tokens(text: str) -> int:
    """Deterministically estimate a mock token count for ``text``.

    A simple local heuristic: the number of whitespace-separated words,
    with a floor of 1. This is NOT real token accounting and must
    never be presented as such; it exists only to give the mock a
    deterministic, explainable usage figure.
    """
    if not text:
        return 1
    return max(1, len(text.split()))


def count_request_input_tokens(request: ProviderRequest) -> int:
    """Deterministically estimate the input-token count of a request.

    The estimate sums the mock token count of the system instructions,
    the task instructions, and every message's content.

    output_contract / constraints / source_policy are deliberately
    EXCLUDED from this count: they are structural configuration of the
    request, not natural-language content sent to a model, and
    including them would conflate request shape with prompt length.
    They still participate in build_request_hash so the request
    identity stays complete; they just do not inflate the token
    estimate. This is a documented, deterministic choice.
    """
    total = 0
    if request.system_instructions:
        total += estimate_mock_tokens(request.system_instructions)
    if request.task_instructions:
        total += estimate_mock_tokens(request.task_instructions)
    for msg in request.messages:
        total += estimate_mock_tokens(msg.content)
    return max(1, total)


def _request_hash_view(request: ProviderRequest) -> dict[str, Any]:
    """Return the stable, deterministic view of a request used for
    hashing.

    Only fields that are part of the logical identity of the request
    are included. Nothing time-dependent or environment-dependent is
    included. The redaction policy contributes only its identity
    fields (strategy, mode), which are themselves deterministic.
    """
    return {
        "tenant_id": request.tenant_id,
        "project_id": request.project_id,
        "orchestration_run_id": request.orchestration_run_id,
        "orchestration_agent_run_id": request.orchestration_agent_run_id,
        "agent_config_snapshot_id": request.agent_config_snapshot_id,
        "provider_name": request.provider_name,
        "model": request.model,
        "messages": [
            {"role": m.role, "content": m.content} for m in request.messages
        ],
        "system_instructions": request.system_instructions,
        "task_instructions": request.task_instructions,
        "output_contract": request.output_contract,
        "constraints": request.constraints,
        "source_policy": request.source_policy,
        "max_tokens": request.max_tokens,
        "temperature_like_config": request.temperature_like_config,
        "timeout_policy": {"timeout_ms": request.timeout_policy.timeout_ms},
        "retry_policy": {
            "max_attempts": request.retry_policy.max_attempts,
            "retryable_error_codes": list(
                request.retry_policy.retryable_error_codes
            ),
            "backoff_ms": request.retry_policy.backoff_ms,
        },
        "redaction_policy": {
            "strategy": request.redaction_policy.strategy,
            "mode": request.redaction_policy.mode,
        },
        "idempotency_key": request.idempotency_key,
        "is_mock_expected": request.is_mock_expected,
    }


def build_request_hash(request: ProviderRequest) -> str:
    """Return a stable hash of a ProviderRequest.

    Deterministic: it does not include wall-clock time, random values,
    or anything environment-dependent. Two equivalent requests produce
    the same hash regardless of dict key insertion order.
    """
    return stable_hash(_request_hash_view(request))


def build_response_hash(payload: Any) -> str:
    """Return a stable hash of a response payload.

    Deterministic and order-stable, on the same basis as stable_hash.
    """
    return stable_hash(payload)


# ===========================================================================
# Budget preflight (PHASE_ORCH_PROVIDER_A §12)
# ===========================================================================


def enforce_mock_budget(
    request: ProviderRequest,
    *,
    budget_limit_tokens: int | None,
) -> ProviderError | None:
    """Mock preflight budget check.

    Rules:
      - budget_limit_tokens is None -> OK (return None).
      - estimated input tokens > budget_limit_tokens -> return a
        ProviderError(error_code='budget_exceeded', retryable=False).
      - otherwise -> OK (return None).

    This function does NOT write events, does NOT write
    token_usage_records, does NOT mutate the request, does NOT touch a
    database or a network. It is pure.
    """
    if budget_limit_tokens is None:
        return None
    estimated = count_request_input_tokens(request)
    if estimated > budget_limit_tokens:
        return normalize_error(
            ERROR_BUDGET_EXCEEDED,
            (
                f"mock preflight budget check: estimated input tokens "
                f"{estimated} exceed budget limit {budget_limit_tokens}"
            ),
        )
    return None


# ===========================================================================
# Provider adapter interface (PHASE_ORCH_PROVIDER_A §10)
# ===========================================================================


class ProviderAdapter(ABC):
    """Logical, uniform interface toward a concrete provider.

    Concrete adapters implement provider_name, supported_models,
    capabilities, invoke and parse_response. The remaining methods have
    sensible base implementations that the mock adapter reuses.

    A ProviderAdapter never writes to a database (persistence is the
    caller's job), never decides publishability, never verifies
    sources, never opens a network connection from this module.
    """

    @abstractmethod
    def provider_name(self) -> str:
        """Return the opaque provider name."""

    @abstractmethod
    def supported_models(self) -> tuple[str, ...]:
        """Return the model identifiers this adapter supports."""

    @abstractmethod
    def capabilities(self) -> tuple[ProviderCapability, ...]:
        """Return the declared capabilities of this adapter."""

    def build_request(self, **kwargs: Any) -> ProviderRequest:
        """Build a ProviderRequest from keyword arguments.

        The base implementation simply forwards kwargs to the
        ProviderRequest constructor, applying defaults for the policy
        fields when they are omitted. A future remote adapter could
        override this to plug in provider-specific defaults.
        """
        kwargs.setdefault("timeout_policy", ProviderTimeoutPolicy())
        kwargs.setdefault("retry_policy", ProviderRetryPolicy())
        kwargs.setdefault("redaction_policy", ProviderRedactionPolicy())
        kwargs.setdefault("messages", ())
        kwargs.setdefault("output_contract", {})
        kwargs.setdefault("constraints", {})
        kwargs.setdefault("source_policy", {})
        kwargs.setdefault("temperature_like_config", {})
        return ProviderRequest(**kwargs)

    def estimate_usage(self, request: ProviderRequest) -> ProviderUsage:
        """Estimate the usage of a request before invocation.

        The base implementation produces a mock estimate: input tokens
        from count_request_input_tokens, zero output tokens (not yet
        produced), zero cost, is_mock True. A future remote adapter
        would override this with a real estimate.
        """
        return ProviderUsage(
            tokens_input=count_request_input_tokens(request),
            tokens_output=0,
            cost_estimate=Decimal("0"),
            is_mock=True,
        )

    def enforce_preflight_budget(
        self, request: ProviderRequest, budget_limit_tokens: int | None
    ) -> ProviderError | None:
        """Run the preflight budget check for a request.

        Base implementation delegates to enforce_mock_budget. Returns
        None when the request is within budget, or a normalized
        budget_exceeded ProviderError when it is not.
        """
        return enforce_mock_budget(
            request, budget_limit_tokens=budget_limit_tokens
        )

    @abstractmethod
    def invoke(self, request: ProviderRequest) -> ProviderResult:
        """Execute the invocation and return a ProviderResult."""

    @abstractmethod
    def parse_response(self, raw: Any, request: ProviderRequest) -> ProviderResult:
        """Normalize a raw provider response into a ProviderResult."""

    def normalize_error(
        self, error_code: str, error_message: str | None = None
    ) -> ProviderError:
        """Normalize an error. Base implementation delegates to the
        module-level normalize_error.
        """
        return normalize_error(error_code, error_message)


# ===========================================================================
# MockProviderAdapter (PHASE_ORCH_PROVIDER_A §10)
# ===========================================================================


class MockProviderAdapter(ProviderAdapter):
    """Deterministic, no-network mock provider adapter.

    Behaviour:
      - provider_name() == "mock".
      - supported_models() contains "mock-model".
      - capabilities() include text, structured_output,
        source_candidates and error_injection.
      - invoke() is deterministic: the same request always yields the
        same result. It does NOT use the network, and does NOT use
        wall-clock time for any content or hash.
      - status is 'succeeded' unless an error is injected; 'failed'
        when an error is injected.
      - is_mock is always True.
      - usage is computed deterministically; cost_estimate is "0".
      - source candidates always carry status 'proposed' and
        is_verified False.

    Error injection:
      If request.constraints contains {"mock_error_code": "<code>"},
      invoke() returns a failed ProviderResult with the normalized
      error code.

    Source candidate injection:
      If request.source_policy contains {"mock_source_candidates":
      [ {"title": ..., "url": ..., "locator": ...}, ... ]}, invoke()
      returns those as ProviderSourceCandidate tuples, each with
      candidate_type 'agent_cited', status 'proposed', is_verified
      False, and a safe mock metadata dict.

    The mock provider does NOT produce real intelligence, does NOT
    replace a remote provider, does NOT replace a local LLM, and does
    NOT render a response publishable.
    """

    # A fixed, deterministic mock latency. Not derived from wall-clock
    # time, so invoke() is fully reproducible.
    _MOCK_LATENCY_MS = 0

    def provider_name(self) -> str:
        return MOCK_PROVIDER_NAME

    def supported_models(self) -> tuple[str, ...]:
        return (MOCK_MODEL_NAME,)

    def capabilities(self) -> tuple[ProviderCapability, ...]:
        return (
            ProviderCapability.TEXT,
            ProviderCapability.STRUCTURED_OUTPUT,
            ProviderCapability.SOURCE_CANDIDATES,
            ProviderCapability.ERROR_INJECTION,
        )

    # -- internal helpers ---------------------------------------------------

    def _mock_content_text(self, request: ProviderRequest) -> str:
        """Build a deterministic mock content string for a request.

        The string is derived only from the request hash, so it is
        reproducible and never depends on wall-clock time.
        """
        digest = build_request_hash(request)
        return (
            f"[mock-provider-output] deterministic candidate text for "
            f"request {digest[:16]}. This is mock output: a candidate, "
            f"not evidence, not a final answer."
        )

    def _mock_structured_payload(
        self, request: ProviderRequest
    ) -> dict[str, Any]:
        """Build the deterministic structured payload of a mock result.

        Carries the mock flag and the semantic warning so a downstream
        consumer can detect that the output is mock-driven, a candidate
        only, and not a publishable answer.
        """
        return {
            "mock": True,
            "semantic_warning": MOCK_SEMANTIC_WARNING,
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "provider_name": self.provider_name(),
            "model": request.model,
            "request_hash": build_request_hash(request),
            "kind": "provider_candidate_output",
            "is_publishable_answer": False,
            "is_evidence": False,
        }

    def _extract_injected_source_candidates(
        self, request: ProviderRequest
    ) -> tuple[ProviderSourceCandidate, ...]:
        """Build ProviderSourceCandidate tuples from source_policy.

        Reads request.source_policy["mock_source_candidates"], if
        present, and turns each entry into an unverified candidate.
        Every candidate carries status 'proposed', is_verified False,
        candidate_type 'agent_cited', and a safe mock metadata dict
        with the semantic warning. Malformed entries are skipped
        defensively rather than raising.
        """
        raw = request.source_policy.get("mock_source_candidates")
        if not isinstance(raw, list):
            return ()
        candidates: list[ProviderSourceCandidate] = []
        for entry in raw:
            if not isinstance(entry, dict):
                # Skip anything that is not a dict: the mock never
                # fabricates a candidate from a malformed entry.
                continue
            candidates.append(
                ProviderSourceCandidate(
                    candidate_type=CANDIDATE_TYPE_AGENT_CITED,
                    status=CANDIDATE_STATUS_PROPOSED,
                    title=entry.get("title"),
                    url=entry.get("url"),
                    locator=entry.get("locator"),
                    raw_text=entry.get("raw_text"),
                    metadata={
                        "mock": True,
                        "semantic_warning": (
                            "unverified provider source candidate; "
                            "must be resolved and verified before it "
                            "can contribute to the Final Answer Gate"
                        ),
                        "is_evidence": False,
                    },
                    is_verified=False,
                )
            )
        return tuple(candidates)

    def _failed_result(
        self,
        request: ProviderRequest,
        *,
        error: ProviderError,
    ) -> ProviderResult:
        """Build a deterministic failed ProviderResult for an injected
        or preflight error.
        """
        structured = {
            "mock": True,
            "semantic_warning": MOCK_SEMANTIC_WARNING,
            "service_name": SERVICE_NAME,
            "service_version": SERVICE_VERSION,
            "provider_name": self.provider_name(),
            "model": request.model,
            "request_hash": build_request_hash(request),
            "kind": "provider_failed_invocation",
            "error_code": error.error_code,
            "is_publishable_answer": False,
            "is_evidence": False,
        }
        usage = ProviderUsage(
            # A failed invocation still has a minimal, deterministic
            # mock input-token figure; output tokens are zero.
            tokens_input=count_request_input_tokens(request),
            tokens_output=0,
            cost_estimate=Decimal("0"),
            is_mock=True,
        )
        redacted = redact_payload(structured, request.redaction_policy)
        return ProviderResult(
            status=PROVIDER_STATUS_FAILED,
            content_text=None,
            structured_payload=structured,
            source_candidates=(),
            usage=usage,
            latency_ms=self._MOCK_LATENCY_MS,
            response_hash=build_response_hash(structured),
            raw_response_redacted=redacted,
            error=error,
            is_mock=True,
        )

    # -- public interface ---------------------------------------------------

    def invoke(self, request: ProviderRequest) -> ProviderResult:
        """Execute a deterministic mock invocation.

        No network, no wall-clock-derived content or hash. The result
        is fully reproducible for a given request.
        """
        # Error injection: a constraints["mock_error_code"] triggers a
        # failed result with the normalized error code.
        injected_code = request.constraints.get("mock_error_code")
        if injected_code is not None:
            error = self.normalize_error(
                str(injected_code),
                f"mock injected error: {injected_code}",
            )
            return self._failed_result(request, error=error)

        # Success path.
        content_text = self._mock_content_text(request)
        structured = self._mock_structured_payload(request)
        candidates = self._extract_injected_source_candidates(request)
        usage = ProviderUsage(
            tokens_input=count_request_input_tokens(request),
            tokens_output=estimate_mock_tokens(content_text),
            cost_estimate=Decimal("0"),
            is_mock=True,
        )
        redacted = redact_payload(structured, request.redaction_policy)
        return ProviderResult(
            status=PROVIDER_STATUS_SUCCEEDED,
            content_text=content_text,
            structured_payload=structured,
            source_candidates=candidates,
            usage=usage,
            latency_ms=self._MOCK_LATENCY_MS,
            response_hash=build_response_hash(structured),
            raw_response_redacted=redacted,
            error=None,
            is_mock=True,
        )

    def parse_response(
        self, raw: Any, request: ProviderRequest
    ) -> ProviderResult:
        """Normalize a raw mock response into a ProviderResult.

        For the mock adapter the canonical raw response IS a
        ProviderResult produced by invoke(); parse_response simply
        returns it unchanged when handed one, and otherwise treats the
        raw value as a malformed response.
        """
        if isinstance(raw, ProviderResult):
            return raw
        error = self.normalize_error(
            ERROR_MALFORMED_RESPONSE,
            "mock parse_response received a non-ProviderResult raw value",
        )
        return self._failed_result(request, error=error)


# ===========================================================================
# Provider registry (PHASE_ORCH_PROVIDER_A §10)
# ===========================================================================


class ProviderRegistry:
    """A minimal, static, in-memory registry of provider adapters.

    The registry holds NO secrets, performs NO calls, and decides NO
    publishability. It is a simple name -> adapter map.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        """Register an adapter under its provider_name().

        Re-registering the same provider name overwrites the previous
        adapter; this keeps the registry simple and predictable for a
        static MVP-0 catalogue.
        """
        self._adapters[adapter.provider_name()] = adapter

    def get(self, provider_name: str) -> ProviderAdapter:
        """Return the adapter registered for ``provider_name``.

        Raises ValueError for an unknown provider. The error is a
        controlled, testable failure: no real provider is ever
        silently substituted.
        """
        try:
            return self._adapters[provider_name]
        except KeyError:
            raise ValueError(
                f"unknown provider {provider_name!r}; registered providers: "
                f"{sorted(self._adapters)}"
            ) from None

    def has(self, provider_name: str) -> bool:
        """Return True iff ``provider_name`` is registered."""
        return provider_name in self._adapters

    def provider_names(self) -> tuple[str, ...]:
        """Return the sorted tuple of registered provider names."""
        return tuple(sorted(self._adapters))


def default_registry() -> ProviderRegistry:
    """Return a fresh registry containing only the MockProviderAdapter.

    In MVP-0 the only operational provider is the mock. No real
    provider is registered.
    """
    registry = ProviderRegistry()
    registry.register(MockProviderAdapter())
    return registry


# ===========================================================================
# Schema record mapping (PHASE_ORCH_PROVIDER_A §11)
#
# Pure functions: they build dicts shaped for the ORCH-SCHEMA-A tables.
# They do NOT write to any database.
# ===========================================================================


def _decimal_to_safe(value: Decimal) -> str:
    """Render a Decimal as a stable string for a record dict.

    A record dict is meant to be persisted later by a worker; keeping
    cost as a string keeps it exact and JSON-stable.
    """
    return str(value)


def to_provider_invocation_record(
    request: ProviderRequest,
    result: ProviderResult,
    *,
    attempt_no: int = 1,
) -> dict[str, Any]:
    """Build a dict shaped for the provider_invocations table (0011).

    The dict contains exactly the schema-relevant keys, including
    ``tenant_id`` (provider_invocations.tenant_id is NOT NULL in
    migration 0011_orchestration_schema.sql). It NEVER contains an API
    key, an Authorization header, a password, a credential, a secret,
    a raw unredacted request or a raw unredacted response. request_hash
    and response_hash are present for audit.

    This function does NOT write to the database; it returns a plain
    dict a future worker can persist.
    """
    error_code = result.error.error_code if result.error is not None else None
    error_message = (
        result.error.error_message if result.error is not None else None
    )
    return {
        "tenant_id": request.tenant_id,
        "orchestration_run_id": request.orchestration_run_id,
        "agent_run_id": request.orchestration_agent_run_id,
        "provider_name": request.provider_name,
        "model": request.model,
        "request_hash": build_request_hash(request),
        "response_hash": result.response_hash,
        "status": result.status,
        "error_code": error_code,
        "error_message": error_message,
        "tokens_input": result.usage.tokens_input,
        "tokens_output": result.usage.tokens_output,
        "cost_estimate": _decimal_to_safe(result.usage.cost_estimate),
        "latency_ms": result.latency_ms,
        "attempt_no": attempt_no,
        "is_mock": result.is_mock,
        "redaction_strategy": request.redaction_policy.strategy,
        "idempotency_key": request.idempotency_key,
    }


def to_token_usage_record(
    request: ProviderRequest,
    result: ProviderResult,
    *,
    provider_invocation_id: str | None = None,
    pass_kind: str | None = None,
    attempt_no: int = 1,
) -> dict[str, Any]:
    """Build a dict shaped for the token_usage_records table (0011).

    The dict includes ``tenant_id`` (token_usage_records.tenant_id is
    NOT NULL in migration 0011_orchestration_schema.sql).
    provider_invocation_id may be None: the ORCH-SCHEMA-A schema allows
    it and has dedicated partial UNIQUE indexes for the NULL and
    NOT NULL cases. This function does NOT write to the database.

    pass_kind, when provided, is validated against the schema codomain;
    an unrecognized value raises ValueError so a typo surfaces here
    rather than at INSERT time.
    """
    if pass_kind is not None and pass_kind not in PASS_KIND_VALUES:
        raise ValueError(
            f"pass_kind {pass_kind!r} is not in the schema codomain "
            f"{PASS_KIND_VALUES!r}"
        )
    return {
        "tenant_id": request.tenant_id,
        "orchestration_run_id": request.orchestration_run_id,
        "agent_run_id": request.orchestration_agent_run_id,
        "provider_invocation_id": provider_invocation_id,
        "pass_kind": pass_kind,
        "tokens_input": result.usage.tokens_input,
        "tokens_output": result.usage.tokens_output,
        "cost_estimate": _decimal_to_safe(result.usage.cost_estimate),
        "attempt_no": attempt_no,
        "is_mock": result.is_mock,
        "idempotency_key": request.idempotency_key,
    }


def source_candidates_to_records(
    request: ProviderRequest,
    result: ProviderResult,
    *,
    agent_output_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build dicts conceptually compatible with source_candidates (0011).

    Each dict uses the real column names of the source_candidates table
    as defined in migration 0011_orchestration_schema.sql, including
    ``tenant_id`` (NOT NULL in 0011) and the nullable
    ``orchestration_run_id`` / ``master_prompt_id`` scope columns:
      tenant_id, orchestration_run_id, master_prompt_id, candidate_type,
      status, agent_output_id, title, url, citation_text, quoted_text,
      declared_confidence, provenance, created_by, raw_citation_payload.

    master_prompt_id is set to None here: a provider invocation is
    scoped to an orchestration run, and this mock-first module has no
    master_prompt_id on its inputs. A future worker that does have one
    can populate it; the schema column is nullable.

    The ProviderSourceCandidate model carries title / url / locator /
    raw_text; this function maps locator and raw_text into the schema
    fields that exist: ``locator`` is carried inside provenance (the
    0011 schema has no dedicated locator column), and ``raw_text`` is
    mapped to ``citation_text``. quoted_text is left None: the mock
    does not assert a verified quote.

    Each record carries NO evidence_span_id, NO claim link and NO FK
    toward evidence_spans: a provider source candidate is an unverified
    candidate, never evidence. status is always 'proposed' and the
    metadata records that the candidate is unverified.

    This function does NOT write to the database.
    """
    records: list[dict[str, Any]] = []
    for cand in result.source_candidates:
        provenance: dict[str, Any] = {
            "mock": True,
            "semantic_warning": (
                "unverified provider source candidate; not evidence"
            ),
            "provider_name": request.provider_name,
            "model": request.model,
            # The ProviderSourceCandidate.locator has no dedicated
            # column in source_candidates (0011); it is carried here
            # inside provenance so no information is lost. Documented
            # in the implementation report as a conservative choice.
            "locator": cand.locator,
            "is_verified": cand.is_verified,
        }
        # Fold any candidate-level metadata into provenance without
        # letting it overwrite the keys above.
        for k, v in (cand.metadata or {}).items():
            provenance.setdefault(f"candidate_metadata_{k}", v)
        records.append(
            {
                "tenant_id": request.tenant_id,
                "orchestration_run_id": request.orchestration_run_id,
                "master_prompt_id": None,
                "candidate_type": cand.candidate_type,
                "status": cand.status,
                "agent_output_id": agent_output_id,
                "title": cand.title,
                "url": cand.url,
                "citation_text": cand.raw_text,
                "quoted_text": None,
                "declared_confidence": None,
                "provenance": provenance,
                "created_by": request.provider_name,
                "raw_citation_payload": {
                    "mock": True,
                    "title": cand.title,
                    "url": cand.url,
                    "locator": cand.locator,
                    "raw_text": cand.raw_text,
                },
            }
        )
    return records
