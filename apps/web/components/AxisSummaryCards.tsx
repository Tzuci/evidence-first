import * as React from "react";
import type {
  AxisSummary,
  MockIndicators,
} from "../lib/reportTypes";

/**
 * `AxisSummaryCards` surfaces the four-axis summary of the report:
 *   1. CVE-lite (quote/hash check)
 *   2. Source Quality (mock evaluator — `unknown` only in MVP-0)
 *   3. Claim ↔ Evidence relation (Claim Entailment)
 *   4. Final Gate outcome (derived from coverage gaps)
 *
 * Hard semantic constraints (UI-REPORT-A §7, §3.3):
 *   - NO aggregate "trust score" or cross-axis percentage is shown.
 *     The whole point of separating these axes is to keep them
 *     visually orthogonal.
 *   - Each card MUST show every codomain counter even when 0, so
 *     the reviewer sees the full range of possible values and is
 *     not misled by a hidden zero.
 *   - Each card carries a short axis-specific disclaimer:
 *       * CVE-lite: "Quote/hash check, not semantic support."
 *       * Source Quality: "Source quality does not prove the claim."
 *       * Claim Entailment: "Entailed does not mean true."
 *       * Final Gate: a short note that this is a derived view, not
 *         a recomputed decision.
 *   - When the corresponding mock indicator is true, an inline
 *     `mock` chip is rendered next to the card title. The Final
 *     Gate card has no mock chip (the Gate is policy-versioned, not
 *     "mock" in the same sense).
 */
export interface AxisSummaryCardsProps {
  axisSummary: AxisSummary;
  mockIndicators: MockIndicators;
}

const cardContainerStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
  gap: 12,
  marginTop: 16,
  marginBottom: 16,
};

const cardStyle: React.CSSProperties = {
  padding: 14,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fff",
};

const cardTitleStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
  fontSize: 14,
  fontWeight: 600,
  margin: 0,
  marginBottom: 8,
  color: "#111",
};

const counterRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  fontSize: 13,
  marginTop: 4,
  color: "#333",
};

const counterKeyStyle: React.CSSProperties = {
  color: "#555",
};

const counterValueStyle: React.CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  color: "#111",
};

const mockChipStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "1px 6px",
  fontSize: 11,
  fontWeight: 600,
  background: "#efe3f5",
  color: "#5a387a",
  border: "1px solid #d6c1e3",
  borderRadius: 3,
  letterSpacing: 0.3,
};

const disclaimerStyle: React.CSSProperties = {
  marginTop: 10,
  fontSize: 11,
  color: "#666",
  fontStyle: "italic",
};

function Counter({
  label,
  value,
}: {
  label: string;
  value: number;
}): React.ReactElement {
  return (
    <div style={counterRowStyle}>
      <span style={counterKeyStyle}>{label}</span>
      <span style={counterValueStyle}>{value}</span>
    </div>
  );
}

function MockChip(): React.ReactElement {
  return (
    <span aria-label="Mock evaluator" style={mockChipStyle}>
      mock
    </span>
  );
}

export default function AxisSummaryCards(
  props: AxisSummaryCardsProps
): React.ReactElement {
  const { axisSummary, mockIndicators } = props;

  return (
    <section aria-labelledby="axis-summary-heading">
      <h2
        id="axis-summary-heading"
        style={{
          fontSize: 18,
          fontWeight: 600,
          margin: 0,
          marginTop: 16,
          color: "#111",
        }}
      >
        Axis summary
      </h2>
      <div style={cardContainerStyle}>
        {/* CVE-lite ---------------------------------------------------- */}
        <article
          aria-label="CVE-lite axis"
          data-axis="cve_lite"
          style={cardStyle}
        >
          <h3 style={cardTitleStyle}>
            <span>CVE-lite</span>
            {mockIndicators.uses_mock_cve_lite ? <MockChip /> : null}
          </h3>
          <Counter
            label="verified_claims_count"
            value={axisSummary.cve_lite.verified_claims_count}
          />
          <Counter
            label="unverified_claims_count"
            value={axisSummary.cve_lite.unverified_claims_count}
          />
          <Counter
            label="inconclusive_count"
            value={axisSummary.cve_lite.inconclusive_count}
          />
          <p style={disclaimerStyle}>
            Quote/hash check, not semantic support.
          </p>
        </article>

        {/* Source Quality ---------------------------------------------- */}
        <article
          aria-label="Source Quality axis"
          data-axis="source_quality"
          style={cardStyle}
        >
          <h3 style={cardTitleStyle}>
            <span>Source Quality</span>
            {mockIndicators.uses_mock_source_quality ? <MockChip /> : null}
          </h3>
          <Counter
            label="strong_count"
            value={axisSummary.source_quality.strong_count}
          />
          <Counter
            label="adequate_count"
            value={axisSummary.source_quality.adequate_count}
          />
          <Counter
            label="weak_count"
            value={axisSummary.source_quality.weak_count}
          />
          <Counter
            label="unsuitable_count"
            value={axisSummary.source_quality.unsuitable_count}
          />
          <Counter
            label="unknown_count"
            value={axisSummary.source_quality.unknown_count}
          />
          <Counter
            label="missing_count"
            value={axisSummary.source_quality.missing_count}
          />
          <p style={disclaimerStyle}>
            Source quality does not prove the claim.
          </p>
        </article>

        {/* Claim ↔ Evidence relation ---------------------------------- */}
        <article
          aria-label="Claim Entailment axis"
          data-axis="claim_entailment"
          style={cardStyle}
        >
          <h3 style={cardTitleStyle}>
            <span>Claim {"\u2194"} Evidence relation</span>
            {mockIndicators.uses_mock_claim_entailment ? (
              <MockChip />
            ) : null}
          </h3>
          <Counter
            label="entailed_count"
            value={axisSummary.claim_entailment.entailed_count}
          />
          <Counter
            label="partially_supported_count"
            value={axisSummary.claim_entailment.partially_supported_count}
          />
          <Counter
            label="not_supported_count"
            value={axisSummary.claim_entailment.not_supported_count}
          />
          <Counter
            label="contradicted_count"
            value={axisSummary.claim_entailment.contradicted_count}
          />
          <Counter
            label="uncertain_count"
            value={axisSummary.claim_entailment.uncertain_count}
          />
          <Counter
            label="missing_count"
            value={axisSummary.claim_entailment.missing_count}
          />
          <p style={disclaimerStyle}>Entailed does not mean true.</p>
        </article>

        {/* Final Gate (derived) ---------------------------------------- */}
        <article
          aria-label="Final Gate (derived)"
          data-axis="final_gate"
          style={cardStyle}
        >
          <h3 style={cardTitleStyle}>
            <span>Final Gate (derived)</span>
          </h3>
          <Counter
            label="blocking_gap_count"
            value={axisSummary.final_gate.blocking_gap_count}
          />
          <Counter
            label="warning_gap_count"
            value={axisSummary.final_gate.warning_gap_count}
          />
          <div style={counterRowStyle}>
            <span style={counterKeyStyle}>has_blocking_gaps</span>
            <span style={counterValueStyle}>
              {String(axisSummary.final_gate.has_blocking_gaps)}
            </span>
          </div>
          <div style={counterRowStyle}>
            <span style={counterKeyStyle}>has_warnings</span>
            <span style={counterValueStyle}>
              {String(axisSummary.final_gate.has_warnings)}
            </span>
          </div>
          <p style={disclaimerStyle}>
            Report is a derived read-only view; not a new decision.
          </p>
        </article>
      </div>
    </section>
  );
}
