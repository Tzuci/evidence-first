"""Pure-logic base of the source resolution pass (Phase ORCH-MULTI-B1).

This module is the FIRST, pure-logic-only slice of the mock/local-only
source resolution pass designed in PHASE_ORCH_MULTI_B_PRE.md. It defines the
request/result contracts, the deterministic value objects, and the pure
functions that a later DB-backed pass (ORCH-MULTI-B2) will compose. It does
NOT write to a database, does NOT open a connection, does NOT invoke a
provider, opens NO socket, imports NO FastAPI, NO Redis, NO HTTP client and
NO provider SDK. It uses only the Python standard library.

Strict scope (PHASE_ORCH_MULTI_B_PRE.md §1, §8, §16):

  - NO network, NO HTTP, NO browser, NO real provider, NO real retrieval.
  - NO source verification, NO ``source_verifications`` row.
  - NO ``evidence_spans``, NO ``claim_evidence_links``, NO claim binding.
  - NO Final Answer Gate, NO ``final_gate_reports``, NO ``published_answers``.
  - NO UPDATE of ``source_candidates``: ``source_candidates`` is append-only;
    the current state of a candidate is DERIVED from its latest
    ``source_resolutions`` row (see ``_derive_candidate_state``), never read
    from a mutated ``source_candidates.status``.

Semantic invariants (PHASE_ORCH_MULTI_B_PRE.md §4):

  - source_candidate is NOT evidence.
  - a provider citation/source is NOT a verified source.
  - source resolution is NOT source verification.
  - a resolved source is NOT an evidence span and is NOT a publishable claim.
  - provider output is NOT a final answer.
  - an ``outcome='resolved'`` means ONLY that the candidate target was
    located / normalized locally per the resolver policy. It does NOT mean
    the source was retrieved, checked, linked to a claim, or approved by the
    gate. A proposed candidate stays an unverified proposal even after it is
    "resolved".

Redaction note:

  - ``orchestration_provider._safe_error_message`` is a private helper; rather
    than couple this module to a private cross-module symbol, this module
    keeps its own small, self-contained redaction helper with the same
    conceptual behaviour (mask ``name=value`` / ``name: value`` fragments,
    bound the length). No other file is read or imported to obtain it.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


# ===========================================================================
# Constants — real codomains of migration 0011_orchestration_schema.sql
# ===========================================================================

# Subset of source_candidates.status reused for the DERIVED candidate state.
# These strings are NEVER written onto source_candidates by this pass.
SOURCE_CANDIDATE_STATUS_PROPOSED = "proposed"
SOURCE_CANDIDATE_STATUS_RESOLUTION_PENDING = "resolution_pending"
SOURCE_CANDIDATE_STATUS_RESOLVED = "resolved"
SOURCE_CANDIDATE_STATUS_RESOLUTION_FAILED = "resolution_failed"
SOURCE_CANDIDATE_STATUS_INSUFFICIENT_METADATA = "insufficient_metadata"

# source_resolutions.outcome codomain (0011).
RESOLUTION_OUTCOME_RESOLVED = "resolved"
RESOLUTION_OUTCOME_FAILED = "failed"
RESOLUTION_OUTCOME_INSUFFICIENT_METADATA = "insufficient_metadata"
RESOLUTION_OUTCOME_PARTIAL = "partial"
RESOLUTION_OUTCOME_UNREACHABLE = "unreachable"
RESOLUTION_OUTCOME_NOT_FOUND = "not_found"

RESOLUTION_OUTCOME_VALUES: tuple[str, ...] = (
    RESOLUTION_OUTCOME_RESOLVED,
    RESOLUTION_OUTCOME_FAILED,
    RESOLUTION_OUTCOME_INSUFFICIENT_METADATA,
    RESOLUTION_OUTCOME_PARTIAL,
    RESOLUTION_OUTCOME_UNREACHABLE,
    RESOLUTION_OUTCOME_NOT_FOUND,
)

# source_resolutions.resolution_target_kind codomain (0011).
RESOLUTION_TARGET_KIND_URL = "url"
RESOLUTION_TARGET_KIND_WEB_PAGE = "web_page"
RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT = "internal_document"
RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT = "uploaded_document"
RESOLUTION_TARGET_KIND_RETRIEVED_DOCUMENT = "retrieved_document"

RESOLUTION_TARGET_KIND_VALUES: tuple[str, ...] = (
    RESOLUTION_TARGET_KIND_URL,
    RESOLUTION_TARGET_KIND_WEB_PAGE,
    RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT,
    RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT,
    RESOLUTION_TARGET_KIND_RETRIEVED_DOCUMENT,
)

# orchestration_events.event_type values this pass is allowed to emit (0011).
EVENT_SOURCE_RESOLUTION_STARTED = "source_resolution_started"
EVENT_SOURCE_RESOLUTION_COMPLETED = "source_resolution_completed"

# token_usage_records.pass_kind value reserved for this pass (0011). Not used
# by the pure-logic slice; declared here for the later DB-backed pass.
PASS_KIND_SOURCE_RESOLUTION = "source_resolution"

# Publication is never evaluated by this pass (§4).
PUBLICATION_STATUS_NOT_EVALUATED = "not_evaluated"

# Candidate-selection scopes (§7).
SELECTION_SCOPE_PER_RUN = "per_run"
SELECTION_SCOPE_PER_AGENT_OUTPUT = "per_agent_output"

# Bounded default (§13). The exact value is an implementation constant.
MAX_CANDIDATES_DEFAULT = 32

# Bound for a persisted/returned failure_reason.
_MAX_FAILURE_REASON_LEN = 500

# Sensitive field-name fragments masked inside a free-text failure_reason.
_SENSITIVE_FIELD_NAMES: tuple[str, ...] = (
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

_REDACTED_PLACEHOLDER = "[REDACTED]"

# A name<sep>value fragment where the name is sensitive, the separator is
# '=' or ':' (optionally spaced) and the value is the following run of
# non-whitespace characters. Conservative single-token masking is enough for
# the bounded reasons this module produces; it never builds a two-token secret.
_SENSITIVE_TEXT_RE = re.compile(
    r"(?P<name>(?:" + "|".join(re.escape(f) for f in _SENSITIVE_FIELD_NAMES) + r"))"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\S+)",
    re.IGNORECASE,
)


# ===========================================================================
# Contracts (PHASE_ORCH_MULTI_B_PRE.md §5, §6)
# ===========================================================================


@dataclass(frozen=True)
class SourceResolutionPassRequest:
    """Logical request for one source resolution pass over a run's candidates.

    No secret travels in this contract: no API key, no authentication token,
    no credential, no Authorization header.
    """

    tenant_id: str
    orchestration_run_id: str
    idempotency_key: str
    max_candidates: int = MAX_CANDIDATES_DEFAULT
    candidate_selection_scope: str = SELECTION_SCOPE_PER_RUN
    agent_output_id: str | None = None
    eligible_states: tuple[str, ...] = (SOURCE_CANDIDATE_STATUS_PROPOSED,)
    created_by: str | None = None


@dataclass(frozen=True)
class SourceResolutionPassResult:
    """Logical result describing the outcome of the pass and persisted ids.

    ``publication_status`` is always ``not_evaluated`` and ``gate_report_id``
    is always ``None``: the Final Answer Gate is not executed by this pass.
    """

    status: str
    orchestration_run_id: str | None
    source_resolution_ids: tuple[str, ...]
    per_candidate_outcomes: dict[str, str]
    event_ids: tuple[str, ...]
    counters: dict[str, int]
    publication_status: str = PUBLICATION_STATUS_NOT_EVALUATED
    gate_report_id: str | None = None


@dataclass(frozen=True)
class CandidateResolutionDecision:
    """The deterministic decision the mock/local-only policy makes for one
    candidate: which target kind it is, the honest outcome, and an optional
    bounded/redacted failure reason."""

    resolution_target_kind: str
    outcome: str
    failure_reason: str | None = None


# ===========================================================================
# Redaction helper (self-contained; same behaviour as the provider's seam)
# ===========================================================================


def _redact_failure_reason(message: object | None) -> str | None:
    """Return a redaction-safe, length-bounded failure reason, or None.

    None stays None. Otherwise the value is stringified defensively, sensitive
    ``name=value`` / ``name: value`` fragments are masked with
    ``[REDACTED]``, the text is stripped and truncated to a bounded length.
    """
    if message is None:
        return None
    text = message if isinstance(message, str) else str(message)
    text = _SENSITIVE_TEXT_RE.sub(
        lambda m: f"{m.group('name')}{m.group('sep')}{_REDACTED_PLACEHOLDER}",
        text,
    )
    text = text.strip()
    if len(text) > _MAX_FAILURE_REASON_LEN:
        text = text[:_MAX_FAILURE_REASON_LEN] + "...[truncated]"
    return text


# ===========================================================================
# Pure helpers (PHASE_ORCH_MULTI_B_PRE.md §7, §8, §9, §11)
# ===========================================================================


def _coerce_mapping(value: Any) -> Mapping[str, Any]:
    """Coerce a JSONB-backed value into a read-only mapping, defensively.

    A psycopg JSONB column is typically a dict already; a None or any other
    type yields an empty mapping rather than raising.
    """
    if isinstance(value, Mapping):
        return value
    return {}


def _present(mapping: Mapping[str, Any], key: str) -> bool:
    """Return True iff ``key`` is present in ``mapping`` with a non-empty value.

    Empty string / None / empty container count as absent so a placeholder key
    does not masquerade as a real local marker.
    """
    if key not in mapping:
        return False
    value = mapping[key]
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value) or value == 0


def _is_external_http_url(value: Any) -> bool:
    """Return True iff ``value`` is a string that looks like an external
    http(s) URL. The mock/local-only pass cannot reach the network, so such a
    target can never be honestly reported as resolved."""
    if not isinstance(value, str):
        return False
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _derive_candidate_state(latest_resolution: Mapping[str, Any] | None) -> str:
    """Derive the current state of a candidate from its latest resolution.

    The candidate row is NEVER mutated; this is a read-only view (§9):

      - None (no resolution exists yet)            -> proposed
      - outcome == resolved                        -> resolved
      - outcome == insufficient_metadata           -> insufficient_metadata
      - outcome in {failed, unreachable,
                    not_found, partial}            -> resolution_failed
      - any other / unknown outcome                -> resolution_failed
        (defensive: an unexpected value is treated as a non-success rather
        than silently surfacing as resolved)
    """
    if latest_resolution is None:
        return SOURCE_CANDIDATE_STATUS_PROPOSED
    outcome = latest_resolution.get("outcome")
    if outcome == RESOLUTION_OUTCOME_RESOLVED:
        return SOURCE_CANDIDATE_STATUS_RESOLVED
    if outcome == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA:
        return SOURCE_CANDIDATE_STATUS_INSUFFICIENT_METADATA
    if outcome in (
        RESOLUTION_OUTCOME_FAILED,
        RESOLUTION_OUTCOME_UNREACHABLE,
        RESOLUTION_OUTCOME_NOT_FOUND,
        RESOLUTION_OUTCOME_PARTIAL,
    ):
        return SOURCE_CANDIDATE_STATUS_RESOLUTION_FAILED
    # Unknown / unexpected outcome: never optimistic.
    return SOURCE_CANDIDATE_STATUS_RESOLUTION_FAILED


def _classify_candidate(candidate: Mapping[str, Any]) -> CandidateResolutionDecision:
    """Apply the mock/local-only resolution policy to one candidate.

    Deterministic and honest (§8). No network, no HTTP, no provider:

      1. An explicit local-document marker in raw_citation_payload or
         provenance => the target is located/normalized locally => resolved:
           - uploaded_document_id / document_id -> uploaded_document, resolved
           - internal_document_id               -> internal_document, resolved
         (resolved here means "target individuated and normalized locally",
         NOT "verified" and NOT "evidence".)
      2. An external http(s) URL => url, unreachable. The mock pass does not
         reach the network, so it never claims such a URL is resolved.
      3. A non-empty but non-http url-like locator, or a locator the policy
         does not support => url, insufficient_metadata, bounded reason.
      4. Nothing usable (no marker, no url, no locator) => url,
         insufficient_metadata, bounded reason.
    """
    raw = _coerce_mapping(candidate.get("raw_citation_payload"))
    prov = _coerce_mapping(candidate.get("provenance"))

    # 1) explicit local document markers => resolved (local normalization only)
    if (
        _present(raw, "uploaded_document_id")
        or _present(prov, "uploaded_document_id")
        or _present(raw, "document_id")
        or _present(prov, "document_id")
    ):
        return CandidateResolutionDecision(
            resolution_target_kind=RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT,
            outcome=RESOLUTION_OUTCOME_RESOLVED,
            failure_reason=None,
        )
    if _present(raw, "internal_document_id") or _present(prov, "internal_document_id"):
        return CandidateResolutionDecision(
            resolution_target_kind=RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT,
            outcome=RESOLUTION_OUTCOME_RESOLVED,
            failure_reason=None,
        )

    # 2) external URL => unreachable in mock/local-only mode (never resolved).
    url = candidate.get("url")
    if _is_external_http_url(url):
        return CandidateResolutionDecision(
            resolution_target_kind=RESOLUTION_TARGET_KIND_URL,
            outcome=RESOLUTION_OUTCOME_UNREACHABLE,
            failure_reason=None,
        )

    # 3) a non-empty but non-http url string => unsupported locator.
    if isinstance(url, str) and url.strip():
        return CandidateResolutionDecision(
            resolution_target_kind=RESOLUTION_TARGET_KIND_URL,
            outcome=RESOLUTION_OUTCOME_INSUFFICIENT_METADATA,
            failure_reason=_redact_failure_reason(
                "unsupported non-http locator for mock/local-only policy"
            ),
        )

    # 3b) a declared locator (carried in provenance/raw) the policy can't use.
    locator = prov.get("locator")
    if not (isinstance(locator, str) and locator.strip()):
        locator = raw.get("locator")
    if isinstance(locator, str) and locator.strip():
        return CandidateResolutionDecision(
            resolution_target_kind=RESOLUTION_TARGET_KIND_URL,
            outcome=RESOLUTION_OUTCOME_INSUFFICIENT_METADATA,
            failure_reason=_redact_failure_reason(
                "locator present but unsupported by mock/local-only policy"
            ),
        )

    # 4) nothing usable.
    return CandidateResolutionDecision(
        resolution_target_kind=RESOLUTION_TARGET_KIND_URL,
        outcome=RESOLUTION_OUTCOME_INSUFFICIENT_METADATA,
        failure_reason=_redact_failure_reason(
            "insufficient candidate metadata: no usable locator"
        ),
    )


def _stable_candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return the deterministic sort key for a candidate (§7).

    Orders by (agent_output_id, created_at, id), each coerced to a string with
    an empty-string fallback so a NULL column never breaks the ordering and the
    sort never depends on DB row order.
    """

    def _s(key: str) -> str:
        value = candidate.get(key)
        return "" if value is None else str(value)

    return (_s("agent_output_id"), _s("created_at"), _s("id"))


