import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";

import {
  ApiError,
  ApiNetworkError,
  getAntiHallucinationReport,
} from "../lib/api";

/**
 * Tests for the error branches of `getAntiHallucinationReport`.
 *
 * Coverage:
 *   - 404 RESOURCE_NOT_FOUND with details.resource='task_masters'
 *     → ApiError with status=404, code='RESOURCE_NOT_FOUND',
 *     details.resource='task_masters'.
 *   - 500 with a JSON envelope → ApiError with status=500 and the
 *     envelope members populated.
 *   - 500 with a non-JSON body → ApiError with raw body preserved.
 *   - fetch throws (TypeError, AbortError, etc.) → ApiNetworkError.
 *   - 200 with malformed body → ApiError with status=0 and
 *     code='MALFORMED_RESPONSE'.
 */

const originalFetch = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = vi.fn(async () => {
    throw new Error("fetch should have been overridden by the test");
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("getAntiHallucinationReport — error envelopes", () => {
  it("throws ApiError on 404 RESOURCE_NOT_FOUND with details.resource='task_masters'", async () => {
    const envelope = {
      error: {
        code: "RESOURCE_NOT_FOUND",
        message: "task not found",
        details: { resource: "task_masters", id: "deadbeef" },
      },
    };
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(envelope), {
          status: 404,
          headers: { "content-type": "application/json" },
        })
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await getAntiHallucinationReport("deadbeef");
    } catch (e) {
      caught = e;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(404);
    expect(err.code).toBe("RESOURCE_NOT_FOUND");
    expect(err.details?.["resource"]).toBe("task_masters");
    expect(err.raw).toContain("task not found");
  });

  it("throws ApiError on 500 with a JSON envelope", async () => {
    const envelope = {
      error: {
        code: "INTERNAL_ERROR",
        message: "boom",
        details: {},
      },
    };
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(envelope), {
          status: 500,
          headers: { "content-type": "application/json" },
        })
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await getAntiHallucinationReport("anytask");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(500);
    expect(err.code).toBe("INTERNAL_ERROR");
    expect(err.message).toContain("boom");
  });

  it("preserves the raw body on a non-JSON 500", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response("<html>500 Internal Server Error</html>", {
          status: 500,
          headers: { "content-type": "text/html" },
        })
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await getAntiHallucinationReport("anytask");
    } catch (e) {
      caught = e;
    }
    const err = caught as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(500);
    expect(err.code).toBeNull();
    expect(err.raw).toContain("500 Internal Server Error");
  });

  it("throws ApiNetworkError when fetch itself throws", async () => {
    globalThis.fetch = vi
      .fn()
      .mockRejectedValue(
        new TypeError("Failed to fetch")
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await getAntiHallucinationReport("anytask");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiNetworkError);
    const err = caught as ApiNetworkError;
    expect(err.baseUrl).toBeTypeOf("string");
    expect(err.message).toContain("Failed to fetch");
  });

  it("throws ApiError(0, MALFORMED_RESPONSE) on a 200 with non-JSON body", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response("<not json>", {
          status: 200,
          headers: { "content-type": "text/html" },
        })
      ) as unknown as typeof fetch;

    let caught: unknown;
    try {
      await getAntiHallucinationReport("anytask");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    const err = caught as ApiError;
    expect(err.status).toBe(0);
    expect(err.code).toBe("MALFORMED_RESPONSE");
    expect(err.raw).toContain("<not json>");
  });
});
