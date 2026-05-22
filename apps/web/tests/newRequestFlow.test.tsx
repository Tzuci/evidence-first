import * as React from "react";
import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
} from "vitest";
import {
  cleanup,
  render,
  screen,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

/**
 * Tests for the `/requests/new` flow component `NewRequestFlow`
 * (Phase UI-CREATE-FLOW-A).
 *
 * `fetch` is mocked so the create-flow API client talks to a stub;
 * `next/navigation` is mocked so `router.push` is observable without
 * an app-router harness (mirrors `tasksRedirectPage.test.tsx`).
 *
 * Coverage:
 *   - the four ordered sections render (Project, Sources, Request,
 *     Create task);
 *   - an existing project can be selected;
 *   - a new project can be created;
 *   - an existing document can be selected;
 *   - an invalid file extension is rejected client-side;
 *   - "Create task" stays disabled until project + document +
 *     objective are present;
 *   - a successful create navigates to `/tasks/<id>`;
 *   - a failed create does NOT navigate, shows the error inline, and
 *     preserves the objective input.
 */

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: pushMock,
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
}));

import NewRequestFlow from "../components/NewRequestFlow";

const originalFetch = globalThis.fetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

interface RouteHandlers {
  listProjects?: () => Response;
  createProject?: () => Response;
  listDocuments?: () => Response;
  uploadDocument?: () => Response;
  createTask?: () => Response;
}

/**
 * Install a fetch mock that dispatches on the proxy path + method.
 */
function installFetch(handlers: RouteHandlers): void {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();

    if (url.endsWith("/api/ef/projects") && method === "GET") {
      return (
        handlers.listProjects?.() ??
        jsonResponse({ items: [], next_cursor: null })
      );
    }
    if (url.endsWith("/api/ef/projects") && method === "POST") {
      return (
        handlers.createProject?.() ??
        jsonResponse({ id: "new-p", name: "x" }, 201)
      );
    }
    if (url.includes("/documents") && method === "GET") {
      return (
        handlers.listDocuments?.() ?? jsonResponse({ items: [] })
      );
    }
    if (url.includes("/documents") && method === "POST") {
      return (
        handlers.uploadDocument?.() ??
        jsonResponse({ id: "up-d", filename: "u.txt" }, 201)
      );
    }
    if (url.endsWith("/api/ef/tasks") && method === "POST") {
      return (
        handlers.createTask?.() ?? jsonResponse({ id: "task-x" }, 201)
      );
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  }) as unknown as typeof fetch;
}

function projectsBody() {
  return {
    items: [
      {
        id: "p1",
        tenant_id: "t1",
        name: "Existing project",
        mode_default: "closed_corpus",
        created_by: "u1",
        created_at: "2026-05-19T09:00:00Z",
      },
    ],
    next_cursor: null,
  };
}

function documentsBody() {
  return {
    items: [
      {
        id: "d1",
        tenant_id: "t1",
        project_id: "p1",
        filename: "source-1.txt",
        content_hash: "h",
        mime_type: "text/plain",
        size_bytes: 20,
        tier: "user_provided",
        language: "en",
        created_by: "u1",
        created_at: "2026-05-19T09:10:00Z",
      },
    ],
  };
}

beforeEach(() => {
  pushMock.mockReset();
});

