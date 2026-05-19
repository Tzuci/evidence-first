import * as React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import ReportStatusBadge from "../components/ReportStatusBadge";

describe("ReportStatusBadge", () => {
  it("renders 'Published' for status='published'", () => {
    render(<ReportStatusBadge status="published" />);
    const badge = screen.getByTestId("report-status-badge");
    expect(badge).toHaveTextContent("Published");
  });

  it("renders 'Held' for status='publication_held'", () => {
    render(<ReportStatusBadge status="publication_held" />);
    const badge = screen.getByTestId("report-status-badge");
    expect(badge).toHaveTextContent("Held");
  });

  it("renders 'Not ready' for status='not_ready'", () => {
    render(<ReportStatusBadge status="not_ready" />);
    expect(
      screen.getByTestId("report-status-badge")
    ).toHaveTextContent("Not ready");
  });

  it("exposes an aria-label that mirrors the visible status", () => {
    render(<ReportStatusBadge status="published" />);
    expect(
      screen.getByLabelText("Publication status: Published")
    ).toBeInTheDocument();
  });

  it("never renders misleading phrases like 'verified true'", () => {
    for (const status of [
      "published",
      "publication_held",
      "withdrawn",
      "superseded",
      "not_ready",
      "failed",
      "unknown",
    ] as const) {
      const { unmount } = render(
        <ReportStatusBadge status={status} />
      );
      const text = document.body.textContent ?? "";
      expect(text.toLowerCase()).not.toContain("verified true");
      expect(text.toLowerCase()).not.toContain("ai verified");
      expect(text.toLowerCase()).not.toContain("truth score");
      unmount();
    }
  });
});
