import * as React from "react";

/**
 * `LimitationsPanel` renders the `limitations` array of the report
 * verbatim, as a bullet list.
 *
 * Hard constraints (UI-REPORT-A §3.4, §7):
 *   - The panel MUST be visible at all times (no collapse).
 *   - When the array is empty, a fallback message is rendered telling
 *     the reviewer that an empty limitations array is UNEXPECTED. The
 *     backend always emits at least four disclaimers; an empty list is
 *     a signal worth flagging.
 *   - The strings are rendered VERBATIM. We do not paraphrase, filter,
 *     or i18n-translate them.
 */
export interface LimitationsPanelProps {
  limitations: string[];
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

const listStyle: React.CSSProperties = {
  margin: 0,
  paddingLeft: 20,
  fontSize: 13,
  color: "#333",
};

const fallbackStyle: React.CSSProperties = {
  padding: "8px 10px",
  background: "#fff7e6",
  border: "1px solid #f0d8a8",
  borderRadius: 4,
  color: "#5c3d10",
  fontSize: 13,
};

export default function LimitationsPanel(
  props: LimitationsPanelProps
): React.ReactElement {
  const list = Array.isArray(props.limitations) ? props.limitations : [];

  return (
    <section
      aria-labelledby="limitations-heading"
      data-testid="limitations-panel"
      style={sectionStyle}
    >
      <h2 id="limitations-heading" style={headerStyle}>
        Limitations
      </h2>

      {list.length === 0 ? (
        <p style={fallbackStyle}>
          No limitations were returned by the report. Treat this as
          unexpected.
        </p>
      ) : (
        <ul style={listStyle}>
          {list.map((line, idx) => (
            <li key={idx} style={{ marginTop: 4 }}>
              {line}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