afterEach(() => {
  cleanup();
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("NewRequestFlow — structure", () => {
  it("renders the four ordered sections", async () => {
    installFetch({});
    render(<NewRequestFlow />);
    await waitFor(() =>
      expect(screen.getByTestId("section-project")).toBeInTheDocument()
    );
    expect(screen.getByTestId("section-sources")).toBeInTheDocument();
    expect(screen.getByTestId("section-objective")).toBeInTheDocument();
    expect(screen.getByTestId("section-create")).toBeInTheDocument();
  });

  it("labels the request field 'Request', never 'truth verification'", async () => {
    installFetch({});
    render(<NewRequestFlow />);
    await waitFor(() =>
      expect(screen.getByTestId("section-objective")).toBeInTheDocument()
    );
    const body = (document.body.textContent ?? "").toLowerCase();
    expect(body).toContain("request");
    expect(body).not.toContain("truth verification");
  });
});

describe("NewRequestFlow — project selection", () => {
  it("lists existing projects and lets the user select one", async () => {
    installFetch({ listProjects: () => jsonResponse(projectsBody()) });
    render(<NewRequestFlow />);

    const select = await screen.findByTestId("project-select");
    fireEvent.change(select, { target: { value: "p1" } });

    await waitFor(() =>
      expect(
        screen.getByTestId("project-selected-note")
      ).toHaveTextContent("Existing project")
    );
  });

  it("creates a new project when the list is empty", async () => {
    installFetch({
      listProjects: () =>
        jsonResponse({ items: [], next_cursor: null }),
      createProject: () =>
        jsonResponse(
          { id: "p-new", name: "Fresh project" },
          201
        ),
    });
    render(<NewRequestFlow />);

    // Empty list → create form shown first.
    const nameInput = await screen.findByTestId("new-project-name");
    fireEvent.change(nameInput, {
      target: { value: "Fresh project" },
    });
    fireEvent.click(screen.getByTestId("create-project-button"));

    await waitFor(() =>
      expect(
        screen.getByTestId("project-selected-note")
      ).toHaveTextContent("Fresh project")
    );
  });

  it("shows an inline error when project creation conflicts", async () => {
    installFetch({
      listProjects: () =>
        jsonResponse({ items: [], next_cursor: null }),
      createProject: () =>
        jsonResponse(
          {
            error: {
              code: "RESOURCE_CONFLICT",
              message: "already exists",
            },
          },
          409
        ),
    });
    render(<NewRequestFlow />);

    const nameInput = await screen.findByTestId("new-project-name");
    fireEvent.change(nameInput, { target: { value: "Dup" } });
    fireEvent.click(screen.getByTestId("create-project-button"));

    await waitFor(() =>
      expect(
        screen.getByTestId("project-create-error")
      ).toBeInTheDocument()
    );
  });
});

describe("NewRequestFlow — sources", () => {
  it("lists project documents and lets the user select one", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse(documentsBody()),
    });
    render(<NewRequestFlow />);

    const select = await screen.findByTestId("project-select");
    fireEvent.change(select, { target: { value: "p1" } });

    const checkbox = await screen.findByTestId(
      "document-checkbox-d1"
    );
    fireEvent.click(checkbox);

    await waitFor(() =>
      expect(
        screen.getByTestId("sources-selected-count")
      ).toHaveTextContent("1 source(s) selected")
    );
  });

  it("rejects an invalid file extension client-side", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse({ items: [] }),
    });
    render(<NewRequestFlow />);

    const select = await screen.findByTestId("project-select");
    fireEvent.change(select, { target: { value: "p1" } });

    const fileInput = await screen.findByTestId("source-file-input");
    const badFile = new File(["data"], "report.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(fileInput, { target: { files: [badFile] } });

    await waitFor(() =>
      expect(
        screen.getByTestId("document-upload-error")
      ).toBeInTheDocument()
    );
  });
});

describe("NewRequestFlow — create task gating", () => {
  it("keeps the create button disabled until project + document + objective", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse(documentsBody()),
    });
    render(<NewRequestFlow />);

    const button = await screen.findByTestId("create-task-button");
    expect(button).toBeDisabled();

    // Select project.
    fireEvent.change(screen.getByTestId("project-select"), {
      target: { value: "p1" },
    });
    expect(button).toBeDisabled();

    // Select a document.
    const checkbox = await screen.findByTestId(
      "document-checkbox-d1"
    );
    fireEvent.click(checkbox);
    expect(button).toBeDisabled();

    // Enter an objective.
    fireEvent.change(screen.getByTestId("objective-input"), {
      target: { value: "Summarize the revenue figures." },
    });

    await waitFor(() => expect(button).not.toBeDisabled());
  });
});

