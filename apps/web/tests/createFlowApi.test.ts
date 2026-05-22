import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import {
  ApiError,
  ApiNetworkError,
  PROXY_BASE_PATH,
  createProject,
  createTask,
  listProjectDocuments,
  listProjects,
  uploadProjectDocument,
} from "../lib/api";

/**
 * Tests for the request-creation flow API client functions
 * (Phase UI-CREATE-FLOW-A): listProjects, createProject,
 * listProjectDocuments, uploadProjectDocument, createTask.
 *
 * All five target the same-origin proxy (`PROXY_BASE_PATH`), not the
 * backend directly. `fetch` is replaced with a Vitest spy; no real
 * network is involved.
 */

const originalFetch = globalThis.fetch;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => {
    throw new Error("fetch should have been overridden by the test");
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// listProjects
// ---------------------------------------------------------------------------
describe("listProjects", () => {
  it("GETs the proxy projects path and returns the items", async () => {
    const body = {
      items: [
        {
          id: "p1",
          tenant_id: "t1",
          name: "Demo project",
          mode_default: "closed_corpus",
          created_by: "u1",
          created_at: "2026-05-19T09:00:00Z",
        },
      ],
      next_cursor: null,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(body)) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const result = await listProjects();

    expect(result.items.length).toBe(1);
    expect(result.items[0].name).toBe("Demo project");
    expect(result.next_cursor).toBeNull();

    const [url, opts] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${PROXY_BASE_PATH}/projects`);
    expect(opts.method).toBe("GET");
  });

  it("normalizes a missing items array to an empty list", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({})) as unknown as typeof fetch;
    const result = await listProjects();
    expect(result.items).toEqual([]);
  });

  it("throws ApiError on a non-2xx response", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          { error: { code: "INTERNAL_ERROR", message: "boom" } },
          500
        )
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await listProjects();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(500);
  });

  it("throws ApiNetworkError when fetch itself fails", async () => {
    globalThis.fetch = vi
      .fn()
      .mockRejectedValue(
        new TypeError("Failed to fetch")
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await listProjects();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
  });
});

// ---------------------------------------------------------------------------
// createProject
// ---------------------------------------------------------------------------
describe("createProject", () => {
  it("POSTs the trimmed name and returns the created project", async () => {
    const created = {
      id: "p2",
      tenant_id: "t1",
      name: "New project",
      mode_default: "closed_corpus",
      created_by: "u1",
      created_at: "2026-05-19T09:30:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(created, 201)) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const result = await createProject({ name: "  New project  " });
    expect(result.id).toBe("p2");

    const [url, opts] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${PROXY_BASE_PATH}/projects`);
    expect(opts.method).toBe("POST");
    const sent = JSON.parse(String(opts.body));
    expect(sent.name).toBe("New project");
  });

  it("throws ApiError on RESOURCE_CONFLICT (409)", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "RESOURCE_CONFLICT",
              message: "Project with name 'x' already exists",
            },
          },
          409
        )
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await createProject({ name: "x" });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(409);
    expect(err.code).toBe("RESOURCE_CONFLICT");
  });

  it("throws ApiError on a validation error (400)", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          { error: { code: "VALIDATION_ERROR", message: "bad name" } },
          400
        )
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await createProject({ name: "x" });
    } catch (e) {
      caught = e;
    }
    expect((caught as ApiError).code).toBe("VALIDATION_ERROR");
  });

  it("throws ApiNetworkError when fetch fails", async () => {
    globalThis.fetch = vi
      .fn()
      .mockRejectedValue(
        new TypeError("connection refused")
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await createProject({ name: "x" });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
  });
});

