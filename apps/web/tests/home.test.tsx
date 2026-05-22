import * as React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import HomePage from "../app/page";

/**
 * Tests for the product entrypoint home page (updated by
 * UI-CREATE-FLOW-A).
 *
 * The home page is a static server component. After UI-CREATE-FLOW-A
 * it has TWO actions:
 *   - PRIMARY: a link "New evidence-based request" → /requests/new,
 *     where a real task can be created from the browser;
 *   - SECONDARY: the existing "Open existing task" plain HTML GET
 *     form (action="/tasks", method="get", input name="taskId").
 *
 * The tests focus on:
 *   - the hero (product name + subtitle);
 *   - the primary CTA link to /requests/new;
 *   - the still-present "Open existing task" GET form;
 *   - the Task summary vs Technical report distinction;
 *   - the MVP-0 limitations section;
 *   - the direct API health links and the legacy-only framing of
 *     /diagnostic;
 *   - the absence of misleading or overclaiming wording.
 */

/**
 * Phrases that must NEVER appear in the home page DOM (case-
 * insensitive). Mirrors PHASE UI-CREATE-FLOW-A §9.
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
  "entailed = true",
  "source quality proves claim",
  "cve-lite proves support",
  "real nli",
  "contradiction detector",
  "citation-to-claim validator",
];

afterEach(() => {
  cleanup();
});

describe("HomePage (UI-CREATE-FLOW-A)", () => {
  it("renders the hero with product name and subtitle", () => {
    render(<HomePage />);
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: /Evidence-First MVP-0/i,
      })
    ).toBeInTheDocument();
    const body = document.body.textContent ?? "";
    expect(body).toContain("Controlled answers from available evidence");
  });

  it("renders the primary CTA 'New evidence-based request'", () => {
    render(<HomePage />);
    const cta = screen.getByTestId("primary-cta-new-request");
    expect(cta).toBeInTheDocument();
    expect(cta).toHaveTextContent("New evidence-based request");
  });

  it("the primary CTA links to /requests/new", () => {
    render(<HomePage />);
    const cta = screen.getByTestId("primary-cta-new-request");
    expect(cta.getAttribute("href")).toBe("/requests/new");
  });

  it("still renders an 'Open existing task' section", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("open existing task");
  });

  it("the existing-task form keeps action='/tasks', method='get' and input name='taskId'", () => {
    const { container } = render(<HomePage />);
    const form = container.querySelector("form");
    expect(form).not.toBeNull();
    expect(form?.getAttribute("action")).toBe("/tasks");
    expect((form?.getAttribute("method") ?? "").toLowerCase()).toBe(
      "get"
    );
    const input = container.querySelector('input[name="taskId"]');
    expect(input).not.toBeNull();
  });

  it("includes a submit button to open the task summary", () => {
    render(<HomePage />);
    expect(
      screen.getByRole("button", { name: /open task summary/i })
    ).toBeInTheDocument();
  });

  it("explains both the Task summary and Technical report pages", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("Task summary");
    expect(body).toContain("Technical report");
  });

  it("shows both task URL patterns", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("/tasks/<taskId>");
    expect(body).toContain("/tasks/<taskId>/report");
  });

  it("keeps the technical report as a secondary, audit-oriented surface", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    const hasAuditFraming =
      body.includes("audit") || body.includes("debugging");
    expect(hasAuditFraming).toBe(true);
  });

  it("includes an MVP-0 limitations section", () => {
    render(<HomePage />);
    expect(
      screen.getByRole("heading", { name: /MVP-0 limitations/i })
    ).toBeInTheDocument();
  });

  it("includes all seven workflow steps", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    for (const step of [
      "User request",
      "Available sources",
      "Claims extracted",
      "Evidence spans linked",
      "Checks performed",
      "Publication allowed or held",
      "Summary and report available",
    ]) {
      expect(body).toContain(step);
    }
  });

  it("shows the direct API health links", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("http://localhost:8000/health/live");
    expect(body).toContain("http://localhost:8000/health/ready");
  });

  it("does not promote /diagnostic as the primary health check", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    const diagnosticLinks = screen
      .getAllByRole("link")
      .filter(
        (a) =>
          (a as HTMLAnchorElement).getAttribute("href") ===
          "/diagnostic"
      );
    if (diagnosticLinks.length > 0) {
      expect(body).toContain("legacy");
      expect(body).toContain("404");
    }
  });

  it("mentions that publication can be held when support is insufficient", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("hold publication");
    expect(body).toContain("support is insufficient");
  });

  it("declares the MockProvider / no-external-AI MVP-0 mode", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("mockprovider");
    expect(body).toContain("no external ai calls");
  });

  it("does not contain any banned wording (case-insensitive)", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    for (const phrase of BANNED_PHRASES) {
      expect(body.includes(phrase)).toBe(false);
    }
  });
});
