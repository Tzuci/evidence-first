import * as React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import HomePage from "../app/page";

/**
 * Tests for the product-oriented home page (UI-HOME-A).
 *
 * The home page is a static server component. The tests therefore
 * focus on:
 *   - the presence of the key product concepts (master prompt,
 *     available sources / evidence, technical report);
 *   - the explicit "not available from the UI yet" wording around
 *     task creation, document upload, history browsing and
 *     publishing;
 *   - the preservation of the link to '/diagnostic';
 *   - the absence of misleading wording (truth scores, "verified
 *     true", "hallucination-free", etc.), case-insensitive.
 *
 * The tests do NOT pin every sentence in the page — that would make
 * future copy tweaks unnecessarily painful. They verify CONCEPTS,
 * not exact wording, except where the wording is forbidden.
 */

/**
 * Phrases that must NEVER appear in the home page DOM (case-
 * insensitive). The list mirrors PHASE_UI-HOME-A §6.
 *
 * Note: a few entries are deliberately phrased as the full claim
 * ("source quality proves claim") to avoid false positives on
 * legitimate substrings such as "source quality".
 */
const BANNED_PHRASES = [
  "truth score",
  "verified true",
  "verified answer",
  "ai verified",
  "factually true",
  "hallucination-free",
  "hallucination eliminated",
  "guaranteed truth",
  "zero hallucinations",
  "without hallucinations",
  "source quality proves claim",
  "cve-lite proves support",
  "entailed means true",
];

afterEach(() => {
  cleanup();
});

describe("HomePage (UI-HOME-A)", () => {
  it("renders the main product heading", () => {
    render(<HomePage />);
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: /Evidence-First MVP-0/i,
      })
    ).toBeInTheDocument();
  });

  it("mentions the user master prompt concept", () => {
    render(<HomePage />);
    expect(
      (document.body.textContent ?? "").toLowerCase()
    ).toContain("master prompt");
  });

  it("mentions available sources / evidence as the grounding of the answer", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    // At least one of: "available sources" or "evidence spans" or
    // "based on available evidence" must be present.
    const hasSourcesOrEvidence =
      body.includes("available sources") ||
      body.includes("evidence spans") ||
      body.includes("based on available evidence");
    expect(hasSourcesOrEvidence).toBe(true);
  });

  it("clearly states that creating a new request from the browser is not available yet", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("not available from the ui yet");
    expect(body).toContain("creating a new request from the browser");
  });

  it("clearly states that document upload from the browser is not available yet", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain(
      "uploading or selecting documents from the browser"
    );
  });

  it("describes the technical report as a derived, audit-oriented view", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("technical report");
    // The page must frame the report as audit/debugging, not as the
    // main product surface.
    const hasAuditFraming =
      body.includes("audit") || body.includes("debugging");
    expect(hasAuditFraming).toBe(true);
  });

  it("shows the report URL pattern", () => {
    render(<HomePage />);
    // The URL pattern appears in a <code> block.
    expect(
      screen.getByLabelText("Report URL pattern")
    ).toHaveTextContent("/tasks/<taskId>/report");
  });

  it("links to /diagnostic", () => {
    render(<HomePage />);
    const diagnosticLinks = screen
      .getAllByRole("link")
      .filter((a) => (a as HTMLAnchorElement).getAttribute("href") === "/diagnostic");
    expect(diagnosticLinks.length).toBeGreaterThan(0);
  });

  it("does not contain any banned wording (case-insensitive)", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    for (const phrase of BANNED_PHRASES) {
      expect(body.includes(phrase)).toBe(false);
    }
  });

  it("mentions that publication can be held when support is insufficient", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    // The exact wording does not matter; the concept does.
    const hasHeldConcept =
      body.includes("publication can be held") ||
      body.includes("hold publication");
    expect(hasHeldConcept).toBe(true);
  });

  it("declares the MockProvider / no-external-AI MVP-0 mode", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("mockprovider");
    expect(body).toContain("no external ai calls");
  });
});
