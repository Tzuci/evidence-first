import * as React from "react";

import type {
  AntiHallucinationReport,
  AxisSummary,
  PublicationStatus,
} from "../lib/reportTypes";

/**
 * `TaskSummaryView` renders the user-facing summary of a task derived
 * from the Anti-Hallucination Report payload (UI-TASK-FLOW-A).
 *
 * Hard semantic constraints (UI-TASK-FLOW-A §4, §5, §9):
 *   - This view is a USER-FACING summary, NOT the technical report.
 *     The technical report at /tasks/<taskId>/report stays as the
 *     authoritative audit/debug surface; this page links to it.
 *   - The view is derived/read-only. It does NOT recompute the
 *     Final Gate, does NOT mutate any state, does NOT call any
 *     backend other than via the existing report API client.
 *   - Wording is intentionally sober. See UI-TASK-FLOW-A §9 for the
 *     banned wording list; none of those phrases may appear anywhere
 *     in the rendered output. Publication can be HELD when support
 *     is insufficient; "held" is the safe label.
 *   - We do NOT say that the answer is FALSE when publication is
 *     held; we say support was insufficient. The system does not
 *     prove the world.
 *   - The four high-level checks (quote/hash, source quality,
 *     claim-evidence relation, publication gate) are presented as
 *     separate, orthogonal cards. We never compose a single
 *     aggregate "score".
 *
 * Layout (top-down):
 *   1. Header: title + short description framing the page as a
 *      derived summary.
 *   2. Task request: shows the task objective if exposed.
 *   3. Publication status: friendly label derived from
 *      report.publication.status.
 *   4. Held/not-ready explanation (only when applicable).
 *   5. Answer text section.
 *   6. High-level checks (four cards).
 *   7. Footer links: technical report + home.
 */

export interface TaskSummaryViewProps {
  taskId: string;
  report: AntiHallucinationReport;
}

/**
 * Defensive all-zero axis summary. Used only when a malformed report
 * payload is missing the `axis_summary` block entirely; the normal
 * backend response always carries it. This keeps the user-facing
 * page from crashing on an unexpected shape and degrades gracefully
 * to zero counts.
 */
const EMPTY_AXIS_SUMMARY: AxisSummary = {
  cve_lite: {
    verified_claims_count: 0,
    unverified_claims_count: 0,
    inconclusive_count: 0,
  },
  source_quality: {
    strong_count: 0,
    adequate_count: 0,
    weak_count: 0,
    unsuitable_count: 0,
    unknown_count: 0,
    missing_count: 0,
  },
  claim_entailment: {
    entailed_count: 0,
    partially_supported_count: 0,
    not_supported_count: 0,
    contradicted_count: 0,
    uncertain_count: 0,
    missing_count: 0,
  },
  final_gate: {
    has_blocking_gaps: false,
    has_warnings: false,
    blocking_gap_count: 0,
    warning_gap_count: 0,
  },
};

// ---------------------------------------------------------------------------
// Styles (inline; no Tailwind, no CSS modules — project convention).
// ---------------------------------------------------------------------------
const sectionStyle: React.CSSProperties = {
  marginTop: 16,
  marginBottom: 16,
  padding: 16,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fff",
};

const sectionHeadingStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  margin: 0,
  marginBottom: 8,
  color: "#111",
};

const paragraphStyle: React.CSSProperties = {
  margin: 0,
  marginTop: 6,
  fontSize: 14,
  color: "#222",
  lineHeight: 1.55,
};

const subtleParagraphStyle: React.CSSProperties = {
  margin: 0,
  marginTop: 6,
  fontSize: 13,
  color: "#555",
  lineHeight: 1.5,
};

const conservativeNoteStyle: React.CSSProperties = {
  marginTop: 8,
  padding: "8px 10px",
  background: "#fafbfc",
  border: "1px solid #e0e3e7",
  borderRadius: 4,
  color: "#555",
  fontSize: 13,
  fontStyle: "italic",
};

const statusBadgeBaseStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "3px 10px",
  borderRadius: 4,
  fontSize: 13,
  fontWeight: 600,
  letterSpacing: 0.2,
};

const checksGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
  gap: 12,
  marginTop: 10,
};

