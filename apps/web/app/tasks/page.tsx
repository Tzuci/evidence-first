import * as React from "react";
import { redirect } from "next/navigation";

/**
 * Server-rendered `/tasks` index route (UI-HOME-B).
 *
 * Purpose: receive the plain HTML GET form submitted from the home
 * page ("Open existing task") and turn a pasted task id into a
 * redirect to the user-facing Task Summary page at
 * `/tasks/<taskId>`.
 *
 * Hard constraints (PHASE UI-HOME-B §4.2, §5, §8.2):
 *   - Async server component only: no `"use client"`, no hooks, no
 *     client-side JavaScript.
 *   - This route does NOT create tasks, does NOT call the backend,
 *     does NOT validate whether the task exists. Existence is the
 *     concern of `/tasks/[taskId]`, which already renders a
 *     "Task not found" state for unknown ids.
 *   - No backend client imports, no fetch.
 *
 * Behavior:
 *   - `?taskId=<non-empty>` → redirect to
 *     `/tasks/<encodeURIComponent(trimmed)>`.
 *   - missing / empty `taskId` → render a small guidance page that
 *     points the user back to the home page form.
 *
 * Next.js 15 delivers `searchParams` as a Promise that must be
 * awaited before use, matching the existing dynamic routes
 * (`apps/web/app/tasks/[taskId]/page.tsx`).
 */

// The route inspects request-time search params, so it must not be
// statically rendered.
export const dynamic = "force-dynamic";

interface TasksIndexPageProps {
  searchParams: Promise<{
    taskId?: string | string[];
  }>;
}

const containerStyle: React.CSSProperties = {
  // Plain container; the layout already supplies the outer padding.
};

const headingStyle: React.CSSProperties = {
  fontSize: 24,
  fontWeight: 700,
  margin: 0,
  marginBottom: 8,
  color: "#111",
};

const paragraphStyle: React.CSSProperties = {
  fontSize: 14,
  color: "#333",
  lineHeight: 1.55,
  margin: 0,
  marginTop: 6,
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

export default async function TasksIndexPage({
  searchParams,
}: TasksIndexPageProps): Promise<React.ReactElement> {
  const resolved = await searchParams;
  const rawTaskId = resolved?.taskId;
  // A GET form sends a single string; guard defensively against an
  // array (duplicate query keys) by taking the first entry.
  const candidate = Array.isArray(rawTaskId) ? rawTaskId[0] : rawTaskId;
  const taskId = typeof candidate === "string" ? candidate.trim() : "";

  if (taskId) {
    redirect(`/tasks/${encodeURIComponent(taskId)}`);
  }

  return (
    <article
      aria-labelledby="tasks-index-heading"
      style={containerStyle}
    >
      <h1 id="tasks-index-heading" style={headingStyle}>
        Open a task summary
      </h1>
      <p style={paragraphStyle}>
        Paste a task id on the home page to open a task summary.
      </p>
      <p style={paragraphStyle}>
        This page does not create tasks and does not look anything
        up on its own. It only forwards an existing task id to its
        summary page.
      </p>
      <nav aria-label="Related pages" style={linksRowStyle}>
        <a href="/" style={linkStyle} data-testid="link-home">
          Back to home
        </a>
      </nav>
    </article>
  );
}
