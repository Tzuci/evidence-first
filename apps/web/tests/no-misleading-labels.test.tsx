import * as React from "react";
import { describe, it, expect } from "vitest";
import { render, cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import AxisSummaryCards from "../components/AxisSummaryCards";
import GatePanel from "../components/GatePanel";
import LimitationsPanel from "../components/LimitationsPanel";
import MockIndicatorsPanel from "../components/MockIndicatorsPanel";
import PublicationPanel from "../components/PublicationPanel";
import RawJsonCollapsible from "../components/RawJsonCollapsible";
import ReportStatusBadge from "../components/ReportStatusBadge";

import {
  mockNotReadyReport,
  mockPublicationHeldReport,
  mockPublishedWarningReport,
} from "./fixtures/reportFixtures";

/**
 * Cross-component semantic guard: rendering ANY of the UI-REPORT-A
 * components against ANY of the standard fixtures MUST NOT produce
 * text containing any of the FORBIDDEN_PHRASES.
 *
 * The list mirrors the block prompt §9 (semantic safety). It is a
 * deliberately strict lower bound: a future component must not
 * introduce these phrases either, even by accident in copy.
 *
 * The test is case-insensitive: `Truth Score` is just as bad as
 * `truth score`.
 */
const FORBIDDEN_PHRASES = [
  "truth score",
  "verified true",
  "hallucination eliminated",
  "ai verified",
  "factually true",
  "entailed = true",
  "source quality proves claim",
  "cve-lite proves claim support",
  "real nli",
  "contradiction detector",
  "citation-to-claim validator",
];

function renderAllComponents(): void {
  const reports = [
    mockPublishedWarningReport(),
    mockNotReadyReport(),
    mockPublicationHeldReport(),
  ];
  for (const r of reports) {
    render(<ReportStatusBadge status={r.publication.status} />);
    render(<PublicationPanel publication={r.publication} />);
    render(<GatePanel gate={r.gate} />);
    render(
      <AxisSummaryCards
        axisSummary={r.axis_summary}
        mockIndicators={r.mock_indicators}
      />
    );
    render(<MockIndicatorsPanel mockIndicators={r.mock_indicators} />);
    render(<LimitationsPanel limitations={r.limitations} />);
    render(<RawJsonCollapsible data={r} />);
  }
}

describe("no misleading labels", () => {
  it("the rendered DOM never contains any of the forbidden phrases", () => {
    renderAllComponents();
    const body = (document.body.textContent ?? "").toLowerCase();

    for (const phrase of FORBIDDEN_PHRASES) {
      expect(body.includes(phrase)).toBe(false);
    }

    cleanup();
  });

  it("the rendered DOM contains the mandatory anti-hallucination disclaimers", () => {
    renderAllComponents();
    const body = document.body.textContent ?? "";

    // From AxisSummaryCards (always present when any report is rendered).
    expect(body).toContain("Entailed does not mean true.");
    expect(body).toContain("Source quality does not prove the claim.");
    expect(body).toContain("Quote/hash check, not semantic support.");

    // From PublicationPanel.
    expect(body).toContain(
      "Publication status is ledger-level, not truth-level."
    );

    // From GatePanel.
    expect(body).toContain(
      "Decision read from persisted Final Gate report; not recomputed by UI."
    );

    cleanup();
  });
});
