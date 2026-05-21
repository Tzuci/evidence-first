import * as React from "react";

/**
 * Product entrypoint home page (UI-HOME-B).
 *
 * This page makes the `/` route a usable product entrypoint rather
 * than an informational page. It lets the user understand what
 * Evidence-First MVP-0 does, what is and is not available, and —
 * most importantly — open an existing task by pasting a task id.
 *
 * Hard constraints (PHASE UI-HOME-B §5, §6):
 *   - React server component only: no "use client", no hooks, no
 *     fetch, no API calls, no client-side JavaScript. The single
 *     interactive element is a plain HTML <form> with method="get"
 *     and action="/tasks"; the browser performs the navigation.
 *   - The form does NOT create tasks and does NOT call the backend.
 *     It only navigates to the user-facing task summary route.
 *   - No misleading wording. The page must not overclaim what the
 *     system can establish. Publication can be held when the checks
 *     find insufficient support; the wording stays sober.
 *   - Inline styles only, in line with the rest of the project. No
 *     CSS modules, no Tailwind, no component libraries, no new
 *     dependencies.
 *
 * The companion test `apps/web/tests/home.test.tsx` enforces the
 * banned-copy guardrail and the structural expectations.
 */

// ---------------------------------------------------------------------------
// Shared inline styles
// ---------------------------------------------------------------------------
const heroStyle: React.CSSProperties = {
  padding: "28px 24px",
  border: "1px solid #d6dbe1",
  borderRadius: 8,
  background: "#ffffff",
  marginBottom: 24,
};

const heroTitleStyle: React.CSSProperties = {
  fontSize: 30,
  fontWeight: 700,
  margin: 0,
  marginBottom: 6,
  color: "#111",
};

const heroSubtitleStyle: React.CSSProperties = {
  fontSize: 16,
  fontWeight: 600,
  margin: 0,
  marginBottom: 10,
  color: "#2e4d77",
};

const heroExplanationStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 14,
  color: "#333",
  lineHeight: 1.6,
  maxWidth: 640,
};

const heroNoteStyle: React.CSSProperties = {
  margin: 0,
  marginTop: 8,
  fontSize: 12,
  color: "#666",
};

const primaryCardStyle: React.CSSProperties = {
  marginTop: 20,
  padding: 16,
  border: "1px solid #c2d2e6",
  borderRadius: 6,
  background: "#f3f7fc",
};

const primaryCardTitleStyle: React.CSSProperties = {
  fontSize: 16,
  fontWeight: 600,
  margin: 0,
  marginBottom: 8,
  color: "#1f3a5a",
};

const formRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 8,
  flexWrap: "wrap",
  alignItems: "center",
};

const inputStyle: React.CSSProperties = {
  flex: "1 1 280px",
  minWidth: 220,
  padding: "8px 10px",
  fontSize: 14,
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  border: "1px solid #b9c4d0",
  borderRadius: 4,
  color: "#111",
  background: "#fff",
};

const submitButtonStyle: React.CSSProperties = {
  padding: "8px 16px",
  fontSize: 14,
  fontWeight: 600,
  color: "#fff",
  background: "#1f3a8a",
  border: "1px solid #1a3270",
  borderRadius: 4,
  cursor: "pointer",
};

const helperLineStyle: React.CSSProperties = {
  margin: 0,
  marginTop: 8,
  fontSize: 12,
  color: "#555",
};

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
  marginBottom: 12,
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
  lineHeight: 1.6,
};

const noteStyle: React.CSSProperties = {
  marginTop: 10,
  fontSize: 12,
  color: "#555",
  fontStyle: "italic",
};

const linkStyle: React.CSSProperties = {
  color: "#1f3a8a",
  textDecoration: "underline",
};

const cardGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
  gap: 12,
};

const cardStyle: React.CSSProperties = {
  padding: 14,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fafbfc",
};

const cardTitleStyle: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  margin: 0,
  marginBottom: 6,
  color: "#111",
};

const cardBodyStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 13,
  color: "#333",
  lineHeight: 1.5,
};

const codeInlineStyle: React.CSSProperties = {
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
  background: "#eef0f3",
  borderRadius: 3,
  padding: "1px 4px",
  color: "#1f3a5a",
};