const checkCardStyle: React.CSSProperties = {
  padding: 12,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fafbfc",
};

const checkCardTitleStyle: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  margin: 0,
  marginBottom: 6,
  color: "#111",
};

const checkCardBodyStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 13,
  color: "#333",
  lineHeight: 1.5,
};

const checkCountsListStyle: React.CSSProperties = {
  marginTop: 8,
  paddingTop: 8,
  borderTop: "1px solid #e6e9ed",
};

const checkCountRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  fontSize: 12,
  marginTop: 3,
  color: "#444",
};

const checkCountKeyStyle: React.CSSProperties = {
  color: "#666",
};

const checkCountValueStyle: React.CSSProperties = {
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  color: "#111",
};

const linksRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 16,
  flexWrap: "wrap",
  marginTop: 16,
};

const linkStyle: React.CSSProperties = {
  color: "#1f3a8a",
  textDecoration: "underline",
  fontSize: 14,
};

const heldExplanationStyle: React.CSSProperties = {
  marginTop: 8,
  padding: "10px 12px",
  background: "#fdecd4",
  border: "1px solid #f0c890",
  borderRadius: 4,
  color: "#5c3d10",
  fontSize: 13,
  lineHeight: 1.5,
};

const notReadyExplanationStyle: React.CSSProperties = {
  marginTop: 8,
  padding: "10px 12px",
  background: "#e7eef7",
  border: "1px solid #c2d2e6",
  borderRadius: 4,
  color: "#2e4d77",
  fontSize: 13,
  lineHeight: 1.5,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Map a derived publication status from the report payload to a
 * user-friendly label. The raw status is never lost — we expose it
 * as a small secondary tag — but the primary label is sober.
 *
 * Deliberately avoids "verified" and "truth" vocabulary.
 */
function publicationStatusUserLabel(
  status: PublicationStatus | string
): string {
  switch (status) {
    case "published":
      return "Answer available";
    case "publication_held":
      return "Publication held";
    case "not_ready":
      return "Not ready yet";
    case "failed":
      return "Processing failed";
    case "withdrawn":
      return "Withdrawn";
    case "superseded":
      return "Superseded";
    case "unknown":
      return "Unknown status";
    default:
      // An unrecognized status is treated defensively: the user-facing
      // label stays sober ("Unknown status") rather than echoing a raw
      // backend token. The raw value is still shown as the small
      // secondary tag next to the badge, so auditability is preserved.
      return "Unknown status";
  }
}

/**
 * Pick a muted color scheme per status. None of the choices implies
 * truth or celebration: 'published' gets a muted green, 'held' a
 * warm amber, etc. Colors are an aid to scanability, not a semantic
 * signal: the label text always says the same thing.
 */
function publicationStatusColors(
  status: PublicationStatus | string
): { background: string; color: string; border: string } {
  switch (status) {
    case "published":
      return {
        background: "#e7f3ec",
        color: "#1f5d3a",
        border: "#bcdcc8",
      };
    case "publication_held":
      return {
        background: "#fdecd4",
        color: "#7a4a13",
        border: "#f0c890",
      };
    case "withdrawn":
    case "superseded":
      return {
        background: "#eef0f3",
        color: "#4a4f57",
        border: "#d0d4d9",
      };
    case "not_ready":
      return {
        background: "#e7eef7",
        color: "#2e4d77",
        border: "#c2d2e6",
      };
    case "failed":
      return {
        background: "#fbe7e7",
        color: "#7a1f1f",
        border: "#eec0c0",
      };
    case "unknown":
    default:
      return {
        background: "#f0f0f0",
        color: "#3a3a3a",
        border: "#d0d0d0",
      };
  }
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function Header(): React.ReactElement {
  return (
    <header>
      <h1
        style={{
          fontSize: 26,
          fontWeight: 700,
          margin: 0,
          marginBottom: 4,
          color: "#111",
        }}
      >
        Task summary
      </h1>
      <p
        style={{
          margin: 0,
          fontSize: 14,
          color: "#555",
        }}
      >
        A user-facing summary derived from the technical report.
      </p>
      <p
        style={{
          margin: 0,
          marginTop: 4,
          fontSize: 13,
          color: "#666",
          fontStyle: "italic",
        }}
      >
        Derived read-only view; not a new decision.
      </p>
    </header>
  );
}

function TaskRequestSection({
  objective,
}: {
  objective: string | null;
}): React.ReactElement {
  return (
    <section
      aria-labelledby="task-request-heading"
      style={sectionStyle}
    >
      <h2 id="task-request-heading" style={sectionHeadingStyle}>
        Task request
      </h2>
      {objective && objective.trim().length > 0 ? (
        <p
          style={{
            ...paragraphStyle,
            whiteSpace: "pre-wrap",
          }}
          data-testid="task-objective"
        >
          {objective}
        </p>
      ) : (
        <p style={subtleParagraphStyle} data-testid="task-objective-missing">
          The original request is not exposed by this summary payload.
        </p>
      )}
    </section>
  );
}

function PublicationStatusSection({
  status,
  reasonCode,
  hasBlockingGaps,
  hasWarnings,
}: {
  status: PublicationStatus | string;
  reasonCode: string | null;
  hasBlockingGaps: boolean;
  hasWarnings: boolean;
}): React.ReactElement {
  const friendly = publicationStatusUserLabel(status);
  const colors = publicationStatusColors(status);

  return (
    <section
      aria-labelledby="publication-status-heading"
      style={sectionStyle}
    >
      <h2
        id="publication-status-heading"
        style={sectionHeadingStyle}
      >
        Publication status
      </h2>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexWrap: "wrap",
        }}
      >
        <span
          role="status"
          aria-label={`Publication status: ${friendly}`}
          data-testid="user-status-badge"
          data-status={String(status)}
          style={{
            ...statusBadgeBaseStyle,
            background: colors.background,
            color: colors.color,
            border: `1px solid ${colors.border}`,
          }}
        >
          {friendly}
        </span>
        <span
          style={{
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
            fontSize: 12,
            color: "#777",
          }}
        >
          {String(status)}
        </span>
      </div>

      {status === "publication_held" ? (
        <HeldExplanation
          reasonCode={reasonCode}
          hasBlockingGaps={hasBlockingGaps}
          hasWarnings={hasWarnings}
        />
      ) : null}

      {status === "not_ready" ? (
        <p style={notReadyExplanationStyle}>
          This task has not reached the publication step yet. Check
          back later.
        </p>
      ) : null}

      {status === "failed" ? (
        <p style={conservativeNoteStyle}>
          The task did not complete. See the technical report for
          diagnostic detail.
        </p>
      ) : null}

      {status === "withdrawn" ? (
        <p style={conservativeNoteStyle}>
          This task&apos;s answer was withdrawn after publication.
        </p>
      ) : null}

      {status === "superseded" ? (
        <p style={conservativeNoteStyle}>
          This task&apos;s answer was replaced by a newer one.
        </p>
      ) : null}

      {status === "unknown" ? (
        <p style={conservativeNoteStyle}>
          Status unknown (defensive fallback). The technical report
          may have more detail.
        </p>
      ) : null}
    </section>
  );
}

