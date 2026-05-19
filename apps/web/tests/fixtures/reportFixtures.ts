/**
 * Fixture data for UI-REPORT-A tests.
 *
 * Each fixture is a complete `AntiHallucinationReport` object that
 * mirrors what the backend would emit for a representative scenario.
 * They are NOT collected from a running backend in this block — they
 * are constructed manually following the shape in
 * `apps/api/app/routes/anti_hallucination_report.py` and validated by
 * the existing API-level tests at
 * `apps/api/tests/test_anti_hallucination_report_endpoint.py`.
 *
 * Keep the fixtures small. Each scenario sets only the fields the
 * tests assert against; the rest is a sensible zero-default that
 * matches the backend's defaults for an empty task.
 *
 * Do NOT import these fixtures from a real test API: they are
 * synthetic by design, so the tests stay deterministic and run
 * without a live backend.
 */

import type {
  AntiHallucinationReport,
  AxisSummary,
  CoverageGap,
  MockIndicators,
} from "../../lib/reportTypes";

/** A four-axis summary with every counter at zero. */
export function zeroAxisSummary(): AxisSummary {
  return {
    cve_lite: {
      verified_claims_count: 0,
      unverified_claims_count: 0,
      inconclusive_count: 0,
    },
    source_quality: {
      strong_count: 0,
      adequate_count: 0,
      weak_count: 0,
      unsuitable_count: 0,
      unknown_count: 0,
      missing_count: 0,
    },
    claim_entailment: {
      entailed_count: 0,
      partially_supported_count: 0,
      not_supported_count: 0,
      contradicted_count: 0,
      uncertain_count: 0,
      missing_count: 0,
    },
    final_gate: {
      has_blocking_gaps: false,
      has_warnings: false,
      blocking_gap_count: 0,
      warning_gap_count: 0,
    },
  };
}

/** Mock indicators with every flag true (MVP-0 default). */
export function allMockIndicators(): MockIndicators {
  return {
    uses_mock_source_quality: true,
    uses_mock_claim_entailment: true,
    uses_mock_compiler: true,
    uses_mock_cve_lite: true,
    notes: [
      "Una fonte citata non implica un claim vero.",
      "Una quote testualmente presente non implica che la quote " +
        "sostenga il claim.",
      "Un verdict 'entailed' non implica che il claim sia vero nel " +
        "mondo.",
    ],
  };
}

/** Mock indicators with every flag false. */
export function noMockIndicators(): MockIndicators {
  return {
    uses_mock_source_quality: false,
    uses_mock_claim_entailment: false,
    uses_mock_compiler: false,
    uses_mock_cve_lite: false,
    notes: ["All evaluators report non-mock data."],
  };
}

/** Standard limitations array as produced by the backend. */
export function standardLimitations(): string[] {
  return [
    "Una fonte citata non implica che il claim sia vero.",
    "Una quote testualmente presente non implica supporto " +
      "semantico del claim.",
    "Un verdict 'entailed' non implica verità nel mondo.",
    "Il payload JSONB è esposto verbatim; RBAC/redaction non " +
      "implementata.",
  ];
}

/**
 * Published-with-warnings scenario.
 *
 * Mirrors what the backend produces today on the warning path:
 *   - publication.status='published';
 *   - gate.decision='approved',
 *     reason_code='all_spans_verified_with_warnings';
 *   - axis_summary.cve_lite.verified_claims_count >= 1;
 *   - axis_summary.source_quality.unknown_count >= 1;
 *   - axis_summary.claim_entailment.entailed_count >= 1;
 *   - axis_summary.final_gate.has_warnings=true;
 *   - mock_indicators all true.
 */
export function mockPublishedWarningReport(): AntiHallucinationReport {
  const axis = zeroAxisSummary();
  axis.cve_lite.verified_claims_count = 1;
  axis.source_quality.unknown_count = 1;
  axis.claim_entailment.entailed_count = 1;
  axis.final_gate.has_warnings = true;
  axis.final_gate.warning_gap_count = 1;

  const warningGap: CoverageGap = {
    id: "00000000-0000-0000-0000-000000000a01",
    draft_final_answer_id: "00000000-0000-0000-0000-0000000000d1",
    kind: "source_quality_warning",
    severity: "warn",
    gap_key: "span:00000000-0000-0000-0000-0000000000e1:source_quality_warning",
    details: { reason: "overall_quality='unknown'" },
    created_at: "2026-05-19T10:00:00Z",
    axis: "source_quality",
  };

  return {
    task_id: "00000000-0000-0000-0000-0000000000ff",
    project_id: "00000000-0000-0000-0000-000000000001",
    tenant_id: "00000000-0000-0000-0000-000000000002",
    task: {
      status: "published",
      objective: "Aggregate revenue numbers from the 2024 doc.",
      mode: "closed_corpus",
      created_at: "2026-05-19T09:50:00Z",
      updated_at: "2026-05-19T10:00:30Z",
    },
    publication: {
      status: "published",
      published_answer_id: "00000000-0000-0000-0000-0000000000a1",
      published_answer_status: "published",
      summary_text: "Revenue reached 12500000 USD in 2024.",
      content_hash:
        "5b5e0e8a4f1d9c2a3a6f8d4a1c0e3b9d7f2a1b4c5d6e7f8a9b0c1d2e3f4a5b6c",
      final_gate_report_id: "00000000-0000-0000-0000-0000000000b1",
    },
    gate: {
      decision: "approved",
      reason_code: "all_spans_verified_with_warnings",
      policy_name: "mvp0_entailment_gate_policy",
      policy_version: "0.1.0",
      payload: { entailment: { status: "warnings" } },
      coverage_gaps: [warningGap],
    },
    claims: [
      {
        logical_claim_id: "00000000-0000-0000-0000-0000000000c1",
        latest_entry_id: "00000000-0000-0000-0000-0000000000c2",
        latest_state: "verified_fact",
        canonical_claim_text: "Revenue reached 12500000 USD in 2024.",
        claim_type: null,
        support_scope: "supported_by_user_corpus_only",
        evidence_links: [],
        cve_lite: [],
        source_quality: [],
        entailment: [],
      },
    ],
    evidence: [
      {
        evidence_span_id: "00000000-0000-0000-0000-0000000000e1",
        document_chunk_id: "00000000-0000-0000-0000-0000000000d2",
        quote: "Revenue reached 12500000 USD in 2024.",
        quote_hash:
          "f6a1e0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1",
        document_id: "00000000-0000-0000-0000-0000000000d0",
        document_filename: "revenue-2024.txt",
      },
    ],
    axis_summary: axis,
    mock_indicators: allMockIndicators(),
    limitations: standardLimitations(),
  };
}

