import * as React from "react";
import type { GateSection } from "../lib/reportTypes";
import { gateDecisionLabel } from "../lib/reportFormatting";

/**
 * `GatePanel` surfaces the Final Answer Gate decision and reason
 * code from the report.
 *
 * Constraints (UI-REPORT-A §7):
 *   - The detailed `coverage_gaps` list is NOT rendered here — that
 *     panel is scope of UI-REPORT-B. We do show the COUNT of gaps as
 *     a small diagnostic, so the reviewer sees there are gaps to
 *     drill into when they navigate to the future block.
 *   - The raw `reason_code` is shown verbatim in monospace, never
 *     paraphrased into something that could be mistaken for a truth
 *     judgment.
 *   - A short disclaimer reminds the reviewer that the decision was
 *     read from the persisted Final Gate report and is NOT
 *     recomputed by the UI.
 */
export interface GatePanelProps {
  gate: GateSection;
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

const fieldRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  fontSize: 13,
  marginTop: 6,
  color: "#333",
};

const fieldKeyStyle: React.CSSProperties = {
  color: "#666",
  minWidth: 200,
};

const monoStyle: React.CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
};

const decisionBadgeStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "2px 8px",
  borderRadius: 4,
  fontSize: 13,
  fontWeight: 600,
  letterSpacing: 0.2,
};

const disclaimerStyle: React.CSSProperties = {
  marginTop: 12,
  fontSize: 12,
  color: "#555",
  fontStyle: "italic",
};

function decisionBadgeColors(
  decision: string | null
): { background: string; color: string; border: string } {
  if (decision === "approved") {
    return { background: "#e7f3ec", color: "#1f5d3a", border: "#bcdcc8" };
  }
  if (decision === "rejected") {
    return { background: "#fbe7e7", color: "#7a1f1f", border: "#eec0c0" };
  }
  return { background: "#f0f0f0", color: "#3a3a3a", border: "#d0d0d0" };
}

export default function GatePanel(
  props: GatePanelProps
): React.ReactElement {
  const g = props.gate;
  const decisionColors = decisionBadgeColors(g.decision);
  const gapsCount = Array.isArray(g.coverage_gaps)
    ? g.coverage_gaps.length
    : 0;

  return (
    <section
      aria-labelledby="gate-panel-heading"
      style={sectionStyle}
    >
      <h2 id="gate-panel-heading" style={headerStyle}>
        Final Gate
      </h2>

      {g.decision === null ? (
        <p style={{ fontSize: 13, color: "#555", marginTop: 4 }}>
          No gate report yet.
        </p>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span
              style={{ ...decisionBadgeStyle, ...decisionColors }}
              aria-label={`Gate decision: ${gateDecisionLabel(g.decision)}`}
            >
              {gateDecisionLabel(g.decision)}
            </span>
            <span style={{ ...monoStyle, color: "#777" }}>{g.decision}</span>
          </div>

          {g.reason_code ? (
            <div style={fieldRowStyle}>
              <span style={fieldKeyStyle}>reason_code</span>
              <span style={monoStyle}>{g.reason_code}</span>
            </div>
          ) : null}

          {g.policy_name ? (
            <div style={fieldRowStyle}>
              <span style={fieldKeyStyle}>policy_name</span>
              <span style={monoStyle}>{g.policy_name}</span>
            </div>
          ) : null}

          {g.policy_version ? (
            <div style={fieldRowStyle}>
              <span style={fieldKeyStyle}>policy_version</span>
              <span style={monoStyle}>{g.policy_version}</span>
            </div>
          ) : null}

          <div style={fieldRowStyle}>
            <span style={fieldKeyStyle}>coverage_gaps (count)</span>
            <span style={monoStyle}>{gapsCount}</span>
          </div>
        </>
      )}

      <p style={disclaimerStyle}>
        Decision read from persisted Final Gate report; not recomputed
        by UI.
      </p>
    </section>
  );
}