function HeldExplanation({
  reasonCode,
  hasBlockingGaps,
  hasWarnings,
}: {
  reasonCode: string | null;
  hasBlockingGaps: boolean;
  hasWarnings: boolean;
}): React.ReactElement {
  return (
    <div style={heldExplanationStyle} data-testid="held-explanation">
      <p style={{ margin: 0 }}>
        Publication was held because the available checks did not find
        sufficient support for publication.
      </p>
      {reasonCode ? (
        <p
          style={{
            margin: 0,
            marginTop: 6,
            fontSize: 12,
            color: "#5c3d10",
          }}
        >
          Reason recorded by the gate:{" "}
          <code
            style={{
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
              fontSize: 12,
            }}
          >
            {reasonCode}
          </code>
        </p>
      ) : null}
      {hasBlockingGaps || hasWarnings ? (
        <p
          style={{
            margin: 0,
            marginTop: 6,
            fontSize: 12,
            color: "#5c3d10",
          }}
        >
          {hasBlockingGaps
            ? "At least one blocking coverage gap was recorded."
            : null}
          {hasBlockingGaps && hasWarnings ? " " : null}
          {hasWarnings
            ? "Warnings were also recorded on at least one axis."
            : null}
        </p>
      ) : null}
    </div>
  );
}

function AnswerSection({
  summaryText,
  status,
}: {
  summaryText: string | null;
  status: PublicationStatus | string;
}): React.ReactElement {
  const hasText =
    typeof summaryText === "string" && summaryText.trim().length > 0;

  return (
    <section
      aria-labelledby="answer-heading"
      style={sectionStyle}
    >
      <h2 id="answer-heading" style={sectionHeadingStyle}>
        Answer text
      </h2>
      {hasText ? (
        <div
          data-testid="answer-text"
          style={{
            marginTop: 6,
            padding: 10,
            background: "#fafbfc",
            border: "1px solid #e0e3e7",
            borderRadius: 4,
            fontSize: 13,
            whiteSpace: "pre-wrap",
            maxHeight: 280,
            overflow: "auto",
            color: "#222",
          }}
        >
          {summaryText}
        </div>
      ) : (
        <p
          style={subtleParagraphStyle}
          data-testid="answer-text-missing"
        >
          Answer text is not exposed by this MVP-0 summary view yet.
        </p>
      )}
      {hasText && status === "published" ? (
        <p style={conservativeNoteStyle}>
          Answer based on available evidence. The system does not
          guarantee correctness in the world.
        </p>
      ) : null}
    </section>
  );
}

