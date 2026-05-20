import * as React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import TaskSummaryView from "../components/TaskSummaryView";
import type { AntiHallucinationReport } from "../lib/reportTypes";
import {
  mockNotReadyReport,
  mockPublicationHeldReport,
  mockPublishedWarningReport,
} from "./fixtures/reportFixtures";

/**
 * Tests for the user-facing TaskSummaryView (UI-TASK-FLOW-A).
 *
 * These tests exercise the presentational component directly with
 * synthetic report fixtures. They do NOT exercise the route page
 * (`apps/web/app/tasks/[taskId]/page.tsx`); that page is a thin
 * server wrapper that calls `getAntiHallucinationReport` and renders
 * this component. Component-level tests are sufficient because:
 *
 *   1. The wrapper's error paths reuse the same `ApiError` /
 *      `ApiNetworkError` semantics already covered by
 *      `tests/api-error.test.ts`.
 *   2. The wrapper's happy path is "fetch -> render this component";
 *      the rendering is what the user sees and what banned-wording
 *      guarantees must hold against.
 *
 * Test focus:
 *   - Status-dependent copy (published, held, not_ready, withdrawn,
 *     etc.).
 *   - Conservative fallbacks (missing answer text, missing
 *     objective).
 *   - Safe wording: no banned phrases anywhere in the rendered DOM.
 *   - Link targets: technical report and home.
 *   - Four high-level check cards always present.
 *   - Wording does NOT call the answer "false" when publication is
 *     held.
 */

const TASK_ID = "11112222-3333-4444-5555-666677778888";

/**
 * Phrases forbidden by UI-TASK-FLOW-A §9. Comparison is case
 * insensitive.
 */
const BANNED_PHRASES = [
  "truth score",
  "verified true",
  "verified answer",
  "ai verified",
  "factually true",
  "hallucination eliminated",
  "hallucination-free",
  "guaranteed truth",
  "zero hallucinations",
  "entailed = true",
  "source quality proves claim",
  "cve-lite proves support",
  "real nli",
  "contradiction detector",
  "citation-to-claim validator",
];

/** Helper: clone a fixture so per-test mutations don't leak. */
function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// 1. Published status renders a user-friendly label
// ---------------------------------------------------------------------------
describe("TaskSummaryView — published status", () => {
  it("renders an 'Answer available' user-friendly label", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const badge = screen.getByTestId("user-status-badge");
    expect(badge).toHaveTextContent("Answer available");
  });

  it("exposes the raw backend status as a secondary tag", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("published");
  });

  it("renders the task objective when present", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("task-objective")).toHaveTextContent(
      report.task.objective ?? ""
    );
  });

  it("renders the answer text when summary_text is present", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("answer-text")).toHaveTextContent(
      report.publication.summary_text ?? ""
    );
  });
});

// ---------------------------------------------------------------------------
// 2. Publication held: "Publication held" + safe explanation
// ---------------------------------------------------------------------------
describe("TaskSummaryView — publication_held status", () => {
  it("renders the 'Publication held' label", () => {
    const report = mockPublicationHeldReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("user-status-badge")).toHaveTextContent(
      "Publication held"
    );
  });

  it("renders a safe explanation that support was insufficient", () => {
    const report = mockPublicationHeldReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const explanation = screen.getByTestId("held-explanation");
    expect(explanation).toHaveTextContent(
      /did not find sufficient support for publication/i
    );
  });

  it("surfaces the gate reason_code verbatim when present", () => {
    const report = mockPublicationHeldReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("entailment_block");
  });

  // ---------------------------------------------------------------------
  // 3. Publication held does NOT call the answer false
  // ---------------------------------------------------------------------
  it("does NOT say the answer is false anywhere on the page", () => {
    const report = mockPublicationHeldReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = (document.body.textContent ?? "").toLowerCase();

    // Forbidden phrasings that would imply the system PROVED the
    // claim wrong in the world.
    expect(body).not.toContain("the answer is false");
    expect(body).not.toContain("the claim is false");
    expect(body).not.toContain("answer is wrong");
    expect(body).not.toContain("claim is wrong");
    expect(body).not.toContain("proved false");
    expect(body).not.toContain("proven false");
    expect(body).not.toContain("proved untrue");
    expect(body).not.toContain("proven untrue");
  });
});

