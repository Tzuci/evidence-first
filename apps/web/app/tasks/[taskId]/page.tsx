import * as React from "react";

import {
  API_BASE_URL,
  ApiError,
  ApiNetworkError,
  getAntiHallucinationReport,
} from "../../../lib/api";

import TaskSummaryView from "../../../components/TaskSummaryView";

/**
 * Server-rendered route for the user-facing Task Summary page
 * (UI-TASK-FLOW-A). This is the simple, product-level view of one
 * task; the technical report viewer at
 * '/tasks/<taskId>/report' remains as the audit/debug surface.
 *
 * Constraints (UI-TASK-FLOW-A §7):
 *   - Async server component; no "use client", no hooks, no
 *     client-side fetching, no polling.
 *   - Uses the existing `getAntiHallucinationReport` client; does
 *     NOT add new endpoints, does NOT modify the backend.
 *   - Inline styles only, in line with the rest of the project.
 *   - The page does NOT recompute the Final Gate, the CVE-lite, the
 *     Source Quality, or the Claim Entailment results. It renders a
 *     simplified summary derived from the already persisted report
 *     payload.
 *
 * Behavior matrix (UI-TASK-FLOW-A §6):
 *   - taskId missing or empty → "Invalid task id".
 *   - 200 happy path → render the summary.
 *   - 404 RESOURCE_NOT_FOUND / details.resource = task_masters →
 *     "Task not found".
 *   - 5xx → "Task summary API error" with the raw envelope.
 *   - Network failure → "API unreachable".
 *   - Any other error → "Unexpected task summary error".
 *
 * Error handling deliberately mirrors the existing report page
 * (`apps/web/app/tasks/[taskId]/report/page.tsx`) so that both
 * pages stay consistent for the reviewer.
 */

// Disable static rendering. The summary is a derived view of mutable
// DB state; static rendering would freeze it at build time.
export const dynamic = "force-dynamic";

interface PageParams {
  taskId: string;
}

const errorPageContainerStyle: React.CSSProperties = {
  // Plain container; reuse the layout's overall padding.
};

const errorHeadingStyle: React.CSSProperties = {
  fontSize: 24,
  fontWeight: 700,
  margin: 0,
  marginBottom: 8,
  color: "#7a1f1f",
};

const errorMessageStyle: React.CSSProperties = {
  fontSize: 14,
  color: "#333",
};

const linksRowStyle: React.CSSProperties = {
  display: "flex",
  gap: 16,
  flexWrap: "wrap",
  marginTop: 20,
};

const linkStyle: React.CSSProperties = {
  color: "#1f3a8a",
  textDecoration: "underline",
  fontSize: 14,
};

const monoCodeStyle: React.CSSProperties = {
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
  background: "#fafbfc",
  border: "1px solid #e0e3e7",
  borderRadius: 3,
  padding: "1px 4px",
};

const detailsBlockStyle: React.CSSProperties = {
  marginTop: 16,
  border: "1px solid #e0e3e7",
  borderRadius: 6,
  background: "#fafbfc",
};

const summaryBlockStyle: React.CSSProperties = {
  padding: "10px 14px",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 600,
  color: "#333",
};

const preBlockStyle: React.CSSProperties = {
  margin: 0,
  padding: 14,
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  fontSize: 12,
  color: "#222",
  background: "#fff",
  borderTop: "1px solid #e0e3e7",
  maxHeight: 320,
  overflow: "auto",
  whiteSpace: "pre",
};

export default async function TaskSummaryPage({
  params,
}: {
  params: Promise<PageParams>;
}): Promise<React.ReactElement> {
  const resolved = await params;
  const rawTaskId = resolved?.taskId ?? "";
  const taskId = typeof rawTaskId === "string" ? rawTaskId.trim() : "";

  if (!taskId) {
    return (
      <InvalidTaskIdShell />
    );
  }

  try {
    const report = await getAntiHallucinationReport(taskId);
    return <TaskSummaryView taskId={taskId} report={report} />;
  } catch (err) {
    return renderErrorPage(err, taskId);
  }
}

