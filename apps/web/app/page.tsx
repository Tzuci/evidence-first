import * as React from "react";

/**
 * Product-oriented home page (UI-HOME-A).
 *
 * This page is the product entrypoint for Evidence-First MVP-0. It
 * does NOT implement task creation, document upload, history
 * browsing, or any other backend-bound operation: those flows are
 * scope of future blocks (UI-CREATE-FLOW-PRE / UI-CREATE-FLOW-CODE).
 *
 * Hard constraints (PHASE_UI-HOME-A §5, §6, §8):
 *   - React server component only: no "use client", no hooks, no
 *     fetch, no API calls, no client-side navigation, no new
 *     routes.
 *   - No misleading wording. The page must never claim that the
 *     system "verifies truth", "eliminates hallucinations", or
 *     produces an "AI verified" answer. Publication can be HELD
 *     when controls find insufficient support; the wording stays
 *     sober.
 *   - The technical report viewer ('/tasks/<taskId>/report') is a
 *     secondary, audit-oriented surface. It is referenced from the
 *     home but the home is NOT the report viewer.
 *   - Inline styles, in line with the rest of the project
 *     (apps/web/app/diagnostic/page.tsx,
 *     apps/web/app/tasks/[taskId]/report/page.tsx). No CSS modules,
 *     no Tailwind, no component libraries.
 *
 * The wording vocabulary is deliberately constrained. The companion
 * test 'apps/web/tests/home.test.tsx' enforces the banned-copy
 * guardrail.
 */

const sectionStyle: React.CSSProperties = {
  marginTop: 24,
  marginBottom: 24,
  padding: 16,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fff",
};

const sectionHeaderStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 600,
  margin: 0,
  marginBottom: 8,
  color: "#111",
};

const paragraphStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 14,
  color: "#222",
  lineHeight: 1.55,
};

const listStyle: React.CSSProperties = {
  margin: 0,
  paddingLeft: 20,
  fontSize: 14,
  color: "#222",
  lineHeight: 1.55,
};

const noteStyle: React.CSSProperties = {
  marginTop: 8,
  fontSize: 12,
  color: "#555",
  fontStyle: "italic",
};

const linkStyle: React.CSSProperties = {
  color: "#1f3a8a",
  textDecoration: "underline",
};

const codeBlockStyle: React.CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
  background: "#fafbfc",
  border: "1px solid #e0e3e7",
  borderRadius: 3,
  display: "block",
  padding: "8px 10px",
  marginTop: 8,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
};

export default function HomePage(): React.ReactElement {
  return (
    <article aria-labelledby="home-page-heading">
      <header>
        <h1
          id="home-page-heading"
          style={{
            fontSize: 28,
            fontWeight: 700,
            margin: 0,
            marginBottom: 6,
            color: "#111",
          }}
        >
          Evidence-First MVP-0
        </h1>
        <p
          style={{
            margin: 0,
            marginBottom: 4,
            fontSize: 15,
            color: "#333",
          }}
        >
          A controlled workflow for producing answers based on
          available evidence.
        </p>
        <p style={{ margin: 0, fontSize: 13, color: "#555" }}>
          MVP-0 uses MockProvider and no external AI calls.
        </p>
      </header>

      <section aria-labelledby="value-heading" style={sectionStyle}>
        <h2 id="value-heading" style={sectionHeaderStyle}>
          What this product does
        </h2>
        <p style={paragraphStyle}>
          Evidence-First works toward a single answer grounded in
          the sources you make available. A user master prompt is
          matched against those sources, claims are extracted, each
          claim is linked to evidence spans, and a set of technical
          checks decides whether the answer is ready for
          publication. Publication can be held when the checks find
          insufficient support.
        </p>
        <p style={noteStyle}>
          The product does not promise that answers are factually
          correct in the world. It records what was checked, what
          was supported by the available sources, and what was not.
        </p>
      </section>

      <section aria-labelledby="flow-heading" style={sectionStyle}>
        <h2 id="flow-heading" style={sectionHeaderStyle}>
          How the workflow is intended to work
        </h2>
        <ol style={listStyle}>
          <li>Write a master prompt describing the answer you need.</li>
          <li>
            Attach or select the available sources (documents) the
            system should use.
          </li>
          <li>Extract claims from the candidate answer.</li>
          <li>Link each claim to evidence spans in the sources.</li>
          <li>
            Run technical checks on each claim and its supporting
            evidence.
          </li>
          <li>
            Publish the answer, or hold publication when support is
            insufficient.
          </li>
        </ol>
        <p style={noteStyle}>
          Steps 3 to 6 are implemented in the backend and the
          worker today. Creating a new request directly from this
          web UI is not available yet.
        </p>
      </section>

      <section aria-labelledby="available-heading" style={sectionStyle}>
        <h2 id="available-heading" style={sectionHeaderStyle}>
          What is available now
        </h2>
        <ul style={listStyle}>
          <li>
            A technical report viewer for tasks that already exist
            in the system.
          </li>
          <li>
            A backend report endpoint that aggregates publication,
            gate, claims, evidence and checks.
          </li>
          <li>
            MVP-0 MockProvider: no external AI calls are made.
          </li>
          <li>Existing task reports can be opened by URL.</li>
        </ul>
      </section>

      <section
        aria-labelledby="not-available-heading"
        style={sectionStyle}
      >
        <h2 id="not-available-heading" style={sectionHeaderStyle}>
          Not available from the UI yet
        </h2>
        <ul style={listStyle}>
          <li>Creating a new request from the browser.</li>
          <li>Uploading or selecting documents from the browser.</li>
          <li>Browsing task history from the browser.</li>
          <li>Editing or publishing answers from the browser.</li>
        </ul>
        <p style={noteStyle}>
          Some underlying APIs and persisted records already exist,
          but these browser workflows are not available in MVP-0.
          They are planned for a later phase.
        </p>
      </section>

      <section aria-labelledby="report-heading" style={sectionStyle}>
        <h2 id="report-heading" style={sectionHeaderStyle}>
          Technical report
        </h2>
        <p style={paragraphStyle}>
          The technical report is a derived read-only view for
          audit and debugging. It is not a new decision: it reads
          what the backend has already persisted and presents it in
          one place. The report is reached via URL once a task id
          is known.
        </p>
        <p style={{ ...paragraphStyle, marginTop: 8, fontSize: 13, color: "#333" }}>
          URL pattern:
        </p>
        <code style={codeBlockStyle} aria-label="Report URL pattern">
          /tasks/&lt;taskId&gt;/report
        </code>
        <p
          style={{
            ...paragraphStyle,
            marginTop: 12,
            fontSize: 13,
            color: "#333",
          }}
        >
          Example with an all-zero task id. This id is not a real
          task: the report will show a &quot;Task not found&quot;
          state unless that exact id exists in the database.
        </p>
        <p style={{ margin: 0, marginTop: 6, fontSize: 13 }}>
          <a
            href="/tasks/00000000-0000-0000-0000-000000000000/report"
            style={linkStyle}
          >
            Open report error-state demo
          </a>
        </p>
        <p style={noteStyle}>
          The report is a secondary, audit-oriented surface. It is
          not the main product UI.
        </p>
      </section>

      <section aria-labelledby="links-heading" style={sectionStyle}>
        <h2 id="links-heading" style={sectionHeaderStyle}>
          Service links
        </h2>
        <ul style={listStyle}>
          <li>
            <a href="/diagnostic" style={linkStyle}>
              /diagnostic
            </a>
            {" \u2014 service health checks"}
          </li>
        </ul>
      </section>
    </article>
  );
}