// ---------------------------------------------------------------------------
// 4. Missing answer text → conservative copy
// ---------------------------------------------------------------------------
describe("TaskSummaryView — missing answer text", () => {
  it("renders the conservative fallback when summary_text is null", () => {
    const report = clone(mockPublishedWarningReport());
    report.publication.summary_text = null;
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByTestId("answer-text-missing")
    ).toHaveTextContent(
      "Answer text is not exposed by this MVP-0 summary view yet."
    );
    expect(screen.queryByTestId("answer-text")).toBeNull();
  });

  it("renders the same conservative copy when summary_text is empty", () => {
    const report = clone(mockPublishedWarningReport());
    report.publication.summary_text = "";
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByTestId("answer-text-missing")
    ).toHaveTextContent(
      "Answer text is not exposed by this MVP-0 summary view yet."
    );
  });

  it("renders the conservative fallback when summary_text is whitespace only", () => {
    const report = clone(mockPublishedWarningReport());
    report.publication.summary_text = "   \n  ";
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByTestId("answer-text-missing")
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 5. & 6. Link targets: technical report + home
// ---------------------------------------------------------------------------
describe("TaskSummaryView — link targets", () => {
  it("technical report link points to /tasks/<taskId>/report", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const link = screen.getByTestId("link-technical-report");
    expect(link.getAttribute("href")).toBe(`/tasks/${TASK_ID}/report`);
  });

  it("home link points to /", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const link = screen.getByTestId("link-home");
    expect(link.getAttribute("href")).toBe("/");
  });

  it("technical report link uses the exact taskId passed in", () => {
    const otherId = "deadbeef-0000-0000-0000-000000000000";
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={otherId} report={report} />);
    expect(
      screen.getByTestId("link-technical-report").getAttribute("href")
    ).toBe(`/tasks/${otherId}/report`);
  });
});

// ---------------------------------------------------------------------------
// 7. Four high-level checks render
// ---------------------------------------------------------------------------
describe("TaskSummaryView — high-level checks", () => {
  it("renders all four high-level check cards", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("check-card-quote-hash")).toBeInTheDocument();
    expect(
      screen.getByTestId("check-card-source-quality")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-card-claim-evidence")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-card-publication-gate")
    ).toBeInTheDocument();
  });

  it("each card displays its safe descriptive title", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("Quote/hash check");
    expect(body).toContain("Source quality signal");
    expect(body).toContain("Claim-evidence relation");
    expect(body).toContain("Publication gate");
  });

  it("renders the four cards regardless of publication status", () => {
    const report = mockNotReadyReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("check-card-quote-hash")).toBeInTheDocument();
    expect(
      screen.getByTestId("check-card-source-quality")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-card-claim-evidence")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-card-publication-gate")
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 7b. High-level checks render derived axis_summary counts
// ---------------------------------------------------------------------------
describe("TaskSummaryView — high-level check counts (derived)", () => {
  it("each check card carries a derived counts block", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByTestId("check-counts-quote-hash")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-counts-source-quality")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-counts-claim-evidence")
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("check-counts-publication-gate")
    ).toBeInTheDocument();
  });

  it("published fixture shows the verified count from cve_lite", () => {
    const report = mockPublishedWarningReport();
    // fixture sets cve_lite.verified_claims_count = 1
    expect(report.axis_summary.cve_lite.verified_claims_count).toBe(1);
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const counts = screen.getByTestId("check-counts-quote-hash");
    expect(counts).toHaveTextContent("verified");
    expect(counts).toHaveTextContent("1");
  });

  it("published fixture shows the warning_gap_count from final_gate", () => {
    const report = mockPublishedWarningReport();
    // fixture sets final_gate.warning_gap_count = 1
    expect(report.axis_summary.final_gate.warning_gap_count).toBe(1);
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const counts = screen.getByTestId("check-counts-publication-gate");
    expect(counts).toHaveTextContent("warning_gap_count");
    expect(counts).toHaveTextContent("1");
  });

  it("published fixture shows the entailed count from claim_entailment", () => {
    const report = mockPublishedWarningReport();
    // fixture sets claim_entailment.entailed_count = 1
    expect(report.axis_summary.claim_entailment.entailed_count).toBe(1);
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const counts = screen.getByTestId("check-counts-claim-evidence");
    expect(counts).toHaveTextContent("entailed");
    expect(counts).toHaveTextContent("1");
  });

  it("held fixture shows the blocking_gap_count from final_gate", () => {
    const report = mockPublicationHeldReport();
    // fixture sets final_gate.blocking_gap_count = 1
    expect(report.axis_summary.final_gate.blocking_gap_count).toBe(1);
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const counts = screen.getByTestId("check-counts-publication-gate");
    expect(counts).toHaveTextContent("blocking_gap_count");
    expect(counts).toHaveTextContent("1");
  });

  it("held fixture shows has_blocking_gaps as true on the gate card", () => {
    const report = mockPublicationHeldReport();
    expect(report.axis_summary.final_gate.has_blocking_gaps).toBe(true);
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const counts = screen.getByTestId("check-counts-publication-gate");
    expect(counts).toHaveTextContent("has_blocking_gaps");
    expect(counts).toHaveTextContent("true");
  });

  it("held fixture shows the contradicted count from claim_entailment", () => {
    const report = mockPublicationHeldReport();
    // fixture sets claim_entailment.contradicted_count = 1
    expect(
      report.axis_summary.claim_entailment.contradicted_count
    ).toBe(1);
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const counts = screen.getByTestId("check-counts-claim-evidence");
    expect(counts).toHaveTextContent("contradicted");
    expect(counts).toHaveTextContent("1");
  });

  it("not_ready fixture renders safe zero counts on every card", () => {
    const report = mockNotReadyReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);

    // Every counts block is present and shows zeros / false (the
    // zeroAxisSummary fixture). We assert the labels render and the
    // gate booleans degrade to "false".
    const quoteHash = screen.getByTestId("check-counts-quote-hash");
    expect(quoteHash).toHaveTextContent("verified");
    expect(quoteHash).toHaveTextContent("0");

    const gate = screen.getByTestId("check-counts-publication-gate");
    expect(gate).toHaveTextContent("blocking_gap_count");
    expect(gate).toHaveTextContent("has_blocking_gaps");
    expect(gate).toHaveTextContent("false");
  });

  it("renders the source quality missing_count bucket", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByTestId("check-counts-source-quality")
    ).toHaveTextContent("missing");
  });

  it("does not present a single aggregate cross-axis score", () => {
    // The counts are per-axis; there must be no combined "score".
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).not.toContain("overall score");
    expect(body).not.toContain("total score");
    expect(body).not.toContain("trust score");
  });
});

