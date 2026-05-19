import * as React from "react";

import {
  API_BASE_URL,
  ApiError,
  ApiNetworkError,
  getAntiHallucinationReport,
} from "../../../../lib/api";
import type { AntiHallucinationReport } from "../../../../lib/reportTypes";
import {
  formatDateTime,
  hasAnyMockIndicator,
} from "../../../../lib/reportFormatting";

import AxisSummaryCards from "../../../../components/AxisSummaryCards";
import GatePanel from "../../../../components/GatePanel";
import LimitationsPanel from "../../../../components/LimitationsPanel";
import MockIndicatorsPanel from "../../../../components/MockIndicatorsPanel";
import PublicationPanel from "../../../../components/PublicationPanel";
import RawJsonCollapsible from "../../../../components/RawJsonCollapsible";

/**
 * Server-rendered route for the Anti-Hallucination Report viewer
 * (UI-REPORT-A). The page is intentionally a plain async server
 * component:
 *
 *   - No client-side state; no hooks; no `"use client"` directive.
 *   - No polling. The reviewer reloads the page to refetch.
 *   - All formatting goes through the `lib/reportFormatting.ts`
 *     helpers so future tone changes happen in one place.
 *
 * Behavior matrix:
 *   - taskId missing or empty → render an "Invalid task id" page.
 *   - 200 happy path → render the report.
 *   - 200 with `publication.status = not_ready` → render with a
 *     soft banner; this is NOT treated as an error.
 *   - 200 with `publication.status = publication_held` → render
 *     normally; the publication panel surfaces the "Held" badge.
 *   - 404 RESOURCE_NOT_FOUND / details.resource = task_masters →
 *     "Task not found" page.
 *   - 5xx → "Report API error" page including the raw envelope.
 *   - network error (fetch threw) → "API unreachable" page.
 *   - any other error → "Unexpected report error" page.
 *
 * The page deliberately does NOT call `notFound()` for 404 (which
 * would lose the envelope detail). The reviewer needs to see the
 * raw `code`/`details` to diagnose the failure.
 */

// Disable static rendering. The report is a derived view of mutable
// DB state; static rendering would freeze it at build time.
export const dynamic = "force-dynamic";

interface PageParams {
  taskId: string;
}

/**
 * Next.js 15 introduces async `params` for dynamic routes: the type
 * is a Promise that the page must await before consuming.
 */
export default async function TaskReportPage({
  params,
}: {
  params: Promise<PageParams>;
}): Promise<React.ReactElement> {
  const resolved = await params;
  const rawTaskId = resolved?.taskId ?? "";
  const taskId = typeof rawTaskId === "string" ? rawTaskId.trim() : "";

  if (!taskId) {
    return (
      <ErrorShell
        h1="Invalid task id"
        message="The URL did not include a task id."
      />
    );
  }

  try {
    const report = await getAntiHallucinationReport(taskId);
    return <ReportView report={report} taskId={taskId} />;
  } catch (err) {
    return renderErrorPage(err, taskId);
  }
}

// ---------------------------------------------------------------------------
// Happy-path render
// ---------------------------------------------------------------------------
function ReportView({
  report,
  taskId,
}: {
  report: AntiHallucinationReport;
  taskId: string;
}): React.ReactElement {
  const showMockBanner = hasAnyMockIndicator(report.mock_indicators);

  return (
    <article aria-labelledby="report-page-heading">
      <h1
        id="report-page-heading"
        style={{
          fontSize: 24,
          fontWeight: 700,
          margin: 0,
          marginBottom: 4,
          color: "#111",
        }}
      >
        Anti-Hallucination Report
      </h1>
      <p
        style={{
          margin: 0,
          marginBottom: 16,
          fontSize: 13,
          color: "#555",
        }}
      >
        Derived read-only view. Not a new decision.
      </p>

      {showMockBanner ? (
        <div
          role="alert"
          data-testid="page-mock-banner"
          style={{
            padding: "10px 12px",
            marginBottom: 16,
            background: "#efe3f5",
            border: "1px solid #d6c1e3",
            borderRadius: 4,
            color: "#3f2257",
            fontSize: 13,
            fontWeight: 500,
          }}
        >
          This task ran on mock evaluator(s). See Mock indicators below.
        </div>
      ) : null}

      <TaskHeader report={report} taskId={taskId} />

      <PublicationPanel publication={report.publication} />

      <GatePanel gate={report.gate} />

      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />

      <ClaimsAndEvidenceCounts
        claimsCount={
          Array.isArray(report.claims) ? report.claims.length : 0
        }
        evidenceCount={
          Array.isArray(report.evidence) ? report.evidence.length : 0
        }
      />

      <MockIndicatorsPanel mockIndicators={report.mock_indicators} />

      <LimitationsPanel limitations={report.limitations} />

      <RawJsonCollapsible data={report} />
    </article>
  );
}

