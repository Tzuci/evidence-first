/**
 * Pure formatting helpers for the Anti-Hallucination Report viewer
 * (UI-REPORT-A).
 *
 * None of these helpers mutate input. None call the network. None
 * introduce a library dependency (date-fns / dayjs / etc.) — we use
 * the native `Date` parser with a defensive fallback so a malformed
 * timestamp surfaces verbatim instead of throwing.
 *
 * Semantic note: helpers like `publicationStatusLabel` and
 * `gateDecisionLabel` produce HUMAN labels for display. The raw
 * backend strings (`reason_code` codes, etc.) MUST always remain
 * visible elsewhere in the UI (monospace, alongside the label). The
 * goal of the label is readability; the goal of the raw code is
 * auditability.
 */

import type {
  GateDecision,
  MockIndicators,
  PublicationStatus,
} from "./reportTypes";

/**
 * Placeholder rendered when a value is null/undefined/empty. Em dash
 * is the conventional "no data" glyph.
 */
const EMPTY_PLACEHOLDER = "—";

/**
 * Format an ISO 8601 datetime string into a human-readable form.
 *
 * Rules:
 *   - null / undefined / empty → "—".
 *   - parseable date → ISO display: `YYYY-MM-DD HH:MM:SS UTC`. We
 *     deliberately render in UTC (no locale-dependent formatting)
 *     to keep test output deterministic and to avoid surprising the
 *     reviewer with timezone-shifted timestamps that disagree with
 *     `psql` output.
 *   - unparseable date → original string (defensive; surfaces the bug
 *     without crashing).
 */
export function formatDateTime(
  value: string | null | undefined
): string {
  if (value === null || value === undefined || value === "") {
    return EMPTY_PLACEHOLDER;
  }
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) {
    return value;
  }
  // YYYY-MM-DD HH:MM:SS UTC (deterministic across timezones).
  const yyyy = d.getUTCFullYear().toString().padStart(4, "0");
  const mm = (d.getUTCMonth() + 1).toString().padStart(2, "0");
  const dd = d.getUTCDate().toString().padStart(2, "0");
  const hh = d.getUTCHours().toString().padStart(2, "0");
  const mi = d.getUTCMinutes().toString().padStart(2, "0");
  const ss = d.getUTCSeconds().toString().padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}:${ss} UTC`;
}

/**
 * Truncate a UUID (or any long identifier) to its first `chars`
 * characters, suffixed with an ellipsis when truncated. Useful for
 * showing UUIDs in tight layouts while keeping the full value
 * available via tooltip elsewhere.
 *
 * Rules:
 *   - null / undefined / empty → "—".
 *   - shorter than `chars` → returned unchanged.
 *   - otherwise → first `chars` characters + "…".
 *
 * Default `chars` is 8: enough to disambiguate within a single task
 * scope without dominating the layout.
 */
export function shortId(
  value: string | null | undefined,
  chars: number = 8
): string {
  if (value === null || value === undefined || value === "") {
    return EMPTY_PLACEHOLDER;
  }
  if (chars <= 0) {
    return EMPTY_PLACEHOLDER;
  }
  if (value.length <= chars) {
    return value;
  }
  return `${value.slice(0, chars)}…`;
}

/**
 * Human-readable label for a derived publication status. The raw
 * backend value (e.g. `publication_held`) MUST be shown alongside the
 * label elsewhere in the UI — labels are NOT a substitute for the
 * raw status code.
 *
 * The label vocabulary is deliberately sober:
 *   - "Published" (not "✓ Verified", "Truth confirmed", etc.);
 *   - "Held" (not "Failed", "Blocked", etc.);
 *   - "Not ready" instead of "Pending" — clearer that the task did
 *     not reach the gate yet.
 *
 * `unknown` surfaces as "Unknown" so the reviewer sees the defensive
 * fallback explicitly.
 */
export function publicationStatusLabel(
  status: PublicationStatus | string
): string {
  switch (status) {
    case "published":
      return "Published";
    case "withdrawn":
      return "Withdrawn";
    case "superseded":
      return "Superseded";
    case "publication_held":
      return "Held";
    case "not_ready":
      return "Not ready";
    case "failed":
      return "Failed";
    case "unknown":
      return "Unknown";
    default:
      return String(status);
  }
}

/**
 * Human-readable label for the Final Answer Gate decision.
 *
 * - "approved" / "rejected" capitalized.
 * - `null` (no gate report yet) → "No gate report".
 * - anything else → echoed verbatim.
 */
export function gateDecisionLabel(decision: GateDecision | string): string {
  if (decision === null || decision === undefined) {
    return "No gate report";
  }
  switch (decision) {
    case "approved":
      return "Approved";
    case "rejected":
      return "Rejected";
    default:
      return String(decision);
  }
}

/**
 * Return true when the publication status is a terminal lifecycle
 * state (no further pipeline progress expected without an explicit
 * intervention).
 *
 * Used by the page to disable auto-refresh hints. Today the UI does
 * NOT auto-poll, but this helper is convenient for the eventual
 * UI-CREATE-FLOW polling logic and is kept here so the vocabulary
 * stays in one place.
 *
 * Terminal: published, withdrawn, superseded, publication_held,
 * failed.
 * Non-terminal: not_ready (gate not reached yet), unknown (defensive
 * fallback — we cannot tell).
 */
export function isTerminalPublicationStatus(
  status: PublicationStatus | string
): boolean {
  return (
    status === "published" ||
    status === "withdrawn" ||
    status === "superseded" ||
    status === "publication_held" ||
    status === "failed"
  );
}

/**
 * Return true if any of the four `mock_indicators` flags is true.
 *
 * Used by the page to decide whether to render the top-of-page
 * "mock evaluator(s)" banner. Defensive: missing flags are treated
 * as false (the banner should err on the side of NOT showing when
 * data is malformed, rather than show a false alarm).
 */
export function hasAnyMockIndicator(
  indicators: MockIndicators | null | undefined
): boolean {
  if (!indicators) {
    return false;
  }
  return Boolean(
    indicators.uses_mock_source_quality ||
      indicators.uses_mock_claim_entailment ||
      indicators.uses_mock_compiler ||
      indicators.uses_mock_cve_lite
  );
}
