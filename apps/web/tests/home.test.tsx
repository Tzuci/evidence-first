import * as React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import HomePage from "../app/page";

/**
 * Tests for the product entrypoint home page (UI-HOME-B).
 *
 * The home page is a static server component whose only interactive
 * element is a plain HTML GET form. The tests focus on:
 *   - the stronger hero (product name + subtitle);
 *   - the "Open existing task" form (input name, action, method);
 *   - the "What you can do now" and "Not available in the browser
 *     yet" sections;
 *   - the seven workflow steps;
 *   - the Task summary vs Technical report distinction and both URL
 *     patterns;
 *   - direct API health links and the legacy-only framing of
 *     /diagnostic;
 *   - the absence of misleading or overclaiming wording,
 *     case-insensitive.
 *
 * The tests verify CONCEPTS and required strings, not every
 * sentence, so future copy tweaks stay cheap — except where the
 * wording is forbidden.
 */

/**
 * Phrases that must NEVER appear in the home page DOM (case-
 * insensitive). Mirrors PHASE UI-HOME-B §6.
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

describe("HomePage (UI-HOME-B)", () => {
  it("renders the stronger hero with product name and subtitle", () => {
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

  it("renders an 'Open existing task' form", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("open existing task");
  });

  it("the task-id form has the right input name, action and method", () => {
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

  it("includes a helper line about where to get a task id", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("need a task id");
  });

  it("explains both the Task summary and Technical report pages", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("Task summary");
    expect(body).toContain("Technical report");
  });

  it("shows both URL patterns", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("/tasks/<taskId>");
    expect(body).toContain("/tasks/<taskId>/report");
  });

  it("includes the 'What you can do now' section", () => {
    render(<HomePage />);
    expect(
      screen.getByRole("heading", { name: /What you can do now/i })
    ).toBeInTheDocument();
  });

  it("includes the 'Not available in the browser yet' section", () => {
    render(<HomePage />);
    const body = document.body.textContent ?? "";
    expect(body).toContain("Not available in the browser yet");
  });

  it("lists the browser workflows that are not available yet", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("creating a new task from the browser");
    expect(body).toContain(
      "uploading or selecting documents from the browser"
    );
    expect(body).toContain("browsing task history from the browser");
    expect(body).toContain(
      "editing or publishing answers from the browser"
    );
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
    // /diagnostic may still be linked, but only as a legacy page
    // carrying a warning.
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

  it("describes the technical report as audit/debugging oriented", () => {
    render(<HomePage />);
    const body = (document.body.textContent ?? "").toLowerCase();
    const hasAuditFraming =
      body.includes("audit") || body.includes("debugging");
    expect(hasAuditFraming).toBe(true);
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