function TaskHeader({
  report,
  taskId,
}: {
  report: AntiHallucinationReport;
  taskId: string;
}): React.ReactElement {
  const t = report.task;
  return (
    <section
      aria-labelledby="task-header-heading"
      style={{
        marginBottom: 16,
        padding: 16,
        border: "1px solid #e0e3e7",
        borderRadius: 6,
        background: "#fff",
      }}
    >
      <h2
        id="task-header-heading"
        style={{
          fontSize: 18,
          fontWeight: 600,
          margin: 0,
          marginBottom: 8,
          color: "#111",
        }}
      >
        Task
      </h2>
      <KvRow label="task_id" value={taskId} mono />
      {report.project_id ? (
        <KvRow label="project_id" value={report.project_id} mono />
      ) : null}
      {report.tenant_id ? (
        <KvRow label="tenant_id" value={report.tenant_id} mono />
      ) : null}
      <KvRow label="status" value={t.status ?? "—"} mono />
      <KvRow label="mode" value={t.mode ?? "—"} mono />
      {t.objective ? (
        <div style={{ marginTop: 6, fontSize: 13, color: "#333" }}>
          <div style={{ color: "#666", marginBottom: 2 }}>objective</div>
          <div style={{ whiteSpace: "pre-wrap" }}>{t.objective}</div>
        </div>
      ) : null}
      <KvRow label="created_at" value={formatDateTime(t.created_at)} />
      <KvRow label="updated_at" value={formatDateTime(t.updated_at)} />
    </section>
  );
}

function KvRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): React.ReactElement {
  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        fontSize: 13,
        marginTop: 4,
        color: "#333",
      }}
    >
      <span style={{ color: "#666", minWidth: 110 }}>{label}</span>
      <span
        style={
          mono
            ? {
                fontFamily:
                  "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                fontSize: 12,
              }
            : undefined
        }
      >
        {value}
      </span>
    </div>
  );
}

function ClaimsAndEvidenceCounts({
  claimsCount,
  evidenceCount,
}: {
  claimsCount: number;
  evidenceCount: number;
}): React.ReactElement {
  return (
    <section
      aria-labelledby="counts-heading"
      style={{
        marginTop: 16,
        marginBottom: 16,
        padding: 16,
        border: "1px solid #e0e3e7",
        borderRadius: 6,
        background: "#fff",
      }}
    >
      <h2
        id="counts-heading"
        style={{
          fontSize: 18,
          fontWeight: 600,
          margin: 0,
          marginBottom: 8,
          color: "#111",
        }}
      >
        Claims and evidence (counts)
      </h2>
      <KvRow label="Claims" value={String(claimsCount)} />
      <KvRow label="Evidence spans" value={String(evidenceCount)} />
      <p
        style={{
          marginTop: 8,
          fontSize: 12,
          color: "#666",
          fontStyle: "italic",
        }}
      >
        Detailed claims and evidence rendering is scope of UI-REPORT-B.
      </p>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Error renderers
// ---------------------------------------------------------------------------
function renderErrorPage(err: unknown, taskId: string): React.ReactElement {
  if (err instanceof ApiError) {
    // 404 with details.resource == 'task_masters' → "Task not found".
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
          envelope={{
            code: err.code,
            message: err.message,
            details: err.details,
          }}
          raw={err.raw}
        />
      );
    }
    if (err.status >= 500) {
      return (
        <ErrorShell
          h1="Report API error"
          message={`The server returned HTTP ${err.status}${
            err.code ? ` (${err.code})` : ""
          }: ${err.message}`}
          envelope={{
            code: err.code,
            message: err.message,
            details: err.details,
          }}
          raw={err.raw}
        />
      );
    }
    return (
      <ErrorShell
        h1="Report API error"
        message={`HTTP ${err.status}${
          err.code ? ` (${err.code})` : ""
        }: ${err.message}`}
        envelope={{
          code: err.code,
          message: err.message,
          details: err.details,
        }}
        raw={err.raw}
      />
    );
  }

  if (err instanceof ApiNetworkError) {
    return (
      <ErrorShell
        h1="API unreachable"
        message={err.message}
        baseUrl={API_BASE_URL}
      />
    );
  }

  const message = err instanceof Error ? err.message : String(err);
  return (
    <ErrorShell
      h1="Unexpected report error"
      message={message}
    />
  );
}

interface ErrorShellProps {
  h1: string;
  message: string;
  envelope?: Record<string, unknown>;
  raw?: string;
  baseUrl?: string;
}

function ErrorShell(props: ErrorShellProps): React.ReactElement {
  return (
    <article aria-labelledby="error-page-heading">
      <h1
        id="error-page-heading"
        style={{
          fontSize: 24,
          fontWeight: 700,
          margin: 0,
          marginBottom: 8,
          color: "#7a1f1f",
        }}
      >
        {props.h1}
      </h1>
      <p style={{ fontSize: 14, color: "#333" }}>{props.message}</p>
      {props.baseUrl ? (
        <p style={{ fontSize: 13, color: "#555" }}>
          API base URL:{" "}
          <code
            style={{
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
              fontSize: 12,
              background: "#fafbfc",
              border: "1px solid #e0e3e7",
              borderRadius: 3,
              padding: "1px 4px",
            }}
          >
            {props.baseUrl}
          </code>
        </p>
      ) : null}
      {props.envelope ? (
        <RawJsonCollapsible
          data={props.envelope}
          summary="Error envelope"
        />
      ) : null}
      {props.raw ? (
        <RawJsonCollapsible
          data={{ raw_response: props.raw }}
          summary="Raw response body"
        />
      ) : null}
    </article>
  );
}