const codeBlockStyle: React.CSSProperties = {
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
  background: "#fafbfc",
  border: "1px solid #e0e3e7",
  borderRadius: 3,
  display: "block",
  padding: "8px 10px",
  marginTop: 8,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
  color: "#222",
};

const workflowGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
  gap: 10,
};

const stepCardStyle: React.CSSProperties = {
  padding: 12,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fafbfc",
};

const stepNumberStyle: React.CSSProperties = {
  display: "inline-block",
  width: 22,
  height: 22,
  lineHeight: "22px",
  textAlign: "center",
  borderRadius: 11,
  background: "#1f3a8a",
  color: "#fff",
  fontSize: 12,
  fontWeight: 700,
  marginBottom: 6,
};

const stepTitleStyle: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  margin: 0,
  marginBottom: 4,
  color: "#111",
};

const stepBodyStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 12,
  color: "#444",
  lineHeight: 1.5,
};

// ---------------------------------------------------------------------------
// Static data
// ---------------------------------------------------------------------------
const WORKFLOW_STEPS: { title: string; body: string }[] = [
  {
    title: "User request",
    body: "The task starts from a user objective.",
  },
  {
    title: "Available sources",
    body: "The system works from sources attached to the task.",
  },
  {
    title: "Claims extracted",
    body: "Candidate answer claims are split into checkable units.",
  },
  {
    title: "Evidence spans linked",
    body: "Claims are linked to available source spans.",
  },
  {
    title: "Checks performed",
    body:
      "Quote/hash, source quality, claim-evidence relation and the " +
      "publication gate are read from persisted pipeline results.",
  },
  {
    title: "Publication allowed or held",
    body:
      "The persisted gate can allow publication or hold it when " +
      "support is insufficient.",
  },
  {
    title: "Summary and report available",
    body:
      "Users can open the task summary first, then the technical " +
      "report for audit and debugging.",
  },
];

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------
function Hero(): React.ReactElement {
  return (
    <header style={heroStyle}>
      <h1 id="home-page-heading" style={heroTitleStyle}>
        Evidence-First MVP-0
      </h1>
      <p style={heroSubtitleStyle}>
        Controlled answers from available evidence.
      </p>
      <p style={heroExplanationStyle}>
        Evidence-First checks generated answers against the sources
        available to the task and may hold publication when support
        is insufficient.
      </p>
      <p style={heroNoteStyle}>
        MVP-0 uses MockProvider and makes no external AI calls.
      </p>

      <section
        aria-labelledby="open-task-heading"
        style={primaryCardStyle}
      >
        <h2 id="open-task-heading" style={primaryCardTitleStyle}>
          Open existing task
        </h2>
        <p
          style={{
            ...paragraphStyle,
            fontSize: 13,
            marginBottom: 10,
          }}
        >
          Paste an existing task id to open its user-facing task
          summary.
        </p>
        <form action="/tasks" method="get">
          <div style={formRowStyle}>
            <input
              type="text"
              name="taskId"
              placeholder="Task id (UUID)"
              aria-label="Task id"
              autoComplete="off"
              style={inputStyle}
            />
            <button type="submit" style={submitButtonStyle}>
              Open task summary
            </button>
          </div>
        </form>
        <p style={helperLineStyle}>
          Need a task id? Query your local DB or use an id from an
          existing report.
        </p>
      </section>
    </header>
  );
}

function WhatYouCanDo(): React.ReactElement {
  return (
    <section aria-labelledby="available-heading" style={sectionStyle}>
      <h2 id="available-heading" style={sectionHeaderStyle}>
        What you can do now
      </h2>
      <div style={cardGridStyle}>
        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Open an existing task summary</h3>
          <p style={cardBodyStyle}>
            Route:{" "}
            <code style={codeInlineStyle}>/tasks/&lt;taskId&gt;</code>
            . User-facing view; the best starting point.
          </p>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Open a technical report</h3>
          <p style={cardBodyStyle}>
            Route:{" "}
            <code style={codeInlineStyle}>
              /tasks/&lt;taskId&gt;/report
            </code>
            . Audit and debugging view.
          </p>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Check backend health</h3>
          <p style={cardBodyStyle}>
            Call the API health endpoints directly:
          </p>
          <code style={codeBlockStyle} aria-label="API health endpoints">
            {"http://localhost:8000/health/live\n" +
              "http://localhost:8000/health/ready"}
          </code>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Inspect local task ids</h3>
          <p style={cardBodyStyle}>
            Task ids come from existing backend and database state.
            This browser UI does not run database queries. From a
            shell you can list them with a pattern such as:
          </p>
          <code style={codeBlockStyle} aria-label="Local task id command pattern">
            docker exec -it ef_db ...
          </code>
        </article>
      </div>
    </section>
  );
}