describe("NewRequestFlow — create task outcome", () => {
  async function fillToReady(): Promise<void> {
    const select = await screen.findByTestId("project-select");
    fireEvent.change(select, { target: { value: "p1" } });
    const checkbox = await screen.findByTestId(
      "document-checkbox-d1"
    );
    fireEvent.click(checkbox);
    fireEvent.change(screen.getByTestId("objective-input"), {
      target: { value: "An objective for the task." },
    });
    await waitFor(() =>
      expect(screen.getByTestId("create-task-button")).not.toBeDisabled()
    );
  }

  it("navigates to /tasks/<id> on a successful create", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse(documentsBody()),
      createTask: () =>
        jsonResponse({ id: "real-task-id" }, 201),
    });
    render(<NewRequestFlow />);

    await fillToReady();
    fireEvent.click(screen.getByTestId("create-task-button"));

    await waitFor(() =>
      expect(pushMock).toHaveBeenCalledWith("/tasks/real-task-id")
    );
  });

  it("does NOT navigate and shows an inline error when create fails", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse(documentsBody()),
      createTask: () =>
        jsonResponse(
          {
            error: {
              code: "INTERNAL_ERROR",
              message: "task creation failed",
            },
          },
          500
        ),
    });
    render(<NewRequestFlow />);

    await fillToReady();
    fireEvent.click(screen.getByTestId("create-task-button"));

    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-error")
      ).toBeInTheDocument()
    );
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("preserves the objective input after a failed create", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse(documentsBody()),
      createTask: () =>
        jsonResponse(
          { error: { code: "INTERNAL_ERROR", message: "boom" } },
          500
        ),
    });
    render(<NewRequestFlow />);

    await fillToReady();
    fireEvent.click(screen.getByTestId("create-task-button"));

    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-error")
      ).toBeInTheDocument()
    );
    const objectiveInput = screen.getByTestId(
      "objective-input"
    ) as HTMLTextAreaElement;
    expect(objectiveInput.value).toBe("An objective for the task.");
  });

  it("does not contain banned wording on the rendered flow", async () => {
    installFetch({
      listProjects: () => jsonResponse(projectsBody()),
      listDocuments: () => jsonResponse(documentsBody()),
    });
    render(<NewRequestFlow />);
    await screen.findByTestId("project-select");

    const banned = [
      "truth score",
      "verified true",
      "ai verified",
      "hallucination eliminated",
      "guaranteed truth",
      "real nli",
      "contradiction detector",
      "citation-to-claim validator",
    ];
    const body = (document.body.textContent ?? "").toLowerCase();
    for (const phrase of banned) {
      expect(body.includes(phrase)).toBe(false);
    }
  });
});