// ---------------------------------------------------------------------------
// Error renderers
// ---------------------------------------------------------------------------
function renderErrorPage(
  err: unknown,
  taskId: string
): React.ReactElement {
  if (err instanceof ApiError) {
    if (
      err.status === 404 &&
      err.code === "RESOURCE_NOT_FOUND" &&
      err.details &&
      err.details["resource"] === "task_masters"
    ) {
      return (
        <ErrorShell
          h1="Task not found"
          message={`No task exists with id ${taskId}.`}
          taskId={taskId}
          envelope={{
            code: err.code,
            message: err.message,
            details: err.details,
          }}
        />
      );
    }
    if (err.status >= 500) {
      return (
        <ErrorShell
          h1="Task summary API error"
          message={`The server returned HTTP ${err.status}${
            err.code ? ` (${err.code})` : ""
          }: ${err.message}`}
          taskId={taskId}
          envelope={{
            code: err.code,
            message: err.message,
            details: err.details,
          }}
        />
      );
    }
    return (
      <ErrorShell
        h1="Task summary API error"
        message={`HTTP ${err.status}${
          err.code ? ` (${err.code})` : ""
        }: ${err.message}`}
        taskId={taskId}
        envelope={{
          code: err.code,
          message: err.message,
          details: err.details,
        }}
      />
    );
  }

  if (err instanceof ApiNetworkError) {
    return (
      <ErrorShell
        h1="API unreachable"
        message={err.message}
        taskId={taskId}
        baseUrl={API_BASE_URL}
      />
    );
  }

  const message = err instanceof Error ? err.message : String(err);
  return (
    <ErrorShell
      h1="Unexpected task summary error"
      message={message}
      taskId={taskId}
    />
  );
}

interface ErrorShellProps {
  h1: string;
  message: string;
  taskId: string;
  envelope?: Record<string, unknown>;
  baseUrl?: string;
}

function ErrorShell(props: ErrorShellProps): React.ReactElement {
  return (
    <article
      aria-labelledby="task-summary-error-heading"
      style={errorPageContainerStyle}
    >
      <h1
        id="task-summary-error-heading"
        style={errorHeadingStyle}
      >
        {props.h1}
      </h1>
      <p style={errorMessageStyle}>{props.message}</p>
      {props.baseUrl ? (
        <p style={{ fontSize: 13, color: "#555" }}>
          API base URL: <code style={monoCodeStyle}>{props.baseUrl}</code>
        </p>
      ) : null}
      {props.envelope ? (
        <details style={detailsBlockStyle}>
          <summary style={summaryBlockStyle}>Error envelope</summary>
          <pre style={preBlockStyle}>
            <code>{safeStringify(props.envelope)}</code>
          </pre>
        </details>
      ) : null}
      <nav aria-label="Related pages" style={linksRowStyle}>
        <a
          href={`/tasks/${props.taskId}/report`}
          style={linkStyle}
          data-testid="link-technical-report"
        >
          Open technical report
        </a>
        <a href="/" style={linkStyle} data-testid="link-home">
          Back to home
        </a>
      </nav>
    </article>
  );
}

function InvalidTaskIdShell(): React.ReactElement {
  return (
    <article
      aria-labelledby="task-summary-error-heading"
      style={errorPageContainerStyle}
    >
      <h1
        id="task-summary-error-heading"
        style={errorHeadingStyle}
      >
        Invalid task id
      </h1>
      <p style={errorMessageStyle}>The URL did not include a task id.</p>
      <nav aria-label="Related pages" style={linksRowStyle}>
        <a href="/" style={linkStyle} data-testid="link-home">
          Back to home
        </a>
      </nav>
    </article>
  );
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch (e) {
    return `// Could not serialize value as JSON: ${
      e instanceof Error ? e.message : String(e)
    }`;
  }
}
