import * as React from "react";
import type { PublicationSection } from "../lib/reportTypes";
import { shortId } from "../lib/reportFormatting";
import ReportStatusBadge from "./ReportStatusBadge";

/**
 * `PublicationPanel` surfaces the `publication` section of the
 * Anti-Hallucination Report.
 *
 * Renders, in order:
 *   - The status badge.
 *   - When `published`: the `published_answer_id`, its
 *     `published_answer_status`, the `content_hash`, the
 *     `final_gate_report_id`, and the truncated `summary_text`.
 *   - When `publication_held`: a textual note pointing the reviewer
 *     to the blocking gaps.
 *   - When `not_ready`: a textual note explaining the task has not
 *     reached the gate yet.
 *   - A small disclaimer reminding the reviewer that publication
 *     status is ledger-level, not truth-level.
 *
 * Constraint: this component does NOT compute any state from the
 * coverage gaps or claim data — it operates strictly on the
 * `publication` slice.
 */
export interface PublicationPanelProps {
  publication: PublicationSection;
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

const noteStyle: React.CSSProperties = {
  marginTop: 8,
  padding: "8px 10px",
  background: "#fff7e6",
  border: "1px solid #f0d8a8",
  borderRadius: 4,
  color: "#5c3d10",
  fontSize: 13,
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

const disclaimerStyle: React.CSSProperties = {
  marginTop: 12,
  fontSize: 12,
  color: "#555",
  fontStyle: "italic",
};

export default function PublicationPanel(
  props: PublicationPanelProps
): React.ReactElement {
  const p = props.publication;
  return (
    <section
      aria-labelledby="publication-panel-heading"
      style={sectionStyle}
    >
      <h2 id="publication-panel-heading" style={headerStyle}>
        Publication
      </h2>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <ReportStatusBadge status={p.status} />
        <span style={{ ...monoStyle, color: "#777" }}>{p.status}</span>
      </div>

      {p.status === "not_ready" ? (
        <p style={noteStyle}>
          Report not ready yet. The task may not have reached the Final
          Gate.
        </p>
      ) : null}

      {p.status === "publication_held" ? (
        <p style={noteStyle}>
          Publication held. Review blocking gaps in the report.
        </p>
      ) : null}

      {p.status === "failed" ? (
        <p style={noteStyle}>
          Task failed. Inspect the audit chain for diagnostic detail.
        </p>
      ) : null}

      {p.status === "unknown" ? (
        <p style={noteStyle}>
          Publication status unknown (defensive fallback). This usually
          signals an unexpected DB combination and is worth
          investigating.
        </p>
      ) : null}

      {p.published_answer_id ? (
        <div style={fieldRowStyle}>
          <span style={fieldKeyStyle}>published_answer_id</span>
          <span style={monoStyle} title={p.published_answer_id}>
            {shortId(p.published_answer_id, 12)}
          </span>
        </div>
      ) : null}

      {p.published_answer_status ? (
        <div style={fieldRowStyle}>
          <span style={fieldKeyStyle}>published_answer_status</span>
          <span style={monoStyle}>{p.published_answer_status}</span>
        </div>
      ) : null}

      {p.content_hash ? (
        <div style={fieldRowStyle}>
          <span style={fieldKeyStyle}>content_hash</span>
          <span style={monoStyle} title={p.content_hash}>
            {shortId(p.content_hash, 16)}
          </span>
        </div>
      ) : null}

      {p.final_gate_report_id ? (
        <div style={fieldRowStyle}>
          <span style={fieldKeyStyle}>final_gate_report_id</span>
          <span style={monoStyle} title={p.final_gate_report_id}>
            {shortId(p.final_gate_report_id, 12)}
          </span>
        </div>
      ) : null}

      {p.summary_text ? (
        <div style={{ marginTop: 10 }}>
          <div style={{ ...fieldKeyStyle, marginBottom: 4 }}>
            summary_text
          </div>
          <div
            style={{
              padding: 10,
              background: "#fafbfc",
              border: "1px solid #e0e3e7",
              borderRadius: 4,
              fontSize: 13,
              whiteSpace: "pre-wrap",
              maxHeight: 260,
              overflow: "auto",
              color: "#222",
            }}
          >
            {p.summary_text}
          </div>
        </div>
      ) : null}

      <p style={disclaimerStyle}>
        Publication status is ledger-level, not truth-level.
      </p>
    </section>
  );
}
