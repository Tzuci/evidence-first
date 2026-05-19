import * as React from "react";
import type { MockIndicators } from "../lib/reportTypes";
import { hasAnyMockIndicator } from "../lib/reportFormatting";

/**
 * `MockIndicatorsPanel` surfaces the four mock-indicator flags AND
 * the `notes` array verbatim, plus a banner when any flag is true.
 *
 * Hard constraints (UI-REPORT-A §3.5, §7):
 *   - The panel MUST be visible at all times (no `<details>`, no
 *     collapse-on-default). The whole anti-hallucination promise
 *     rests on the reviewer seeing whether the data came from a
 *     mock evaluator/checker; hiding it behind a click is a
 *     promise-breaker.
 *   - When any flag is true, a banner sits above the indicator rows
 *     stating that the task ran on mock evaluator(s). The banner
 *     text is the canonical wording prescribed by the block prompt;
 *     do NOT paraphrase it elsewhere.
 *   - Each flag renders as a small pill with value "mock" or
 *     "not detected". We deliberately avoid the word "real" — a
 *     non-mock evaluator does not automatically imply a "real" /
 *     production-grade one in MVP-0.
 *   - The `notes` are rendered verbatim, as a `<ul>` list. The UI
 *     does NOT paraphrase or filter them; they come from the
 *     backend.
 */
export interface MockIndicatorsPanelProps {
  mockIndicators: MockIndicators;
}

const sectionStyle: React.CSSProperties = {
  marginTop: 16,
  marginBottom: 16,
  padding: 16,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fff",
};

const headerStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  margin: 0,
  marginBottom: 8,
  color: "#111",
};

const bannerStyle: React.CSSProperties = {
  padding: "10px 12px",
  marginBottom: 12,
  background: "#efe3f5",
  border: "1px solid #d6c1e3",
  borderRadius: 4,
  color: "#3f2257",
  fontSize: 13,
  fontWeight: 500,
};

const rowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "6px 0",
  fontSize: 13,
};

const indicatorLabelStyle: React.CSSProperties = {
  color: "#333",
};

const mockPillStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "1px 8px",
  fontSize: 11,
  fontWeight: 600,
  background: "#efe3f5",
  color: "#5a387a",
  border: "1px solid #d6c1e3",
  borderRadius: 3,
  letterSpacing: 0.3,
};

const neutralPillStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "1px 8px",
  fontSize: 11,
  fontWeight: 600,
  background: "#eef0f3",
  color: "#4a4f57",
  border: "1px solid #d0d4d9",
  borderRadius: 3,
  letterSpacing: 0.3,
};

function IndicatorRow({
  label,
  isMock,
}: {
  label: string;
  isMock: boolean;
}): React.ReactElement {
  return (
    <div style={rowStyle}>
      <span style={indicatorLabelStyle}>{label}</span>
      <span
        style={isMock ? mockPillStyle : neutralPillStyle}
        aria-label={`${label}: ${isMock ? "mock" : "not detected"}`}
      >
        {isMock ? "mock" : "not detected"}
      </span>
    </div>
  );
}

export default function MockIndicatorsPanel(
  props: MockIndicatorsPanelProps
): React.ReactElement {
  const mi = props.mockIndicators;
  const anyMock = hasAnyMockIndicator(mi);

  return (
    <section
      aria-labelledby="mock-indicators-heading"
      data-testid="mock-indicators-panel"
      style={sectionStyle}
    >
      <h2 id="mock-indicators-heading" style={headerStyle}>
        Mock indicators
      </h2>

      {anyMock ? (
        <div role="alert" style={bannerStyle}>
          This task ran on mock evaluator(s). Output is for system
          validation only.
        </div>
      ) : null}

      <IndicatorRow
        label="Source Quality"
        isMock={mi.uses_mock_source_quality}
      />
      <IndicatorRow
        label="Claim Entailment"
        isMock={mi.uses_mock_claim_entailment}
      />
      <IndicatorRow label="Compiler" isMock={mi.uses_mock_compiler} />
      <IndicatorRow label="CVE-lite" isMock={mi.uses_mock_cve_lite} />

      {Array.isArray(mi.notes) && mi.notes.length > 0 ? (
        <div style={{ marginTop: 12 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 600,
              color: "#555",
              marginBottom: 4,
            }}
          >
            Notes
          </div>
          <ul
            style={{
              margin: 0,
              paddingLeft: 18,
              fontSize: 12,
              color: "#444",
            }}
          >
            {mi.notes.map((note, idx) => (
              <li key={idx} style={{ marginTop: 2 }}>
                {note}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
