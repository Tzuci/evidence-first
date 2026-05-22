import * as React from "react";

/**
 * Product entrypoint home page.
 *
 * Updated by Phase UI-CREATE-FLOW-A: the home page now makes
 * "New evidence-based request" the PRIMARY action, linking to
 * `/requests/new`, where the user can create a real task from the
 * browser without knowing a task id. "Open existing task" — the
 * plain HTML GET form that forwards a pasted task id to its summary
 * page — is kept as the SECONDARY action for developers who already
 * have an id.
 *
 * Hard constraints (PHASE UI-CREATE-FLOW-A §8, §12):
 *   - React server component only: no "use client", no hooks, no
 *     fetch, no API calls. The primary CTA is a plain link to
 *     `/requests/new`; the secondary action is a plain HTML <form>
 *     with method="get" and action="/tasks". Neither creates a task
 *     here — task creation happens on `/requests/new`.
 *   - No misleading wording. The page must not overclaim what the
 *     system can establish. Publication can be held when the checks
 *     find insufficient support; the wording stays sober.
 *   - Inline styles only, in line with the rest of the project. No
 *     CSS modules, no Tailwind, no component libraries, no new
 *     dependencies.
 *   - The Task summary vs Technical report distinction, the MVP
 *     limitations, the direct health links and the legacy
 *     `/diagnostic` note are all kept; only the hierarchy changes so
 *     the home no longer feels like the user must already know a
 *     task id.
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
  padding: 18,
  border: "1px solid #1f3a8a",
  borderRadius: 6,
  background: "#eef3fb",
};

const primaryCardTitleStyle: React.CSSProperties = {
  fontSize: 18,
  fontWeight: 700,
  margin: 0,
  marginBottom: 8,
  color: "#1f2f57",
};

const primaryCtaLinkStyle: React.CSSProperties = {
  display: "inline-block",
  marginTop: 6,
  padding: "10px 20px",
  fontSize: 15,
  fontWeight: 600,
  color: "#fff",
  background: "#1f3a8a",
  border: "1px solid #1a3270",
  borderRadius: 4,
  textDecoration: "none",
};

const secondaryCardStyle: React.CSSProperties = {
  marginTop: 16,
  padding: 16,
  border: "1px solid #d6dbe1",
  borderRadius: 6,
  background: "#fafbfc",
};

const secondaryCardTitleStyle: React.CSSProperties = {
  fontSize: 15,
  fontWeight: 600,
  margin: 0,
  marginBottom: 8,
  color: "#444",
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
  color: "#1f3a5a",
  background: "#f3f7fc",
  border: "1px solid #c2d2e6",
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

      {/* Primary action: start a new evidence-based request. */}
      <section
        aria-labelledby="new-request-cta-heading"
        style={primaryCardStyle}
      >
        <h2
          id="new-request-cta-heading"
          style={primaryCardTitleStyle}
        >
          Start here
        </h2>
        <p
          style={{
            margin: 0,
            marginBottom: 4,
            fontSize: 13,
            color: "#333",
            lineHeight: 1.55,
          }}
        >
          Begin from what you need: choose a project, attach sources,
          write your request, and the system creates the task for
          you. You do not need a task id.
        </p>
        <a
          href="/requests/new"
          style={primaryCtaLinkStyle}
          data-testid="primary-cta-new-request"
        >
          New evidence-based request
        </a>
      </section>

      {/* Secondary action: open an existing task by id. */}
      <section
        aria-labelledby="open-task-heading"
        style={secondaryCardStyle}
      >
        <h2 id="open-task-heading" style={secondaryCardTitleStyle}>
          Open existing task
        </h2>
        <p
          style={{
            margin: 0,
            marginBottom: 10,
            fontSize: 13,
            color: "#555",
          }}
        >
          Already have a task id? Paste it to open its user-facing
          task summary. This is a shortcut for developers; most users
          should start a new request above.
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
          <h3 style={cardTitleStyle}>
            Start a new evidence-based request
          </h3>
          <p style={cardBodyStyle}>
            Route:{" "}
            <code style={codeInlineStyle}>/requests/new</code>. Create
            a project, attach sources, write your request, and create
            a task — the recommended starting point.
          </p>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Open an existing task summary</h3>
          <p style={cardBodyStyle}>
            Route:{" "}
            <code style={codeInlineStyle}>/tasks/&lt;taskId&gt;</code>
            . User-facing view for a task you already have an id for.
          </p>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Open a technical report</h3>
          <p style={cardBodyStyle}>
            Route:{" "}
            <code style={codeInlineStyle}>
              /tasks/&lt;taskId&gt;/report
            </code>
            . Secondary audit and debugging view.
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
            User-facing view and the best starting point after a task
            is created. It shows the task objective, publication
            status, the answer text when it is exposed, and
            high-level checks.
          </p>
          <p style={{ ...cardBodyStyle, marginTop: 8 }}>
            URL:{" "}
            <code style={codeInlineStyle}>/tasks/&lt;taskId&gt;</code>
          </p>
        </article>

        <article style={cardStyle}>
          <h3 style={cardTitleStyle}>Technical report</h3>
          <p style={cardBodyStyle}>
            Secondary audit and debugging view. It shows gate details,
            axis summaries, mock indicators, limitations and raw
            report data.
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

function MvpLimitations(): React.ReactElement {
  return (
    <section
      aria-labelledby="mvp-limitations-heading"
      style={sectionStyle}
    >
      <h2 id="mvp-limitations-heading" style={sectionHeaderStyle}>
        MVP-0 limitations
      </h2>
      <ul style={listStyle}>
        <li>
          Browsing task history from the browser is not available
          yet.
        </li>
        <li>
          Editing, deleting or publishing answers from the browser is
          not available.
        </li>
        <li>
          Processing runs on MockProvider; there are no external AI
          calls.
        </li>
        <li>
          Publication can be held when the available checks find
          support is insufficient. A held publication does not mean
          the answer is false in the world.
        </li>
      </ul>
      <p style={noteStyle}>
        Creating a task and attaching sources are available from the
        browser via the new request flow. Other browser workflows are
        planned for later phases.
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
      <MvpLimitations />
      <ServiceLinks />
    </article>
  );
}