// ---------------------------------------------------------------------------
// listProjectDocuments
// ---------------------------------------------------------------------------
describe("listProjectDocuments", () => {
  it("GETs the project documents path and returns the items", async () => {
    const body = {
      items: [
        {
          id: "d1",
          tenant_id: "t1",
          project_id: "p1",
          filename: "doc.txt",
          content_hash: "h",
          mime_type: "text/plain",
          size_bytes: 10,
          tier: "user_provided",
          language: "en",
          created_by: "u1",
          created_at: "2026-05-19T09:00:00Z",
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(body)) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const result = await listProjectDocuments("p1");
    expect(result.items.length).toBe(1);
    expect(result.items[0].filename).toBe("doc.txt");

    const [url] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${PROXY_BASE_PATH}/projects/p1/documents`);
  });

  it("encodes the project id in the path", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ items: [] })) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    await listProjectDocuments("p/1");
    const [url] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(url).toContain("p%2F1");
  });

  it("throws ApiNetworkError when fetch fails", async () => {
    globalThis.fetch = vi
      .fn()
      .mockRejectedValue(new TypeError("down")) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await listProjectDocuments("p1");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
  });
});

// ---------------------------------------------------------------------------
// uploadProjectDocument
// ---------------------------------------------------------------------------
describe("uploadProjectDocument", () => {
  it("POSTs a multipart body with the field name 'file'", async () => {
    const created = {
      id: "d2",
      tenant_id: "t1",
      project_id: "p1",
      filename: "uploaded.md",
      content_hash: "h",
      mime_type: "text/markdown",
      size_bytes: 12,
      tier: "user_provided",
      language: "en",
      created_by: "u1",
      created_at: "2026-05-19T10:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(created, 201)) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const file = new File(["# hello"], "uploaded.md", {
      type: "text/markdown",
    });
    const result = await uploadProjectDocument("p1", file);
    expect(result.id).toBe("d2");

    const [url, opts] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${PROXY_BASE_PATH}/projects/p1/documents`);
    expect(opts.method).toBe("POST");
    expect(opts.body).toBeInstanceOf(FormData);
    expect((opts.body as FormData).get("file")).toBeInstanceOf(File);
    // The Content-Type must NOT be set manually (fetch adds the
    // multipart boundary itself).
    const headers = (opts.headers ?? {}) as Record<string, string>;
    expect(headers["Content-Type"]).toBeUndefined();
  });

  it("throws ApiError on a backend validation error (400)", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "VALIDATION_ERROR",
              message: "Unsupported file extension",
            },
          },
          400
        )
      ) as unknown as typeof fetch;

    const file = new File(["x"], "bad.pdf", { type: "application/pdf" });
    let caught: unknown;
    try {
      await uploadProjectDocument("p1", file);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).code).toBe("VALIDATION_ERROR");
  });

  it("throws ApiNetworkError when fetch fails", async () => {
    globalThis.fetch = vi
      .fn()
      .mockRejectedValue(new TypeError("down")) as unknown as typeof fetch;

    const file = new File(["x"], "a.txt", { type: "text/plain" });
    let caught: unknown;
    try {
      await uploadProjectDocument("p1", file);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
  });
});

// ---------------------------------------------------------------------------
// createTask
// ---------------------------------------------------------------------------
describe("createTask", () => {
  it("POSTs the task and sends the Idempotency-Key header", async () => {
    const created = {
      id: "task-1",
      tenant_id: "t1",
      project_id: "p1",
      mode: "closed_corpus",
      objective: "An objective",
      status: "created",
      policy: {},
      created_at: "2026-05-19T10:00:00Z",
      updated_at: "2026-05-19T10:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(created, 201)) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const result = await createTask(
      {
        project_id: "p1",
        objective: "An objective",
        mode: "closed_corpus",
        document_ids: ["d1"],
      },
      "idem-key-123"
    );
    expect(result.id).toBe("task-1");

    const [url, opts] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${PROXY_BASE_PATH}/tasks`);
    expect(opts.method).toBe("POST");
    const headers = (opts.headers ?? {}) as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("idem-key-123");
  });

  it("sends mode='closed_corpus' in the payload", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ id: "task-2" }, 201)
      ) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    await createTask(
      {
        project_id: "p1",
        objective: "obj",
        mode: "closed_corpus",
        document_ids: ["d1", "d2"],
      },
      "k"
    );

    const [, opts] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    const sent = JSON.parse(String(opts.body));
    expect(sent.mode).toBe("closed_corpus");
    expect(sent.project_id).toBe("p1");
    expect(sent.objective).toBe("obj");
    expect(sent.document_ids).toEqual(["d1", "d2"]);
  });

  it("throws ApiError on a document validation error (400)", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: "VALIDATION_ERROR",
              message: "foreign document",
              details: { foreign: ["d9"] },
            },
          },
          400
        )
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await createTask(
        {
          project_id: "p1",
          objective: "obj",
          mode: "closed_corpus",
          document_ids: ["d9"],
        },
        "k"
      );
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(400);
    expect(err.details?.["foreign"]).toEqual(["d9"]);
  });

  it("throws ApiError on a backend 5xx", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          { error: { code: "INTERNAL_ERROR", message: "boom" } },
          500
        )
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await createTask(
        {
          project_id: "p1",
          objective: "obj",
          mode: "closed_corpus",
          document_ids: ["d1"],
        },
        "k"
      );
    } catch (e) {
      caught = e;
    }
    expect((caught as ApiError).status).toBe(500);
  });

  it("throws ApiNetworkError when fetch fails", async () => {
    globalThis.fetch = vi
      .fn()
      .mockRejectedValue(new TypeError("down")) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await createTask(
        {
          project_id: "p1",
          objective: "obj",
          mode: "closed_corpus",
          document_ids: ["d1"],
        },
        "k"
      );
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
  });
});
