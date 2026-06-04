"""Source resolution pass: pure logic (ORCH-MULTI-B1) + DB-backed pass
(ORCH-MULTI-B2).

This module is the mock/local-only source resolution pass designed in
PHASE_ORCH_MULTI_B_PRE.md. ORCH-MULTI-B1 introduced the request/result
contracts, the deterministic value objects, and the pure functions; this
ORCH-MULTI-B2 slice composes them into the DB-backed
``run_source_resolution_pass``. It does NOT open its own connection, does NOT
commit and does NOT rollback: the caller owns the transaction. It opens NO
socket, invokes NO provider, imports NO FastAPI, NO Redis, NO HTTP client and
NO provider SDK. Beyond the Python standard library it uses only SQLAlchemy's
``text`` / ``Connection`` to talk to the caller-owned connection.

Strict scope (PHASE_ORCH_MULTI_B_PRE.md §1, §8, §16):

  - NO network, NO HTTP, NO browser, NO real provider, NO real retrieval.
  - NO source verification, NO ``source_verifications`` row.
  - NO ``evidence_spans``, NO ``claim_evidence_links``, NO claim binding.
  - NO Final Answer Gate, NO ``final_gate_reports``, NO ``published_answers``.
  - NO ``token_usage_records`` written by this pass.
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

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


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
# by this pass; declared here for a future usage-recording phase.
PASS_KIND_SOURCE_RESOLUTION = "source_resolution"

# Publication is never evaluated by this pass (§4).
PUBLICATION_STATUS_NOT_EVALUATED = "not_evaluated"

# Synthetic pass result-status values (§6, §12).
RESULT_STATUS_SUCCEEDED = "succeeded"
RESULT_STATUS_FAILED = "failed"

# Candidate-selection scopes (§7).
SELECTION_SCOPE_PER_RUN = "per_run"
SELECTION_SCOPE_PER_AGENT_OUTPUT = "per_agent_output"

# Discriminant kind used to compose the resolution idempotency key (§11).
RESOLUTION_IDEMPOTENCY_KIND = "source_resolution"

# Related-entity type tag written on orchestration_events.related_entity_type.
RELATED_SOURCE_CANDIDATE = "source_candidate"

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
    text_value = message if isinstance(message, str) else str(message)
    text_value = _SENSITIVE_TEXT_RE.sub(
        lambda m: f"{m.group('name')}{m.group('sep')}{_REDACTED_PLACEHOLDER}",
        text_value,
    )
    text_value = text_value.strip()
    if len(text_value) > _MAX_FAILURE_REASON_LEN:
        text_value = text_value[:_MAX_FAILURE_REASON_LEN] + "...[truncated]"
    return text_value


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


def _derive_current_candidate_state(
    candidate: Mapping[str, Any],
    latest_resolution: Mapping[str, Any] | None,
) -> str:
    """Derive a candidate's current state, honouring its initial status (§9).

    When a resolution exists, the latest resolution is authoritative and the
    derived state comes from it. When NO resolution exists yet, the candidate's
    own ``status`` is authoritative: source_candidates is append-only, so this
    is the row's *initial* status (never a mutated one). A candidate seeded as
    e.g. ``rejected`` or ``insufficient_metadata`` with no resolution must NOT
    be treated as ``proposed`` and silently picked up by the pass.

    Falls back to ``proposed`` only when the candidate carries no usable status.
    """
    if latest_resolution is not None:
        return _derive_candidate_state(latest_resolution)
    return str(candidate.get("status") or SOURCE_CANDIDATE_STATUS_PROPOSED)


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
# DB-backed pass — validation and result builders (ORCH-MULTI-B2)
# ===========================================================================


def _validate_pass_request(request: SourceResolutionPassRequest) -> str | None:
    """Validate the request without any DB access (§1).

    Returns an error string on the first problem, or None when the request is
    well-formed. A controlled validation failure makes the pass return a failed
    result that wrote nothing, never an uncaught exception.
    """
    if not request.tenant_id or not str(request.tenant_id).strip():
        return "tenant_id must be a non-empty string"
    if not request.orchestration_run_id or not str(request.orchestration_run_id).strip():
        return "orchestration_run_id must be a non-empty string"
    if not request.idempotency_key or not str(request.idempotency_key).strip():
        return "idempotency_key must be a non-empty string"
    if not isinstance(request.max_candidates, int) or request.max_candidates <= 0:
        return "max_candidates must be a positive integer"
    if request.candidate_selection_scope not in (
        SELECTION_SCOPE_PER_RUN,
        SELECTION_SCOPE_PER_AGENT_OUTPUT,
    ):
        return (
            "candidate_selection_scope must be "
            f"{SELECTION_SCOPE_PER_RUN!r} or {SELECTION_SCOPE_PER_AGENT_OUTPUT!r}"
        )
    if request.candidate_selection_scope == SELECTION_SCOPE_PER_AGENT_OUTPUT and (
        not request.agent_output_id or not str(request.agent_output_id).strip()
    ):
        return "agent_output_id is required when scope is per_agent_output"
    return None


def _failed_pass_result(
    request: SourceResolutionPassRequest,
) -> SourceResolutionPassResult:
    """Build a failed pass result that wrote nothing (§1, §2)."""
    return SourceResolutionPassResult(
        status=RESULT_STATUS_FAILED,
        orchestration_run_id=request.orchestration_run_id or None,
        source_resolution_ids=(),
        per_candidate_outcomes={},
        event_ids=(),
        counters=_build_initial_counters(),
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )


# ===========================================================================
# DB-backed pass — DB read/write helpers (ORCH-MULTI-B2)
# ===========================================================================


def _select_run(conn: Connection, tenant_id: str, run_id: str) -> Mapping[str, Any] | None:
    """Confirm the run exists AND belongs to the tenant (§2)."""
    return (
        conn.execute(
            text(
                "SELECT id FROM orchestration_runs "
                "WHERE id = :run_id AND tenant_id = :tenant_id"
            ),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        .mappings()
        .first()
    )


def _select_scoped_candidates(
    conn: Connection, request: SourceResolutionPassRequest
) -> list[dict[str, Any]]:
    """Select candidates in scope, ordered deterministically (§3, §7).

    Tenant + run scoped, optionally further filtered by agent_output_id; sorted
    in memory by ``_stable_candidate_sort_key`` so the DB row order never leaks
    into the result.
    """
    sql = (
        "SELECT id, agent_output_id, status, created_at, url, provenance, "
        "raw_citation_payload "
        "FROM source_candidates "
        "WHERE tenant_id = :tenant_id AND orchestration_run_id = :run_id"
    )
    params: dict[str, Any] = {
        "tenant_id": request.tenant_id,
        "run_id": request.orchestration_run_id,
    }
    if request.candidate_selection_scope == SELECTION_SCOPE_PER_AGENT_OUTPUT:
        sql += " AND agent_output_id = :agent_output_id"
        params["agent_output_id"] = request.agent_output_id
    rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]
    rows.sort(key=_stable_candidate_sort_key)
    return rows


def _next_sequence_no(conn: Connection, run_id: str) -> int:
    """Compute the next sequence_no in the run's existing event space (§4).

    ``COALESCE(MAX(sequence_no), -1) + 1`` so a run with no events starts at 0
    and an existing run continues contiguously; never restarts from 0.
    """
    value = conn.execute(
        text(
            "SELECT COALESCE(MAX(sequence_no), -1) + 1 "
            "FROM orchestration_events WHERE orchestration_run_id = :run_id"
        ),
        {"run_id": run_id},
    ).scalar()
    return int(value if value is not None else 0)


def _latest_resolution(
    conn: Connection, candidate_id: str
) -> Mapping[str, Any] | None:
    """Return the candidate's latest resolution (created_at DESC, id DESC) (§3, §9)."""
    return (
        conn.execute(
            text(
                "SELECT outcome FROM source_resolutions "
                "WHERE source_candidate_id = :cid "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"cid": candidate_id},
        )
        .mappings()
        .first()
    )


