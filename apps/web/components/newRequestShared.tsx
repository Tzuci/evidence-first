import * as React from "react";
import { ApiError, ApiNetworkError, API_BASE_URL } from "../lib/api";

/**
 * Shared inline styles and error helpers for the request-creation
 * flow components (Phase UI-CREATE-FLOW-A).
 *
 * Centralizing the styles here keeps every section component
 * consistent without introducing CSS modules, Tailwind, or a new
 * dependency — inline styles only, in line with the rest of the
 * project (PHASE UI-CREATE-FLOW-A §12). The visual language matches
 * the existing home page and report viewer (muted blues / greys,
 * system font stack, sober tone).
 */

// ---------------------------------------------------------------------------
// Section layout
// ---------------------------------------------------------------------------
export const sectionStyle: React.CSSProperties = {
  marginTop: 20,
  marginBottom: 20,
  padding: 18,
  border: "1px solid #e0e3e7",
  borderRadius: 8,
  background: "#fff",
};

export const sectionHeaderStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  margin: 0,
  marginBottom: 4,
  color: "#111",
};

export const sectionStepLabelStyle: React.CSSProperties = {
  display: "inline-block",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: 0.6,
  textTransform: "uppercase",
  color: "#2e4d77",
  marginBottom: 6,
};

export const helperTextStyle: React.CSSProperties = {
  margin: 0,
  marginBottom: 12,
  fontSize: 13,
  color: "#555",
  lineHeight: 1.55,
};

// ---------------------------------------------------------------------------
// Form controls
// ---------------------------------------------------------------------------
export const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: 13,
  fontWeight: 600,
  color: "#333",
  marginBottom: 4,
};

export const textInputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "8px 10px",
  fontSize: 14,
  border: "1px solid #b9c4d0",
  borderRadius: 4,
  color: "#111",
  background: "#fff",
  fontFamily: "inherit",
};

export const textAreaStyle: React.CSSProperties = {
  ...textInputStyle,
  minHeight: 90,
  resize: "vertical",
  lineHeight: 1.5,
};

export const selectStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "8px 10px",
  fontSize: 14,
  border: "1px solid #b9c4d0",
  borderRadius: 4,
  color: "#111",
  background: "#fff",
  fontFamily: "inherit",
};

export const primaryButtonStyle: React.CSSProperties = {
  padding: "9px 18px",
  fontSize: 14,
  fontWeight: 600,
  color: "#fff",
  background: "#1f3a8a",
  border: "1px solid #1a3270",
  borderRadius: 4,
  cursor: "pointer",
};

export const secondaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  fontSize: 13,
  fontWeight: 600,
  color: "#1f3a5a",
  background: "#f3f7fc",
  border: "1px solid #c2d2e6",
  borderRadius: 4,
  cursor: "pointer",
};

export const disabledButtonStyle: React.CSSProperties = {
  opacity: 0.5,
  cursor: "not-allowed",
};

// ---------------------------------------------------------------------------
// Inline error panel
// ---------------------------------------------------------------------------
export const errorPanelStyle: React.CSSProperties = {
  marginTop: 12,
  padding: "10px 12px",
  background: "#fbe7e7",
  border: "1px solid #eec0c0",
  borderRadius: 4,
  color: "#7a1f1f",
  fontSize: 13,
  lineHeight: 1.5,
};

export const detailsBlockStyle: React.CSSProperties = {
  marginTop: 8,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fafbfc",
};

export const detailsSummaryStyle: React.CSSProperties = {
  padding: "8px 12px",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 600,
  color: "#555",
};

export const preBlockStyle: React.CSSProperties = {
  margin: 0,
  padding: 12,
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 11,
  color: "#222",
  background: "#fff",
  borderTop: "1px solid #e0e3e7",
  maxHeight: 240,
  overflow: "auto",
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
};

export const monoStyle: React.CSSProperties = {
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
};

// ---------------------------------------------------------------------------
// Error helpers
// ---------------------------------------------------------------------------

/**
 * A normalized, render-ready description of a thrown error from the
 * API client.
 */
export interface DescribedError {
  /** Short headline, e.g. "API unreachable" or "HTTP 409 (RESOURCE_CONFLICT)". */
  headline: string;
  /** Human-readable detail line. */
  message: string;
  /** Backend error code when available (e.g. "RESOURCE_CONFLICT"). */
  code: string | null;
  /** Raw error envelope, suitable for a <details> block. May be null. */
  envelope: Record<string, unknown> | null;
}

/**
 * Map a thrown error from the API client onto a `DescribedError`.
 *
 * Handles `ApiError` (non-2xx — surfaces status + code + envelope),
 * `ApiNetworkError` (fetch failed — surfaces the configured base
 * URL), and any other thrown value (defensive fallback). It NEVER
 * masks an error with an empty state: there is always a headline and
 * a message.
 */
export function describeError(err: unknown): DescribedError {
  if (err instanceof ApiError) {
    const codePart = err.code ? ` (${err.code})` : "";
    return {
      headline:
        err.status === 0
          ? "Unexpected response"
          : `HTTP ${err.status}${codePart}`,
      message: err.message,
      code: err.code,
      envelope: {
        status: err.status,
        code: err.code,
        message: err.message,
        details: err.details,
        raw: err.raw,
      },
    };
  }
  if (err instanceof ApiNetworkError) {
    return {
      headline: "API unreachable",
      message: `${err.message} (base URL: ${API_BASE_URL})`,
      code: null,
      envelope: null,
    };
  }
  const message = err instanceof Error ? err.message : String(err);
  return {
    headline: "Unexpected error",
    message,
    code: null,
    envelope: null,
  };
}

/**
 * `true` when the error is an `ApiError` whose backend code matches
 * `code`. Used by the project section to detect `RESOURCE_CONFLICT`
 * specifically.
 */
export function isApiErrorCode(err: unknown, code: string): boolean {
  return err instanceof ApiError && err.code === code;
}

/**
 * Inline error panel: renders a `DescribedError` with an optional
 * collapsible raw envelope. Never silently empty — if `error` is
 * given, the panel always shows at least a headline and message.
 */
export function InlineError({
  error,
  testId,
}: {
  error: DescribedError;
  testId?: string;
}): React.ReactElement {
  return (
    <div role="alert" data-testid={testId} style={errorPanelStyle}>
      <strong>{error.headline}</strong>
      <div style={{ marginTop: 4 }}>{error.message}</div>
      {error.envelope ? (
        <details style={detailsBlockStyle}>
          <summary style={detailsSummaryStyle}>
            Raw error envelope
          </summary>
          <pre style={preBlockStyle}>
            <code>{safeStringify(error.envelope)}</code>
          </pre>
        </details>
      ) : null}
    </div>
  );
}

/** JSON.stringify with a defensive fallback for non-serializable values. */
export function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch (e) {
    return `// Could not serialize value as JSON: ${
      e instanceof Error ? e.message : String(e)
    }`;
  }
}
