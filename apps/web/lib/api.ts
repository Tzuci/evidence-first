/**
 * Minimal HTTP client for the Anti-Hallucination Report API
 * (Phase 8.8B-REPORT), consumed by the UI viewer (UI-REPORT-A).
 *
 * Scope:
 *   - Single function `getAntiHallucinationReport(taskId)` calling
 *     `GET ${API_BASE_URL}/api/v1/tasks/{taskId}/anti-hallucination-report`
 *     with `cache: "no-store"`.
 *   - On a non-OK response, the function throws `ApiError` carrying
 *     the HTTP status, the normalized error envelope (when present)
 *     and the raw body string.
 *   - On a network failure (fetch threw), the function throws
 *     `ApiNetworkError` so the page can render a dedicated
 *     "API unreachable" state.
 *
 * Read-only consumer guarantees: this module never issues mutating
 * requests and never retries automatically. The route is a derived
 * view; refetch is a manual page reload.
 */

import type { AntiHallucinationReport } from "./reportTypes";

/**
 * Base URL of the Evidence-First API.
 *
 * Resolution order:
 *   1. `process.env.NEXT_PUBLIC_API_BASE_URL` (must be set at build
 *      time when deployed behind a non-localhost backend);
 *   2. fallback `http://localhost:8000` (development mode).
 *
 * No trailing slash; the endpoint helper concatenates the path
 * directly.
 */
export const API_BASE_URL: string =
  (typeof process !== "undefined" &&
    process.env &&
    process.env.NEXT_PUBLIC_API_BASE_URL) ||
  "http://localhost:8000";

/**
 * Normalized backend error envelope shape (see
 * `packages/shared/evidencefirst_shared/errors.py`).
 *
 * Every field is optional defensively: the client must not assume
 * any specific structure beyond `code` when present.
 */
export interface NormalizedErrorEnvelope {
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
  [extra: string]: unknown;
}

/**
 * Thrown when the backend responded but with a non-2xx status.
 *
 * Carries:
 *   - `status`: the HTTP status code;
 *   - `code`: the backend `error.code` (when the envelope was
 *     parseable);
 *   - `message`: the backend `error.message` (when parseable);
 *   - `details`: the backend `error.details` (when parseable);
 *   - `raw`: the raw response body string (always present), so the
 *     page can show the verbatim envelope in a `<details>` panel.
 *
 * NOTE: not an `Error` subclass via class syntax to keep transpilation
 * straightforward across targets — we extend `Error` and set the
 * prototype explicitly. Existing checks like `err instanceof ApiError`
 * work as expected.
 */
export class ApiError extends Error {
  public readonly status: number;
  public readonly code: string | null;
  public readonly details: Record<string, unknown> | null;
  public readonly raw: string;

  constructor(
    status: number,
    envelope: NormalizedErrorEnvelope | null,
    raw: string
  ) {
    const code = envelope?.code ?? null;
    const message =
      envelope?.message ?? `HTTP ${status}${code ? ` (${code})` : ""}`;
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details =
      envelope && envelope.details &&
      typeof envelope.details === "object" &&
      envelope.details !== null
        ? (envelope.details as Record<string, unknown>)
        : null;
    this.raw = raw;
    // Restore prototype for `instanceof` to work after transpile.
    Object.setPrototypeOf(this, ApiError.prototype);
  }
}

/**
 * Thrown when `fetch` itself failed (DNS error, connection refused,
 * CORS preflight failure, etc.). Distinct from `ApiError` so the page
 * can present a dedicated "API unreachable" view that includes the
 * configured `API_BASE_URL`.
 */
export class ApiNetworkError extends Error {
  public readonly cause: unknown;
  public readonly baseUrl: string;

  constructor(cause: unknown, baseUrl: string) {
    const causeMessage =
      cause instanceof Error ? cause.message : String(cause);
    super(`network error contacting ${baseUrl}: ${causeMessage}`);
    this.name = "ApiNetworkError";
    this.cause = cause;
    this.baseUrl = baseUrl;
    Object.setPrototypeOf(this, ApiNetworkError.prototype);
  }
}

/**
 * Build the endpoint URL for a given task id. Encoded defensively so
 * any non-UUID input (the route segment is a string at this layer)
 * does not produce a malformed URL.
 */
export function buildReportUrl(taskId: string): string {
  return `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(
    taskId
  )}/anti-hallucination-report`;
}

/**
 * Parse a response body string as a normalized error envelope.
 *
 * Returns `null` when:
 *   - the body is empty;
 *   - the body is not valid JSON;
 *   - the parsed JSON does not have an `error` object.
 *
 * Never throws.
 */
function tryParseErrorEnvelope(
  raw: string
): NormalizedErrorEnvelope | null {
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (
      parsed &&
      typeof parsed === "object" &&
      "error" in (parsed as Record<string, unknown>)
    ) {
      const err = (parsed as Record<string, unknown>).error;
      if (err && typeof err === "object") {
        return err as NormalizedErrorEnvelope;
      }
    }
  } catch {
    // ignore: raw body is not JSON
  }
  return null;
}

/**
 * Fetch the aggregated Anti-Hallucination Report for a given task.
 *
 * Behavior:
 *   - Uses `cache: "no-store"` (the report is a derived view; stale
 *     reads would confuse the human reviewer).
 *   - On 2xx response: parses the JSON body as
 *     `AntiHallucinationReport` and returns it. A JSON parsing error
 *     on a 2xx body is rethrown as an `ApiError` with status 0 — that
 *     case indicates the backend response is malformed, which is a
 *     contract violation worth surfacing.
 *   - On non-2xx response: throws `ApiError` carrying status, the
 *     parsed envelope (when available) and the raw body.
 *   - On fetch failure: throws `ApiNetworkError`.
 */
export async function getAntiHallucinationReport(
  taskId: string
): Promise<AntiHallucinationReport> {
  const url = buildReportUrl(taskId);

  let response: Response;
  try {
    response = await fetch(url, { cache: "no-store" });
  } catch (e) {
    throw new ApiNetworkError(e, API_BASE_URL);
  }

  // Always read the body as text first so we can preserve the raw
  // envelope for downstream rendering even on non-2xx responses where
  // .json() would discard it on parse failure.
  const raw = await response.text();

  if (!response.ok) {
    const envelope = tryParseErrorEnvelope(raw);
    throw new ApiError(response.status, envelope, raw);
  }

  try {
    return JSON.parse(raw) as AntiHallucinationReport;
  } catch (e) {
    // 2xx but malformed JSON — surface as ApiError with status 0
    // and the raw body, so the page renders a clear diagnostic.
    throw new ApiError(
      0,
      {
        code: "MALFORMED_RESPONSE",
        message:
          e instanceof Error
            ? `Server returned 2xx with non-JSON body: ${e.message}`
            : "Server returned 2xx with non-JSON body",
      },
      raw
    );
  }
}
