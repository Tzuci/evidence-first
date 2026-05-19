import * as React from "react";

/**
 * `RawJsonCollapsible` renders an arbitrary value as pretty-printed
 * JSON inside a native `<details>` block, closed by default.
 *
 * Why a native `<details>`:
 *   - Zero JS for the collapse behavior.
 *   - Built-in keyboard accessibility.
 *   - Server-component-friendly: nothing to hydrate.
 *
 * The component falls back to a plain "could not serialize" message
 * if `JSON.stringify` throws (e.g. circular references). This is
 * defensive — the report payload from the backend is always
 * serializable, but the component is reused for error envelopes
 * which may include unexpected shapes.
 */
export interface RawJsonCollapsibleProps {
  data: unknown;
  summary?: string;
}

const detailsStyle: React.CSSProperties = {
  marginTop: 16,
  marginBottom: 16,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fafbfc",
};

const summaryStyle: React.CSSProperties = {
  padding: "10px 14px",
  cursor: "pointer",
  fontSize: 14,
  fontWeight: 600,
  color: "#333",
  userSelect: "none",
};

const preStyle: React.CSSProperties = {
  margin: 0,
  padding: 14,
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
  color: "#222",
  background: "#fff",
  borderTop: "1px solid #e0e3e7",
  maxHeight: 480,
  overflow: "auto",
  whiteSpace: "pre",
};

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch (e) {
    return `// Could not serialize value as JSON: ${
      e instanceof Error ? e.message : String(e)
    }`;
  }
}

export default function RawJsonCollapsible(
  props: RawJsonCollapsibleProps
): React.ReactElement {
  const summary = props.summary ?? "Raw report JSON";
  const text = safeStringify(props.data);
  return (
    <details style={detailsStyle}>
      <summary style={summaryStyle}>{summary}</summary>
      <pre style={preStyle}>
        <code>{text}</code>
      </pre>
    </details>
  );
}
