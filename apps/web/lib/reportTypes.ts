/**
 * TypeScript types for the Anti-Hallucination Report API
 * (Phase 8.8B-REPORT) consumed by the UI viewer (UI-REPORT-A).
 *
 * These interfaces mirror the JSON shape produced by
 * apps/api/app/routes/anti_hallucination_report.py and documented in
 * PHASE_8_8B_REPORT_PRE.md §4. They are intentionally manual TypeScript
 * declarations (no codegen, no Pydantic import): a wrapper that the UI
 * can evolve independently from the backend shape if needed.
 *
 * Semantic disclaimer (preserved verbatim from
 * PHASE_8_8B_REPORT_PRE.md §3.3):
 *   - CVE-lite measures quote/hash presence, NOT semantic support.
 *   - Source Quality measures source-level quality, NOT whether the
 *     source supports the claim, and NOT truth.
 *   - Claim Entailment measures the local claim ↔ quote relation under
 *     the mock checker's normalization; "entailed" does NOT mean true.
 *   - Final Gate composes axes for publishability under a versioned
 *     policy; it does NOT guarantee absolute truth.
 *   - The report is a derived read-only view; it does NOT introduce
 *     new decisions and does NOT recompute the Gate.
 */

// ---------------------------------------------------------------------------
// Top-level enums and string-literal unions
// ---------------------------------------------------------------------------

/**
 * Derived publication status surfaced by the report. See §7 of
 * PHASE_8_8B_REPORT_PRE.md for the derivation table.
 *
 * IMPORTANT: `publication_held` is a DERIVED report state, NOT a value
 * of `task_masters.status`. The UI must label it as such.
 *
 * `unknown` is a defensive fallback when the DB combination does not
 * match any documented derivation rule; the UI should surface it as a
 * "Status unknown (defensive fallback)" diagnostic rather than treat
 * it as normal.
 */
export type PublicationStatus =
  | "published"
  | "withdrawn"
  | "superseded"
  | "publication_held"
  | "not_ready"
  | "failed"
  | "unknown";

/**
 * Final Answer Gate decision as written by the worker into
 * `final_gate_reports.decision`. `null` is surfaced when no gate
 * report exists yet (task did not reach the gate step).
 */
export type GateDecision = "approved" | "rejected" | "held_for_review" | null;

/**
 * Coverage gap severity (mirrors the DB CHECK on
 * `coverage_gap_statements.severity`). Sort order surfaced by the
 * report is `block` < `warn` < `info`.
 */
export type CoverageGapSeverity = "info" | "warn" | "block";

/**
 * Derived axis decoration on coverage gaps. Computed by the report
 * route from the gap's `kind` value; see §8 of
 * PHASE_8_8B_REPORT_PRE.md.
 */
export type CoverageGapAxis =
  | "cve_lite"
  | "source_quality"
  | "claim_entailment"
  | "coverage"
  | "source_loss"
  | "other";

/**
 * Latest ledger entry state for a logical claim. The full codomain is
 * defined in 0004 (claim_ledger.sql); the UI treats it as a string and
 * does NOT depend on the exact members.
 */
export type LedgerEntryState = string;

/**
 * Source Quality `overall_quality` codomain (0007 CHECK + mock
 * evaluator output range). The MVP-0 mock evaluator only emits
 * `unknown`.
 */
export type SourceQualityOverall =
  | "strong"
  | "adequate"
  | "weak"
  | "unsuitable"
  | "unknown";

/**
 * Claim Entailment verdict codomain (0009 CHECK). The MVP-0 mock
 * checker only emits `entailed`, `not_supported`, `uncertain`.
 */
export type ClaimEntailmentVerdict =
  | "entailed"
  | "partially_supported"
  | "not_supported"
  | "contradicted"
  | "uncertain";

// ---------------------------------------------------------------------------
// Nested structures
// ---------------------------------------------------------------------------