def _existing_resolution(
    conn: Connection, candidate_id: str, resolution_idem: str
) -> Mapping[str, Any] | None:
    """Return an existing resolution for (candidate, idempotency_key), if any (§7)."""
    return (
        conn.execute(
            text(
                "SELECT id, outcome FROM source_resolutions "
                "WHERE source_candidate_id = :cid AND idempotency_key = :idem"
            ),
            {"cid": candidate_id, "idem": resolution_idem},
        )
        .mappings()
        .first()
    )


def _existing_event_id(
    conn: Connection, run_id: str, event_type: str, idem: str
) -> str | None:
    """Return the id of an already-persisted event with this key, if any (§7)."""
    value = conn.execute(
        text(
            "SELECT id FROM orchestration_events "
            "WHERE orchestration_run_id = :run_id AND event_type = :et "
            "AND idempotency_key = :idem"
        ),
        {"run_id": run_id, "et": event_type, "idem": idem},
    ).scalar()
    return str(value) if value is not None else None


def _insert_resolution_event(
    conn: Connection,
    *,
    run_id: str,
    event_type: str,
    sequence_no: int,
    idem: str,
    candidate_id: str,
    payload: dict[str, Any],
) -> str:
    """Insert one source_resolution_* event in the run's sequence space (§5)."""
    event_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO orchestration_events
                (id, orchestration_run_id, event_type, sequence_no,
                 related_entity_type, related_entity_id, event_payload,
                 idempotency_key)
            VALUES
                (:id, :run_id, :et, :seq, :ret, :rei, CAST(:payload AS JSONB),
                 :idem)
            """
        ),
        {
            "id": event_id,
            "run_id": run_id,
            "et": event_type,
            "seq": sequence_no,
            "ret": RELATED_SOURCE_CANDIDATE,
            "rei": candidate_id,
            "payload": json.dumps(payload or {}),
            "idem": idem,
        },
    )
    return event_id


def _insert_source_resolution(
    conn: Connection,
    *,
    resolution_id: str,
    candidate_id: str,
    run_id: str,
    decision: CandidateResolutionDecision,
    idem: str,
) -> None:
    """Insert one append-only source_resolutions row (§6).

    retrieved_artifact_ref / retrieved_artifact_hash are NULL (no retrieval);
    created_at defaults to NOW() in the DB; no source_verifications,
    evidence_spans or claim_evidence_links are written.
    """
    conn.execute(
        text(
            """
            INSERT INTO source_resolutions
                (id, source_candidate_id, orchestration_run_id,
                 resolution_target_kind, outcome, failure_reason,
                 retrieved_artifact_ref, retrieved_artifact_hash,
                 idempotency_key)
            VALUES
                (:id, :cid, :run_id, :tk, :outcome, :fr, NULL, NULL, :idem)
            """
        ),
        {
            "id": resolution_id,
            "cid": candidate_id,
            "run_id": run_id,
            "tk": decision.resolution_target_kind,
            "outcome": decision.outcome,
            "fr": decision.failure_reason,
            "idem": idem,
        },
    )


# ===========================================================================
# DB-backed pass entry point (ORCH-MULTI-B2)
# ===========================================================================


def run_source_resolution_pass(
    conn: Connection, request: SourceResolutionPassRequest
) -> SourceResolutionPassResult:
    """Run the mock/local-only source resolution pass over a run's candidates.

    DB-backed, append-only, idempotent and bounded. It writes through the
    caller-owned ``conn`` and does NOT commit and does NOT rollback: the caller
    owns the transaction (§10 "no transaction ownership"). It performs no
    network I/O, invokes no provider, and never touches source_verifications,
    evidence_spans, claim_evidence_links, final_gate_reports or
    published_answers. publication_status stays 'not_evaluated' and
    gate_report_id stays None in every path.
    """
    # 1) Validation (no DB access). A controlled failure returns failed without
    #    writing anything.
    if _validate_pass_request(request) is not None:
        return _failed_pass_result(request)

    # 2) Confirm the run exists and belongs to the tenant. A missing run is a
    #    failed pass with no write.
    if _select_run(conn, request.tenant_id, request.orchestration_run_id) is None:
        return _failed_pass_result(request)

    base_key = request.idempotency_key
    counters = _build_initial_counters()
    source_resolution_ids: list[str] = []
    event_ids: list[str] = []
    per_candidate_outcomes: dict[str, str] = {}

    # 3) Candidate selection: tenant + run (+ optional agent_output) scoped,
    #    deterministic order. candidates_seen counts the scope before the bound.
    candidates = _select_scoped_candidates(conn, request)
    counters["candidates_seen"] = len(candidates)

    # 4) Events append onto the run's existing sequence_no space; never from 0.
    next_sequence_no = _next_sequence_no(conn, request.orchestration_run_id)

    # Bound counter: how many ELIGIBLE candidates we newly process this pass.
    eligible_handled = 0

    for candidate in candidates:
        candidate_id = str(candidate["id"])
        resolution_idem = _build_candidate_scoped_idempotency_key(
            base_key, RESOLUTION_IDEMPOTENCY_KIND, candidate_id
        )
        started_idem = _build_candidate_scoped_idempotency_key(
            base_key, EVENT_SOURCE_RESOLUTION_STARTED, candidate_id
        )
        completed_idem = _build_candidate_scoped_idempotency_key(
            base_key, EVENT_SOURCE_RESOLUTION_COMPLETED, candidate_id
        )

        # 7) Idempotent replay: a resolution already exists for this candidate
        #    under this pass key. Reconstruct ids/outcome; never duplicate.
        existing = _existing_resolution(conn, candidate_id, resolution_idem)
        if existing is not None:
            existing_outcome = str(existing["outcome"])
            source_resolution_ids.append(str(existing["id"]))
            per_candidate_outcomes[candidate_id] = existing_outcome
            counters["candidates_attempted"] += 1
            _increment_counters_for_outcome(counters, existing_outcome)
            started_ev = _existing_event_id(
                conn,
                request.orchestration_run_id,
                EVENT_SOURCE_RESOLUTION_STARTED,
                started_idem,
            )
            completed_ev = _existing_event_id(
                conn,
                request.orchestration_run_id,
                EVENT_SOURCE_RESOLUTION_COMPLETED,
                completed_idem,
            )
            if started_ev is not None:
                event_ids.append(started_ev)
            if completed_ev is not None:
                event_ids.append(completed_ev)
            continue

        # Eligibility from the candidate's CURRENT state: the latest resolution
        # if one exists, otherwise the candidate's own (append-only, never
        # mutated) initial status. A candidate whose initial status is not
        # eligible and has no resolution is skipped, not treated as proposed.
        latest_resolution = _latest_resolution(conn, candidate_id)
        derived_state = _derive_current_candidate_state(candidate, latest_resolution)
        if derived_state not in request.eligible_states:
            counters["skipped_count"] += 1
            continue

        # 3) Bound: process at most max_candidates eligible candidates; eligible
        #    candidates beyond the bound are skipped (left to a later pass).
        if eligible_handled >= request.max_candidates:
            counters["skipped_count"] += 1
            continue
        eligible_handled += 1

        # 8) Honest, deterministic mock/local-only decision.
        decision = _classify_candidate(candidate)

        # 5) source_resolution_started event (reuse if a prior partial write
        #    already created it under the same key; never duplicate).
        started_event_id = _existing_event_id(
            conn,
            request.orchestration_run_id,
            EVENT_SOURCE_RESOLUTION_STARTED,
            started_idem,
        )
        if started_event_id is None:
            started_event_id = _insert_resolution_event(
                conn,
                run_id=request.orchestration_run_id,
                event_type=EVENT_SOURCE_RESOLUTION_STARTED,
                sequence_no=next_sequence_no,
                idem=started_idem,
                candidate_id=candidate_id,
                payload={"source_candidate_id": candidate_id},
            )
            next_sequence_no += 1
        event_ids.append(started_event_id)

        # 6) source_resolutions insert (append-only).
        resolution_id = str(uuid.uuid4())
        _insert_source_resolution(
            conn,
            resolution_id=resolution_id,
            candidate_id=candidate_id,
            run_id=request.orchestration_run_id,
            decision=decision,
            idem=resolution_idem,
        )
        source_resolution_ids.append(resolution_id)

        # source_resolution_completed event.
        completed_event_id = _existing_event_id(
            conn,
            request.orchestration_run_id,
            EVENT_SOURCE_RESOLUTION_COMPLETED,
            completed_idem,
        )
        if completed_event_id is None:
            completed_event_id = _insert_resolution_event(
                conn,
                run_id=request.orchestration_run_id,
                event_type=EVENT_SOURCE_RESOLUTION_COMPLETED,
                sequence_no=next_sequence_no,
                idem=completed_idem,
                candidate_id=candidate_id,
                payload={
                    "source_candidate_id": candidate_id,
                    "outcome": decision.outcome,
                    "resolution_target_kind": decision.resolution_target_kind,
                    "failure_reason": decision.failure_reason,
                },
            )
            next_sequence_no += 1
        event_ids.append(completed_event_id)

        per_candidate_outcomes[candidate_id] = decision.outcome
        counters["candidates_attempted"] += 1
        _increment_counters_for_outcome(counters, decision.outcome)

    # 9) Result. A run that exists makes the pass 'succeeded' even with zero
    #    candidates. publication_status stays not_evaluated, gate_report_id None.
    return SourceResolutionPassResult(
        status=RESULT_STATUS_SUCCEEDED,
        orchestration_run_id=request.orchestration_run_id,
        source_resolution_ids=tuple(source_resolution_ids),
        per_candidate_outcomes=per_candidate_outcomes,
        event_ids=tuple(event_ids),
        counters=counters,
        publication_status=PUBLICATION_STATUS_NOT_EVALUATED,
        gate_report_id=None,
    )
