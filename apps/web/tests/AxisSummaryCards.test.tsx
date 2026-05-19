import * as React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import AxisSummaryCards from "../components/AxisSummaryCards";
import {
  allMockIndicators,
  mockPublishedWarningReport,
  noMockIndicators,
  zeroAxisSummary,
} from "./fixtures/reportFixtures";

describe("AxisSummaryCards", () => {
  it("renders the four axis cards as articles", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    expect(screen.getByLabelText("CVE-lite axis")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Source Quality axis")
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Claim Entailment axis")
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Final Gate (derived)")
    ).toBeInTheDocument();
  });

  it("renders the CVE-lite counters with the expected values", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    const cve = screen.getByLabelText("CVE-lite axis");
    expect(cve).toHaveTextContent("verified_claims_count");
    expect(cve).toHaveTextContent("unverified_claims_count");
    expect(cve).toHaveTextContent("inconclusive_count");
  });

  it("renders the Source Quality counters and the missing_count bucket", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    const sq = screen.getByLabelText("Source Quality axis");
    for (const key of [
      "strong_count",
      "adequate_count",
      "weak_count",
      "unsuitable_count",
      "unknown_count",
      "missing_count",
    ]) {
      expect(sq).toHaveTextContent(key);
    }
  });

  it("renders the Claim Entailment counters and the missing_count bucket", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    const ce = screen.getByLabelText("Claim Entailment axis");
    for (const key of [
      "entailed_count",
      "partially_supported_count",
      "not_supported_count",
      "contradicted_count",
      "uncertain_count",
      "missing_count",
    ]) {
      expect(ce).toHaveTextContent(key);
    }
  });

  it("renders the Final Gate booleans and counters", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    const fg = screen.getByLabelText("Final Gate (derived)");
    expect(fg).toHaveTextContent("blocking_gap_count");
    expect(fg).toHaveTextContent("warning_gap_count");
    expect(fg).toHaveTextContent("has_blocking_gaps");
    expect(fg).toHaveTextContent("has_warnings");
  });

  it("contains the mandatory per-axis disclaimers", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    const body = document.body.textContent ?? "";
    expect(body).toContain("Entailed does not mean true.");
    expect(body).toContain("Source quality does not prove the claim.");
    expect(body).toContain("Quote/hash check, not semantic support.");
  });

  it("does NOT contain the misleading phrase 'truth score'", () => {
    const report = mockPublishedWarningReport();
    render(
      <AxisSummaryCards
        axisSummary={report.axis_summary}
        mockIndicators={report.mock_indicators}
      />
    );
    expect(
      (document.body.textContent ?? "").toLowerCase()
    ).not.toContain("truth score");
  });

  it("renders a mock chip on the three mock-aware cards and none on Final Gate", () => {
    render(
      <AxisSummaryCards
        axisSummary={zeroAxisSummary()}
        mockIndicators={allMockIndicators()}
      />
    );
    expect(screen.getAllByLabelText("Mock evaluator").length).toBe(3);
  });

  it("renders no mock chip when all flags are false", () => {
    render(
      <AxisSummaryCards
        axisSummary={zeroAxisSummary()}
        mockIndicators={noMockIndicators()}
      />
    );
    expect(screen.queryByLabelText("Mock evaluator")).toBeNull();
  });
});