describe("NewRequestFlow — idempotency key reuse", () => {
  /**
   * Install a fetch mock that records the `Idempotency-Key` header of
   * every `POST /api/ef/tasks` call and returns a per-call response
   * taken from `taskResponses` (the i-th create-task call uses the
   * i-th entry; the last entry is reused if more calls happen).
   */
  function installRecordingFetch(taskResponses: Response[]): {
    keys: string[];
  } {
    const keys: string[] = [];
    let taskCallIndex = 0;

    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();

        if (url.endsWith("/api/ef/projects") && method === "GET") {
          return jsonResponse(projectsBody());
        }
        if (url.includes("/documents") && method === "GET") {
          return jsonResponse(documentsBody());
        }
        if (url.endsWith("/api/ef/tasks") && method === "POST") {
          const headers = (init?.headers ?? {}) as Record<
            string,
            string
          >;
          keys.push(headers["Idempotency-Key"] ?? "");
          const resp =
            taskResponses[
              Math.min(taskCallIndex, taskResponses.length - 1)
            ];
          taskCallIndex += 1;
          return resp.clone();
        }
        throw new Error(`unexpected fetch: ${method} ${url}`);
      }
    ) as unknown as typeof fetch;

    return { keys };
  }

  async function fillToReady(): Promise<void> {
    const select = await screen.findByTestId("project-select");
    fireEvent.change(select, { target: { value: "p1" } });
    const checkbox = await screen.findByTestId("document-checkbox-d1");
    fireEvent.click(checkbox);
    fireEvent.change(screen.getByTestId("objective-input"), {
      target: { value: "An objective for the task." },
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-button")
      ).not.toBeDisabled()
    );
  }

  it("a retry after a failed create reuses the same Idempotency-Key", async () => {
    // Both create-task calls fail with a 5xx so the flow stays on the
    // page and a retry is possible.
    const fail = () =>
      jsonResponse(
        { error: { code: "INTERNAL_ERROR", message: "boom" } },
        500
      );
    const recorder = installRecordingFetch([fail(), fail()]);

    render(<NewRequestFlow />);
    await fillToReady();

    // First attempt → fails.
    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-error")
      ).toBeInTheDocument()
    );

    // Second click WITHOUT changing any input → retry.
    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() => expect(recorder.keys.length).toBe(2));

    // Same key on both attempts; no duplicate-creating new key.
    expect(recorder.keys[0]).not.toBe("");
    expect(recorder.keys[1]).toBe(recorder.keys[0]);
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("changing the objective after a failure forces a new Idempotency-Key", async () => {
    const fail = () =>
      jsonResponse(
        { error: { code: "INTERNAL_ERROR", message: "boom" } },
        500
      );
    const recorder = installRecordingFetch([fail(), fail()]);

    render(<NewRequestFlow />);
    await fillToReady();

    // First attempt → fails.
    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() => expect(recorder.keys.length).toBe(1));

    // The user edits the objective: the payload changed, so the next
    // attempt is a different attempt and must use a fresh key.
    fireEvent.change(screen.getByTestId("objective-input"), {
      target: { value: "A different, edited objective." },
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-button")
      ).not.toBeDisabled()
    );

    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() => expect(recorder.keys.length).toBe(2));

    expect(recorder.keys[0]).not.toBe("");
    expect(recorder.keys[1]).not.toBe("");
    expect(recorder.keys[1]).not.toBe(recorder.keys[0]);
  });

  it("changing the document selection after a failure forces a new Idempotency-Key", async () => {
    const fail = () =>
      jsonResponse(
        { error: { code: "INTERNAL_ERROR", message: "boom" } },
        500
      );
    const recorder = installRecordingFetch([fail(), fail()]);

    render(<NewRequestFlow />);
    await fillToReady();

    // First attempt → fails.
    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() => expect(recorder.keys.length).toBe(1));

    // The user de-selects the document, then re-selects it. The
    // intermediate empty selection changes the payload, so the key
    // is reset and the next attempt uses a fresh key.
    const checkbox = screen.getByTestId("document-checkbox-d1");
    fireEvent.click(checkbox); // de-select
    fireEvent.click(checkbox); // re-select
    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-button")
      ).not.toBeDisabled()
    );

    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() => expect(recorder.keys.length).toBe(2));

    expect(recorder.keys[0]).not.toBe("");
    expect(recorder.keys[1]).not.toBe("");
    expect(recorder.keys[1]).not.toBe(recorder.keys[0]);
  });

  it("a successful retry after a failure reuses the key, then navigates", async () => {
    const fail = jsonResponse(
      { error: { code: "INTERNAL_ERROR", message: "boom" } },
      500
    );
    const ok = jsonResponse({ id: "real-task-id" }, 201);
    const recorder = installRecordingFetch([fail, ok]);

    render(<NewRequestFlow />);
    await fillToReady();

    // First attempt → fails.
    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() =>
      expect(
        screen.getByTestId("create-task-error")
      ).toBeInTheDocument()
    );

    // Retry WITHOUT changing input → succeeds, same key.
    fireEvent.click(screen.getByTestId("create-task-button"));
    await waitFor(() =>
      expect(pushMock).toHaveBeenCalledWith("/tasks/real-task-id")
    );

    expect(recorder.keys.length).toBe(2);
    expect(recorder.keys[1]).toBe(recorder.keys[0]);
  });
});
