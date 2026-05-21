import * as React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

/**
 * Tests for the `/tasks` index route (UI-HOME-B).
 *
 * The route is a server component. `next/navigation`'s `redirect`
 * throws internally in a real Next.js runtime; here we mock it with
 * a plain spy so the test stays simple and does not need an
 * app-router harness. We assert:
 *   - with a `taskId`, the route calls `redirect` with the encoded
 *     `/tasks/<taskId>` target;
 *   - without a `taskId`, the route renders the guidance page and a
 *     "Back to home" link, and does NOT redirect.
 *
 * `searchParams` is passed as a resolved Promise to mirror the
 * Next.js 15 contract.
 */

const redirectMock = vi.fn();

vi.mock("next/navigation", () => ({
  redirect: (url: string) => {
    redirectMock(url);
  },
}));

// Imported after the mock is registered.
import TasksIndexPage from "../app/tasks/page";

afterEach(() => {
  cleanup();
  redirectMock.mockReset();
});

describe("TasksIndexPage (UI-HOME-B)", () => {
  it("redirects to /tasks/<taskId> when a taskId is provided", async () => {
    const taskId = "11112222-3333-4444-5555-666677778888";
    await TasksIndexPage({
      searchParams: Promise.resolve({ taskId }),
    });
    expect(redirectMock).toHaveBeenCalledTimes(1);
    expect(redirectMock).toHaveBeenCalledWith(`/tasks/${taskId}`);
  });

  it("trims and encodes the taskId before redirecting", async () => {
    await TasksIndexPage({
      searchParams: Promise.resolve({ taskId: "  with space  " }),
    });
    expect(redirectMock).toHaveBeenCalledWith(
      "/tasks/with%20space"
    );
  });

  it("renders the guidance page when taskId is missing", async () => {
    const element = await TasksIndexPage({
      searchParams: Promise.resolve({}),
    });
    render(element);
    expect(redirectMock).not.toHaveBeenCalled();
    expect(
      screen.getByText(
        /Paste a task id on the home page to open a task summary\./i
      )
    ).toBeInTheDocument();
  });

  it("renders a 'Back to home' link on the guidance page", async () => {
    const element = await TasksIndexPage({
      searchParams: Promise.resolve({}),
    });
    render(element);
    const link = screen.getByTestId("link-home");
    expect(link.getAttribute("href")).toBe("/");
  });

  it("renders the guidance page when taskId is empty/whitespace", async () => {
    const element = await TasksIndexPage({
      searchParams: Promise.resolve({ taskId: "   " }),
    });
    render(element);
    expect(redirectMock).not.toHaveBeenCalled();
    expect(
      screen.getByRole("heading", { name: /Open a task summary/i })
    ).toBeInTheDocument();
  });
});