export interface TaskSection {
  status: string | null;
  objective: string | null;
  mode: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface PublicationSection {
  status: PublicationStatus;
  published_answer_id: string | null;
  published_answer_status: "published" | "withdrawn" | "superseded" | null;
  summary_text: string | null;
  content_hash: string | null;
  final_gate_report_id: string | null;
}

export interface CoverageGap {
  id: string;
  draft_final_answer_id: string;
  kind: string;
  severity: CoverageGapSeverity | string;
  gap_key: string;
  details: unknown;
  created_at: string | null;
  axis: CoverageGapAxis | string;
}

export interface GateSection {
  decision: GateDecision;
  reason_code: string | null;
  policy_name?: string | null;
  policy_version?: string | null;
  payload: unknown;
  coverage_gaps: CoverageGap[];
}

// --- Claims ---------------------------------------------------------------

export interface EvidenceLinkView {
  claim_evidence_link_id: string;
  evidence_span_id: string | null;
  link_role: string | null;
}

export interface CveLiteView {
  verification_record_id: string;
  claim_ledger_entry_id: string;
  outcome: string | null;
  check_name: string | null;
}

export interface SourceQualitySlot {
  evidence_span_id: string;
  latest_assessment_id: string | null;
  overall_quality: SourceQualityOverall | string | null;
  contradiction_status: string | null;
  evaluator_name: string | null;
  policy_name: string | null;
  policy_version: string | null;
  mock: boolean | null;
}

export interface EntailmentSlot {
  claim_ledger_entry_id: string;
  evidence_span_id: string;
  latest_check_id: string | null;
  verdict: ClaimEntailmentVerdict | string | null;
  confidence: number | null;
  checker_name: string | null;
  policy_name: string | null;
  policy_version: string | null;
  mock: boolean | null;
}

export interface ClaimItem {
  logical_claim_id: string;
  latest_entry_id: string | null;
  latest_state: LedgerEntryState | null;
  canonical_claim_text: string | null;
  claim_type: string | null;
  support_scope: string | null;
  evidence_links: EvidenceLinkView[];
  cve_lite: CveLiteView[];
  source_quality: SourceQualitySlot[];
  entailment: EntailmentSlot[];
}

// --- Evidence -------------------------------------------------------------

export interface EvidenceItem {
  evidence_span_id: string;
  document_chunk_id: string | null;
  quote: string | null;
  quote_hash: string | null;
  document_id: string | null;
  document_filename: string | null;
}

// --- Axis summary ---------------------------------------------------------

export interface CveLiteSummary {
  verified_claims_count: number;
  unverified_claims_count: number;
  inconclusive_count: number;
}

export interface SourceQualitySummary {
  strong_count: number;
  adequate_count: number;
  weak_count: number;
  unsuitable_count: number;
  unknown_count: number;
  missing_count: number;
}

export interface ClaimEntailmentSummary {
  entailed_count: number;
  partially_supported_count: number;
  not_supported_count: number;
  contradicted_count: number;
  uncertain_count: number;
  missing_count: number;
}

export interface FinalGateSummary {
  has_blocking_gaps: boolean;
  has_warnings: boolean;
  blocking_gap_count: number;
  warning_gap_count: number;
}

export interface AxisSummary {
  cve_lite: CveLiteSummary;
  source_quality: SourceQualitySummary;
  claim_entailment: ClaimEntailmentSummary;
  final_gate: FinalGateSummary;
}

// --- Mock indicators ------------------------------------------------------

export interface MockIndicators {
  uses_mock_source_quality: boolean;
  uses_mock_claim_entailment: boolean;
  uses_mock_compiler: boolean;
  uses_mock_cve_lite: boolean;
  notes: string[];
}

// --- Top-level report -----------------------------------------------------

export interface AntiHallucinationReport {
  task_id: string;
  project_id: string | null;
  tenant_id: string | null;
  task: TaskSection;
  publication: PublicationSection;
  gate: GateSection;
  claims: ClaimItem[];
  evidence: EvidenceItem[];
  axis_summary: AxisSummary;
  mock_indicators: MockIndicators;
  limitations: string[];
}