/**
 * One labelled counter row inside a check card. Pure presentation:
 * it shows a key and its numeric (or string) value, nothing else.
 */
function CheckCount({
  label,
  value,
}: {
  label: string;
  value: number | string;
}): React.ReactElement {
  return (
    <div style={checkCountRowStyle}>
      <span style={checkCountKeyStyle}>{label}</span>
      <span style={checkCountValueStyle}>{String(value)}</span>
    </div>
  );
}

function HighLevelChecksSection({
  axisSummary,
}: {
  axisSummary: AxisSummary;
}): React.ReactElement {
  // Defensive reads: the report shape always carries these blocks,
  // but a malformed payload should degrade to zeros rather than
  // crash the user-facing page.
  const cve = axisSummary?.cve_lite;
  const sq = axisSummary?.source_quality;
  const ce = axisSummary?.claim_entailment;
  const fg = axisSummary?.final_gate;

  return (
    <section
      aria-labelledby="high-level-checks-heading"
      style={sectionStyle}
    >
      <h2
        id="high-level-checks-heading"
        style={sectionHeadingStyle}
      >
        High-level checks
      </h2>
      <p style={subtleParagraphStyle}>
        Four separate checks performed on the available evidence.
        Each measures a different thing; they are not combined into
        a single score. The counts below summarize what the checks
        recorded; they do not prove that a claim is correct or that
        no problem exists.
      </p>
      <div style={checksGridStyle}>
        {/* Quote/hash check -------------------------------------- */}
        <article
          aria-label="Quote/hash check"
          data-testid="check-card-quote-hash"
          style={checkCardStyle}
        >
          <h3 style={checkCardTitleStyle}>Quote/hash check</h3>
          <p style={checkCardBodyStyle}>
            Checks whether referenced text can be linked back to
            available source spans.
          </p>
          <div
            style={checkCountsListStyle}
            data-testid="check-counts-quote-hash"
          >
            <CheckCount
              label="verified"
              value={cve?.verified_claims_count ?? 0}
            />
            <CheckCount
              label="unverified"
              value={cve?.unverified_claims_count ?? 0}
            />
            <CheckCount
              label="inconclusive"
              value={cve?.inconclusive_count ?? 0}
            />
          </div>
        </article>

        {/* Source quality signal --------------------------------- */}
        <article
          aria-label="Source quality signal"
          data-testid="check-card-source-quality"
          style={checkCardStyle}
        >
          <h3 style={checkCardTitleStyle}>Source quality signal</h3>
          <p style={checkCardBodyStyle}>
            Shows available source quality signals. It is not proof
            that a claim is correct.
          </p>
          <div
            style={checkCountsListStyle}
            data-testid="check-counts-source-quality"
          >
            <CheckCount label="strong" value={sq?.strong_count ?? 0} />
            <CheckCount
              label="adequate"
              value={sq?.adequate_count ?? 0}
            />
            <CheckCount label="weak" value={sq?.weak_count ?? 0} />
            <CheckCount
              label="unsuitable"
              value={sq?.unsuitable_count ?? 0}
            />
            <CheckCount
              label="unknown"
              value={sq?.unknown_count ?? 0}
            />
            <CheckCount
              label="missing"
              value={sq?.missing_count ?? 0}
            />
          </div>
        </article>

        {/* Claim-evidence relation ------------------------------- */}
        <article
          aria-label="Claim-evidence relation"
          data-testid="check-card-claim-evidence"
          style={checkCardStyle}
        >
          <h3 style={checkCardTitleStyle}>Claim-evidence relation</h3>
          <p style={checkCardBodyStyle}>
            Shows the local relation between claims and the available
            evidence.
          </p>
          <div
            style={checkCountsListStyle}
            data-testid="check-counts-claim-evidence"
          >
            <CheckCount
              label="entailed"
              value={ce?.entailed_count ?? 0}
            />
            <CheckCount
              label="partially_supported"
              value={ce?.partially_supported_count ?? 0}
            />
            <CheckCount
              label="not_supported"
              value={ce?.not_supported_count ?? 0}
            />
            <CheckCount
              label="contradicted"
              value={ce?.contradicted_count ?? 0}
            />
            <CheckCount
              label="uncertain"
              value={ce?.uncertain_count ?? 0}
            />
            <CheckCount
              label="missing"
              value={ce?.missing_count ?? 0}
            />
          </div>
        </article>

        {/* Publication gate -------------------------------------- */}
        <article
          aria-label="Publication gate"
          data-testid="check-card-publication-gate"
          style={checkCardStyle}
        >
          <h3 style={checkCardTitleStyle}>Publication gate</h3>
          <p style={checkCardBodyStyle}>
            Shows whether publication was allowed or held by the
            persisted gate decision.
          </p>
          <div
            style={checkCountsListStyle}
            data-testid="check-counts-publication-gate"
          >
            <CheckCount
              label="blocking_gap_count"
              value={fg?.blocking_gap_count ?? 0}
            />
            <CheckCount
              label="warning_gap_count"
              value={fg?.warning_gap_count ?? 0}
            />
            <CheckCount
              label="has_blocking_gaps"
              value={String(Boolean(fg?.has_blocking_gaps))}
            />
            <CheckCount
              label="has_warnings"
              value={String(Boolean(fg?.has_warnings))}
            />
          </div>
        </article>
      </div>
    </section>
  );
}

