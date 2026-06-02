"""Pure, no-DB unit tests for
apps/worker/app/services/orchestration_source_resolution.py (Phase
ORCH-MULTI-B1).

These tests exercise ONLY the pure-logic slice of the source resolution pass:
the request/result contracts and the deterministic pure functions. They need
NO database, NO DATABASE_URL, NO Redis, NO FastAPI, NO network and invoke NO
provider. Package ``app`` resolves to apps/worker/app, so the service module
is importable directly with PYTHONPATH=apps/worker.

Coverage map:
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
  + run_source_resolution_pass raises NotImplementedError
"""
from __future__ import annotations

import dataclasses

import pytest

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
    RESOLUTION_TARGET_KIND_INTERNAL_DOCUMENT,
    RESOLUTION_TARGET_KIND_UPLOADED_DOCUMENT,
    RESOLUTION_TARGET_KIND_URL,
    SOURCE_CANDIDATE_STATUS_INSUFFICIENT_METADATA,
    SOURCE_CANDIDATE_STATUS_PROPOSED,
    SOURCE_CANDIDATE_STATUS_RESOLUTION_FAILED,
    SOURCE_CANDIDATE_STATUS_RESOLVED,
    _build_candidate_scoped_idempotency_key,
    _build_initial_counters,
    _classify_candidate,
    _derive_candidate_state,
    _increment_counters_for_outcome,
    _stable_candidate_sort_key,
    run_source_resolution_pass,
)


# ---------------------------------------------------------------------------
# 1-4) _derive_candidate_state
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 5) classify external URL -> unreachable, never resolved
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 6) classify empty metadata -> insufficient_metadata
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 7) classify local marker -> resolved with correct target kind
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 8) stable sort key deterministic
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 9) candidate-scoped idempotency key
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 10) counters increment correctly
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 11) dataclasses preserve the invariants
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# DB-backed pass stub
# ---------------------------------------------------------------------------
def test_run_source_resolution_pass_is_not_implemented_yet():
    request = SourceResolutionPassRequest(
        tenant_id="t",
        orchestration_run_id="run-1",
        idempotency_key="idem-1",
    )
    with pytest.raises(NotImplementedError):
        run_source_resolution_pass(conn=None, request=request)