function WhichPage(): React.ReactElement {
  return (
    <section aria-labelledby="which-page-heading" style={sectionStyle}>
      <h2 id="which-page-heading" style={sectionHeaderStyle}>
        Which page should I open?
      </h2>
      <div style={cardGridStyle}>
        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Task summary</h3>
          <p style={cardBodyStyle}>
            User-facing view and the best starting point. It shows
            the task objective, publication status, the answer text
            when it is exposed, and high-level checks.
          </p>
          <p style={{ ...cardBodyStyle, marginTop: 8 }}>
            URL:{" "}
            <code style={codeInlineStyle}>/tasks/&lt;taskId&gt;</code>
          </p>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Technical report</h3>
          <p style={cardBodyStyle}>
            Audit and debugging view. It shows gate details, axis
            summaries, mock indicators, limitations and raw report
            data.
          </p>
          <p style={{ ...cardBodyStyle, marginTop: 8 }}>
            URL:{" "}
            <code style={codeInlineStyle}>
              /tasks/&lt;taskId&gt;/report
            </code>
          </p>
        </article>
      </div>
      <p style={noteStyle}>
        The task summary is a derived read-only view; the technical
        report is also derived and read-only. Neither page makes a
        new decision.
      </p>
    </section>
  );
}

function Workflow(): React.ReactElement {
  return (
    <section aria-labelledby="workflow-heading" style={sectionStyle}>
      <h2 id="workflow-heading" style={sectionHeaderStyle}>
        How the workflow works
      </h2>
      <div style={workflowGridStyle}>
        {WORKFLOW_STEPS.map((step, idx) => (
          <article key={step.title} style={stepCardStyle}>
            <span style={stepNumberStyle}>{idx + 1}</span>
            <h3 style={stepTitleStyle}>{step.title}</h3>
            <p style={stepBodyStyle}>{step.body}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

function NotAvailable(): React.ReactElement {
  return (
    <section
      aria-labelledby="not-available-heading"
      style={sectionStyle}
    >
      <h2 id="not-available-heading" style={sectionHeaderStyle}>
        Not available in the browser yet
      </h2>
      <ul style={listStyle}>
        <li>Creating a new task from the browser.</li>
        <li>Uploading or selecting documents from the browser.</li>
        <li>Browsing task history from the browser.</li>
        <li>Editing or publishing answers from the browser.</li>
      </ul>
      <p style={noteStyle}>
        The underlying backend and pipeline pieces already support
        parts of the flow, but these browser workflows are planned
        for later phases.
      </p>
    </section>
  );
}

function ServiceLinks(): React.ReactElement {
  return (
    <section aria-labelledby="links-heading" style={sectionStyle}>
      <h2 id="links-heading" style={sectionHeaderStyle}>
        Service links
      </h2>
      <ul style={listStyle}>
        <li>
          API health (live):{" "}
          <a
            href="http://localhost:8000/health/live"
            style={linkStyle}
          >
            http://localhost:8000/health/live
          </a>
        </li>
        <li>
          API health (ready):{" "}
          <a
            href="http://localhost:8000/health/ready"
            style={linkStyle}
          >
            http://localhost:8000/health/ready
          </a>
        </li>
        <li>
          Legacy diagnostic page:{" "}
          <a href="/diagnostic" style={linkStyle}>
            /diagnostic
          </a>{" "}
          — may show a proxy-health 404 in the current MVP-0. Prefer
          the direct API health links above.
        </li>
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Top-level page
// ---------------------------------------------------------------------------
export default function HomePage(): React.ReactElement {
  return (
    <article aria-labelledby="home-page-heading">
      <Hero />
      <WhatYouCanDo />
      <WhichPage />
      <Workflow />
      <NotAvailable />
      <ServiceLinks />
    </article>
  );
}