function FooterLinks({
  taskId,
}: {
  taskId: string;
}): React.ReactElement {
  return (
    <nav aria-label="Related pages" style={linksRowStyle}>
      <a
        href={`/tasks/${taskId}/report`}
        style={linkStyle}
        data-testid="link-technical-report"
      >
        Open technical report
      </a>
      <a href="/" style={linkStyle} data-testid="link-home">
        Back to home
      </a>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Top-level component
// ---------------------------------------------------------------------------
export default function TaskSummaryView(
  props: TaskSummaryViewProps
): React.ReactElement {
  const { taskId, report } = props;

  const objective = report.task?.objective ?? null;
  const status = report.publication?.status ?? "unknown";
  const reasonCode = report.gate?.reason_code ?? null;
  const summaryText = report.publication?.summary_text ?? null;
  const axisSummary = report.axis_summary ?? EMPTY_AXIS_SUMMARY;
  const hasBlockingGaps = Boolean(
    axisSummary.final_gate?.has_blocking_gaps
  );
  const hasWarnings = Boolean(axisSummary.final_gate?.has_warnings);

  return (
    <article aria-labelledby="task-summary-page-heading">
      <span id="task-summary-page-heading" style={{ display: "none" }}>
        Task summary
      </span>
      <Header />

      <TaskRequestSection objective={objective} />

      <PublicationStatusSection
        status={status}
        reasonCode={reasonCode}
        hasBlockingGaps={hasBlockingGaps}
        hasWarnings={hasWarnings}
      />

      <AnswerSection
        summaryText={summaryText}
        status={status}
      />

      <HighLevelChecksSection axisSummary={axisSummary} />

      <FooterLinks taskId={taskId} />
    </article>
  );
}
