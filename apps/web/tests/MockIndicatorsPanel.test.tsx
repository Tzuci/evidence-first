import * as React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import MockIndicatorsPanel from "../components/MockIndicatorsPanel";
import {
  allMockIndicators,
  noMockIndicators,
} from "./fixtures/reportFixtures";

describe("MockIndicatorsPanel", () => {
  it("renders the four indicator rows with the expected labels", () => {
    render(<MockIndicatorsPanel mockIndicators={allMockIndicators()} />);
    const panel = screen.getByTestId("mock-indicators-panel");
    expect(panel).toHaveTextContent("Source Quality");
    expect(panel).toHaveTextContent("Claim Entailment");
    expect(panel).toHaveTextContent("Compiler");
    expect(panel).toHaveTextContent("CVE-lite");
  });

  it("renders the mock banner when at least one flag is true", () => {
    render(<MockIndicatorsPanel mockIndicators={allMockIndicators()} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(
      "This task ran on mock evaluator(s)."
    );
  });

  it("does NOT render the mock banner when every flag is false", () => {
    render(<MockIndicatorsPanel mockIndicators={noMockIndicators()} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders each indicator with the appropriate aria-label", () => {
    render(<MockIndicatorsPanel mockIndicators={allMockIndicators()} />);
    expect(
      screen.getByLabelText("Source Quality: mock")
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Claim Entailment: mock")
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Compiler: mock")).toBeInTheDocument();
    expect(screen.getByLabelText("CVE-lite: mock")).toBeInTheDocument();
  });

  it("uses 'not detected' for false flags", () => {
    render(
      <MockIndicatorsPanel
        mockIndicators={{
          uses_mock_source_quality: false,
          uses_mock_claim_entailment: true,
          uses_mock_compiler: false,
          uses_mock_cve_lite: true,
          notes: [],
        }}
      />
    );
    expect(
      screen.getByLabelText("Source Quality: not detected")
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Compiler: not detected")
    ).toBeInTheDocument();
  });

  it("renders every note from the notes array", () => {
    render(
      <MockIndicatorsPanel
        mockIndicators={{
          uses_mock_source_quality: true,
          uses_mock_claim_entailment: true,
          uses_mock_compiler: true,
          uses_mock_cve_lite: true,
          notes: ["First note line.", "Second note line."],
        }}
      />
    );
    expect(screen.getByText("First note line.")).toBeInTheDocument();
    expect(screen.getByText("Second note line.")).toBeInTheDocument();
  });

  it("never renders 'truth score' or 'verified true'", () => {
    render(<MockIndicatorsPanel mockIndicators={allMockIndicators()} />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).not.toContain("truth score");
    expect(body).not.toContain("verified true");
  });
});