def _build_candidate_scoped_idempotency_key(
    base_key: str, event_or_resolution_kind: str, candidate_id: str
) -> str:
    """Compose a candidate-scoped idempotency key (§10, §11).

    Includes the pass base key, the event/resolution kind, and the candidate
    id, so the same kind repeated across candidates never collides on
    ``orchestration_events_run_type_idem_uq`` and a replay recomputes the same
    key.
    """
    return f"{base_key}:{event_or_resolution_kind}:{candidate_id}"


def _build_initial_counters() -> dict[str, int]:
    """Return the zeroed counters dict the pass aggregates into (§6)."""
    return {
        "candidates_seen": 0,
        "candidates_attempted": 0,
        "resolved_count": 0,
        "failed_count": 0,
        "insufficient_metadata_count": 0,
        "skipped_count": 0,
    }


def _increment_counters_for_outcome(counters: dict[str, int], outcome: str) -> None:
    """Increment the outcome-specific counter for one resolution outcome (§6).

    failed_count covers the non-success, non-insufficient outcomes
    {failed, unreachable, not_found}; ``partial`` is also bucketed here so the
    counts stay consistent with the derived ``resolution_failed`` state. The
    seen/attempted/skipped counters are managed by the (future) pass loop, not
    here.
    """
    if outcome == RESOLUTION_OUTCOME_RESOLVED:
        counters["resolved_count"] += 1
    elif outcome == RESOLUTION_OUTCOME_INSUFFICIENT_METADATA:
        counters["insufficient_metadata_count"] += 1
    elif outcome in (
        RESOLUTION_OUTCOME_FAILED,
        RESOLUTION_OUTCOME_UNREACHABLE,
        RESOLUTION_OUTCOME_NOT_FOUND,
        RESOLUTION_OUTCOME_PARTIAL,
    ):
        counters["failed_count"] += 1
    # An unknown outcome increments no specific bucket; the pass should never
    # produce one, and silently inventing a bucket would hide a bug.


# ===========================================================================
# DB-backed pass — stub (implemented in ORCH-MULTI-B2)
# ===========================================================================


def run_source_resolution_pass(
    conn: Any, request: SourceResolutionPassRequest
) -> SourceResolutionPassResult:
    """Run the mock/local-only source resolution pass over a run's candidates.

    Not implemented in this pure-logic slice: the DB-backed pass (candidate
    selection, append-only ``source_resolutions`` writes, event emission,
    idempotent replay) is added in ORCH-MULTI-B2.
    """
    raise NotImplementedError(
        "DB-backed source resolution pass is implemented in ORCH-MULTI-B2"
    )
