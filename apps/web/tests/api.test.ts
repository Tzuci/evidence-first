import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import {
  API_BASE_URL,
  buildReportUrl,
  getAntiHallucinationReport,
} from "../lib/api";
import { mockNotReadyReport } from "./fixtures/reportFixtures";

/**
 * Tests for the success branch of `getAntiHallucinationReport`.
 *
 * Strategy:
 *   - Replace `globalThis.fetch` with a Vitest spy that returns a
 *     stubbed Response. No real network is involved.
 *   - Assert that the URL is built correctly (with
 *     `encodeURIComponent`), that `cache: "no-store"` is set, and
 *     that the returned object matches the JSON body.
 */

const originalFetch = globalThis.fetch;

beforeEach(() => {
  // Default to a fetch implementation that fails loudly; each test
  // overrides this with vi.fn().
  globalThis.fetch = vi.fn(async () => {
    throw new Error("fetch should have been overridden by the test");
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("buildReportUrl", () => {
  it("uses the API_BASE_URL and encodes the taskId", () => {
    const url = buildReportUrl("11112222-3333-4444-5555-666677778888");
    expect(url).toBe(
      `${API_BASE_URL}/api/v1/tasks/11112222-3333-4444-5555-666677778888/anti-hallucination-report`
    );
  });

  it("escapes characters that are not URL-safe", () => {
    const url = buildReportUrl("space here");
    expect(url).toBe(
      `${API_BASE_URL}/api/v1/tasks/space%20here/anti-hallucination-report`
    );
  });
});

describe("getAntiHallucinationReport — success", () => {
  it("calls the correct URL with cache: 'no-store' and returns the parsed body", async () => {
    const taskId = "abcd1234-5678-90ab-cdef-1234567890ab";
    const body = mockNotReadyReport();

    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      ) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    const result = await getAntiHallucinationReport(taskId);

    // Returned the parsed body.
    expect(result.task_id).toBe(body.task_id);
    expect(result.publication.status).toBe("not_ready");
    expect(result.limitations.length).toBeGreaterThan(0);

    // Exactly one fetch call, to the expected URL, with cache: "no-store".
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, calledOpts] = (fetchMock as unknown as {
      mock: { calls: unknown[][] };
    }).mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toBe(buildReportUrl(taskId));
    expect(calledOpts.cache).toBe("no-store");
  });

  it("encodes path-sensitive characters in the taskId before fetching", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(mockNotReadyReport()), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      ) as unknown as typeof fetch;
    globalThis.fetch = fetchMock;

    await getAntiHallucinationReport("with/slash");

    const calls = (fetchMock as unknown as { mock: { calls: unknown[][] } })
      .mock.calls;
    expect(calls.length).toBe(1);
    const url = calls[0][0] as string;
    expect(url).toContain("with%2Fslash");
    expect(url).not.toContain("with/slash");
  });
});
