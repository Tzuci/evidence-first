import { describe, it, expect } from "vitest";

import {
  formatDateTime,
  gateDecisionLabel,
  hasAnyMockIndicator,
  isTerminalPublicationStatus,
  publicationStatusLabel,
  shortId,
} from "../lib/reportFormatting";
import {
  allMockIndicators,
  noMockIndicators,
} from "./fixtures/reportFixtures";

describe("shortId", () => {
  it("returns the em dash placeholder for null and empty input", () => {
    expect(shortId(null)).toBe("—");
    expect(shortId(undefined)).toBe("—");
    expect(shortId("")).toBe("—");
  });

  it("returns the input unchanged when shorter than chars", () => {
    expect(shortId("abc", 8)).toBe("abc");
  });

  it("truncates to the requested number of chars with an ellipsis", () => {
    const uuid = "11112222-3333-4444-5555-666677778888";
    expect(shortId(uuid, 8)).toBe("11112222…");
  });

  it("returns the em dash placeholder when chars is non-positive", () => {
    expect(shortId("anything", 0)).toBe("—");
    expect(shortId("anything", -2)).toBe("—");
  });
});

describe("formatDateTime", () => {
  it("returns the em dash placeholder for null and empty input", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("")).toBe("—");
  });

  it("formats a valid ISO datetime in UTC", () => {
    expect(formatDateTime("2026-05-19T09:50:00Z")).toBe(
      "2026-05-19 09:50:00 UTC"
    );
  });

  it("falls back to the raw string when the value is not parseable", () => {
    expect(formatDateTime("not-a-date")).toBe("not-a-date");
  });
});

describe("publicationStatusLabel", () => {
  it("maps publication_held to 'Held'", () => {
    expect(publicationStatusLabel("publication_held")).toBe("Held");
  });

  it("maps not_ready to 'Not ready'", () => {
    expect(publicationStatusLabel("not_ready")).toBe("Not ready");
  });

  it("maps published, withdrawn, superseded, failed, unknown correctly", () => {
    expect(publicationStatusLabel("published")).toBe("Published");
    expect(publicationStatusLabel("withdrawn")).toBe("Withdrawn");
    expect(publicationStatusLabel("superseded")).toBe("Superseded");
    expect(publicationStatusLabel("failed")).toBe("Failed");
    expect(publicationStatusLabel("unknown")).toBe("Unknown");
  });

  it("echoes an unknown status verbatim", () => {
    expect(publicationStatusLabel("frobnicated")).toBe("frobnicated");
  });
});

describe("gateDecisionLabel", () => {
  it("maps approved/rejected to their capitalized forms", () => {
    expect(gateDecisionLabel("approved")).toBe("Approved");
    expect(gateDecisionLabel("rejected")).toBe("Rejected");
  });

  it("maps null to 'No gate report'", () => {
    expect(gateDecisionLabel(null)).toBe("No gate report");
  });
});

describe("isTerminalPublicationStatus", () => {
  it("treats published, withdrawn, superseded, publication_held, failed as terminal", () => {
    expect(isTerminalPublicationStatus("published")).toBe(true);
    expect(isTerminalPublicationStatus("withdrawn")).toBe(true);
    expect(isTerminalPublicationStatus("superseded")).toBe(true);
    expect(isTerminalPublicationStatus("publication_held")).toBe(true);
    expect(isTerminalPublicationStatus("failed")).toBe(true);
  });

  it("treats not_ready and unknown as non-terminal", () => {
    expect(isTerminalPublicationStatus("not_ready")).toBe(false);
    expect(isTerminalPublicationStatus("unknown")).toBe(false);
  });
});

describe("hasAnyMockIndicator", () => {
  it("returns true when at least one flag is true", () => {
    expect(hasAnyMockIndicator(allMockIndicators())).toBe(true);
  });

  it("returns false when every flag is false", () => {
    expect(hasAnyMockIndicator(noMockIndicators())).toBe(false);
  });

  it("returns false on null/undefined", () => {
    expect(hasAnyMockIndicator(null)).toBe(false);
    expect(hasAnyMockIndicator(undefined)).toBe(false);
  });

  it("returns true when only uses_mock_compiler is true", () => {
    expect(
      hasAnyMockIndicator({
        uses_mock_source_quality: false,
        uses_mock_claim_entailment: false,
        uses_mock_compiler: true,
        uses_mock_cve_lite: false,
        notes: [],
      })
    ).toBe(true);
  });
});