/**
 * Not-ready scenario: task exists but the pipeline has not reached
 * the gate. Claims and evidence are empty; axis_summary is all zero;
 * mock indicators are all true (MVP-0 fallback).
 */
export function mockNotReadyReport(): AntiHallucinationReport {
  return {
    task_id: "00000000-0000-0000-0000-0000000000fe",
    project_id: "00000000-0000-0000-0000-000000000001",
    tenant_id: "00000000-0000-0000-0000-000000000002",
    task: {
      status: "created",
      objective: "Pending task for unit tests.",
      mode: "closed_corpus",
      created_at: "2026-05-19T11:00:00Z",
      updated_at: "2026-05-19T11:00:00Z",
    },
    publication: {
      status: "not_ready",
      published_answer_id: null,
      published_answer_status: null,
      summary_text: null,
      content_hash: null,
      final_gate_report_id: null,
    },
    gate: {
      decision: null,
      reason_code: null,
      policy_name: null,
      policy_version: null,
      payload: {},
      coverage_gaps: [],
    },
    claims: [],
    evidence: [],
    axis_summary: zeroAxisSummary(),
    mock_indicators: allMockIndicators(),
    limitations: standardLimitations(),
  };
}

/**
 * Publication-held scenario: gate rejected with `entailment_block`.
 * Useful for testing the held banner + block gap surfacing.
 */
export function mockPublicationHeldReport(): AntiHallucinationReport {
  const axis = zeroAxisSummary();
  axis.cve_lite.verified_claims_count = 1;
  axis.claim_entailment.contradicted_count = 1;
  axis.final_gate.has_blocking_gaps = true;
  axis.final_gate.blocking_gap_count = 1;

  const blockGap: CoverageGap = {
    id: "00000000-0000-0000-0000-000000000a02",
    draft_final_answer_id: "00000000-0000-0000-0000-0000000000d3",
    kind: "entailment_block",
    severity: "block",
    gap_key: "span:00000000-0000-0000-0000-0000000000e2:entailment_block",
    details: { reason: "contradicted" },
    created_at: "2026-05-19T12:00:00Z",
    axis: "claim_entailment",
  };

  return {
    task_id: "00000000-0000-0000-0000-0000000000fd",
    project_id: "00000000-0000-0000-0000-000000000001",
    tenant_id: "00000000-0000-0000-0000-000000000002",
    task: {
      status: "analyzed_partial",
      objective: "Stub task in publication-held state for tests.",
      mode: "closed_corpus",
      created_at: "2026-05-19T11:50:00Z",
      updated_at: "2026-05-19T12:00:10Z",
    },
    publication: {
      status: "publication_held",
      published_answer_id: null,
      published_answer_status: null,
      summary_text: null,
      content_hash: null,
      final_gate_report_id: null,
    },
    gate: {
      decision: "rejected",
      reason_code: "entailment_block",
      policy_name: "mvp0_entailment_gate_policy",
      policy_version: "0.1.0",
      payload: { entailment: { status: "blocked" } },
      coverage_gaps: [blockGap],
    },
    claims: [
      {
        logical_claim_id: "00000000-0000-0000-0000-0000000000c3",
        latest_entry_id: "00000000-0000-0000-0000-0000000000c4",
        latest_state: "verified_fact",
        canonical_claim_text: "Stubbed contradicted claim.",
        claim_type: null,
        support_scope: "supported_by_user_corpus_only",
        evidence_links: [],
        cve_lite: [],
        source_quality: [],
        entailment: [
          {
            claim_ledger_entry_id: "00000000-0000-0000-0000-0000000000c4",
            evidence_span_id: "00000000-0000-0000-0000-0000000000e2",
            latest_check_id: "00000000-0000-0000-0000-0000000000ce",
            verdict: "contradicted",
            confidence: 0.9,
            checker_name: "test_entailment_checker",
            policy_name: "test_entailment_block_policy",
            policy_version: "0.1.0",
            mock: true,
          },
        ],
      },
    ],
    evidence: [
      {
        evidence_span_id: "00000000-0000-0000-0000-0000000000e2",
        document_chunk_id: "00000000-0000-0000-0000-0000000000d4",
        quote: "Stubbed quote for the held scenario.",
        quote_hash:
          "9e8f7a6b5c4d3e2f1a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f",
        document_id: "00000000-0000-0000-0000-0000000000d0",
        document_filename: "stub-held.txt",
      },
    ],
    axis_summary: axis,
    mock_indicators: allMockIndicators(),
    limitations: standardLimitations(),
  };
}
