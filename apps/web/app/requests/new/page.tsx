import * as React from "react";

import NewRequestFlow from "../../../components/NewRequestFlow";

/**
 * Route for the request-creation flow (Phase UI-CREATE-FLOW-A).
 *
 * This is a thin server component shell: it renders the static
 * heading and intro copy, then mounts the interactive client
 * component `NewRequestFlow`, which owns all browser interaction
 * (listing/creating projects, listing/uploading documents, creating
 * the task).
 *
 * Why a server shell + client child rather than a single client
 * page: the intro copy is static and benefits from server rendering;
 * only the form interaction needs the client. This mirrors the
 * project convention of keeping `"use client"` to the smallest
 * surface that genuinely needs it.
 *
 * Semantic guardrails (PHASE UI-CREATE-FLOW-A §9): the intro copy
 * uses only the prescribed safe wording. It frames the user input as
 * a "request", never as "truth verification", and does not promise
 * factual certainty — publication can be held when support is
 * insufficient.
 *
 * Inline styles only, in line with the rest of the project; no
 * Tailwind, no CSS modules, no new dependencies.
 */

const introSectionStyle: React.CSSProperties = {
  padding: "24px 22px",
  border: "1px solid #d6dbe1",
  borderRadius: 8,
  background: "#ffffff",
  marginBottom: 8,
};

const introTitleStyle: React.CSSProperties = {
  fontSize: 26,
  fontWeight: 700,
  margin: 0,
  marginBottom: 6,
  color: "#111",
};

const introCopyStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 14,
  color: "#333",
  lineHeight: 1.6,
  maxWidth: 640,
};

const introNoteStyle: React.CSSProperties = {
  margin: 0,
  marginTop: 10,
  fontSize: 12,
  color: "#666",
};

const linkStyle: React.CSSProperties = {
  color: "#1f3a8a",
  textDecoration: "underline",
  fontSize: 13,
};

export default function NewRequestPage(): React.ReactElement {
  return (
    <article aria-labelledby="new-request-page-heading">
      <header style={introSectionStyle}>
        <h1 id="new-request-page-heading" style={introTitleStyle}>
          New evidence-based request
        </h1>
        <p style={introCopyStyle}>
          Ask a question or describe the answer you need. Evidence-First
          will use the available sources attached to the request and
          may hold publication when support is insufficient.
        </p>
        <p style={introNoteStyle}>
          Follow the steps below: choose a project, attach sources,
          write your request, then create the task. You will be taken
          to the task summary once the task is created.
        </p>
        <p style={{ ...introNoteStyle, marginTop: 12 }}>
          <a href="/" style={linkStyle}>
            Back to home
          </a>
        </p>
      </header>

      <NewRequestFlow />
    </article>
  );
}