// ---------------------------------------------------------------------------
// 8. Banned wording absent
// ---------------------------------------------------------------------------
describe("TaskSummaryView — banned wording", () => {
  it("never contains banned phrases on the published path", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = (document.body.textContent ?? "").toLowerCase();
    for (const phrase of BANNED_PHRASES) {
      expect(body.includes(phrase)).toBe(false);
    }
  });

  it("never contains banned phrases on the publication_held path", () => {
    const report = mockPublicationHeldReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = (document.body.textContent ?? "").toLowerCase();
    for (const phrase of BANNED_PHRASES) {
      expect(body.includes(phrase)).toBe(false);
    }
  });

  it("never contains banned phrases on the not_ready path", () => {
    const report = mockNotReadyReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = (document.body.textContent ?? "").toLowerCase();
    for (const phrase of BANNED_PHRASES) {
      expect(body.includes(phrase)).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// Extra coverage: not_ready and other safe status renderings
// ---------------------------------------------------------------------------
describe("TaskSummaryView — other statuses", () => {
  it("renders 'Not ready yet' label for not_ready", () => {
    const report = mockNotReadyReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("user-status-badge")).toHaveTextContent(
      "Not ready yet"
    );
  });

  it("renders the conservative copy when objective is missing", () => {
    const report = clone(mockNotReadyReport());
    report.task.objective = null;
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByTestId("task-objective-missing")
    ).toHaveTextContent(
      "The original request is not exposed by this summary payload."
    );
  });

  it("renders 'Unknown status' for the 'unknown' publication status", () => {
    const report = clone(mockNotReadyReport());
    // Cast through unknown to set the status without weakening the
    // type elsewhere.
    (report as unknown as {
      publication: { status: string };
    }).publication.status = "unknown";
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(screen.getByTestId("user-status-badge")).toHaveTextContent(
      "Unknown status"
    );
  });

  it("renders 'Unknown status' label for an UNRECOGNIZED status (not the raw token)", () => {
    const report = clone(mockNotReadyReport());
    // A status value outside the documented codomain. The primary
    // user-facing label must be the sober "Unknown status", NOT the
    // raw backend token.
    (report as unknown as {
      publication: { status: string };
    }).publication.status = "frobnicated";
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const badge = screen.getByTestId("user-status-badge");
    // Primary label is sober.
    expect(badge).toHaveTextContent("Unknown status");
    // The raw token is still available as the small secondary tag,
    // so auditability is preserved, but it is NOT the badge label.
    expect(badge.textContent).not.toBe("frobnicated");
    expect(document.body.textContent ?? "").toContain("frobnicated");
  });
});

// ---------------------------------------------------------------------------
// Smoke: structural invariants
// ---------------------------------------------------------------------------
describe("TaskSummaryView — structural invariants", () => {
  it("always exposes the page heading", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    expect(
      screen.getByRole("heading", { level: 1, name: /Task summary/i })
    ).toBeInTheDocument();
  });

  it("frames the page as a derived read-only view", () => {
    const report = mockPublishedWarningReport();
    render(<TaskSummaryView taskId={TASK_ID} report={report} />);
    const body = document.body.textContent ?? "";
    // The page must label itself as derived/read-only somewhere.
    expect(body.toLowerCase()).toContain("derived");
  });

  // Type-narrowing helper: ensures the fixture matches the
  // AntiHallucinationReport contract at compile time.
  it("accepts an AntiHallucinationReport without surprises", () => {
    const report: AntiHallucinationReport = mockPublishedWarningReport();
    expect(() =>
      render(<TaskSummaryView taskId={TASK_ID} report={report} />)
    ).not.toThrow();
  });
});
