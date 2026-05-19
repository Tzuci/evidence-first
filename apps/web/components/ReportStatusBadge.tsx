import * as React from "react";
import type { PublicationStatus } from "../lib/reportTypes";
import { publicationStatusLabel } from "../lib/reportFormatting";

/**
 * `ReportStatusBadge` is the sober status pill used by the publication
 * panel of the report viewer.
 *
 * Hard constraints (UI-REPORT-A §3.3):
 *   - The label vocabulary is `publicationStatusLabel(...)`: never
 *     "Verified", "AI Verified", "Truth", or any equivalent.
 *   - Color is a hint, NOT the only semantic signal: an `aria-label`
 *     mirrors the textual label so screen readers do not depend on
 *     the visual style. Severity is also communicated by text.
 *   - No checkmark glyph for `published`. We deliberately avoid the
 *     "celebratory check" pattern, because the report is not an
 *     assertion of truth.
 */
export interface ReportStatusBadgeProps {
  status: PublicationStatus | string;
}

/**
 * Map a publication status to a small visual cue (background +
 * foreground color). Colors are muted on purpose; see
 * PHASE_UI_PRE.md §11.2.
 */
function styleForStatus(status: PublicationStatus | string): {
  background: string;
  color: string;
  border: string;
} {
  switch (status) {
    case "published":
      // Muted green, NOT a saturated celebratory green.
      return { background: "#e7f3ec", color: "#1f5d3a", border: "#bcdcc8" };
    case "publication_held":
      // Warm/orange to indicate the publication was blocked.
      return { background: "#fdecd4", color: "#7a4a13", border: "#f0c890" };
    case "withdrawn":
    case "superseded":
      // Neutral grey for lifecycle states that are not "active".
      return { background: "#eef0f3", color: "#4a4f57", border: "#d0d4d9" };
    case "not_ready":
      // Light blue for "pending pipeline".
      return { background: "#e7eef7", color: "#2e4d77", border: "#c2d2e6" };
    case "failed":
      // Soft red.
      return { background: "#fbe7e7", color: "#7a1f1f", border: "#eec0c0" };
    case "unknown":
    default:
      return { background: "#f0f0f0", color: "#3a3a3a", border: "#d0d0d0" };
  }
}

export default function ReportStatusBadge(
  props: ReportStatusBadgeProps
): React.ReactElement {
  const label = publicationStatusLabel(props.status);
  const colors = styleForStatus(props.status);
  return (
    <span
      role="status"
      aria-label={`Publication status: ${label}`}
      data-testid="report-status-badge"
      data-status={String(props.status)}
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: 13,
        fontWeight: 600,
        background: colors.background,
        color: colors.color,
        border: `1px solid ${colors.border}`,
        letterSpacing: 0.2,
      }}
    >
      {label}
    </span>
  );
}
