/**
 * HTTP client for the Evidence-First browser UI.
 *
 * Two surfaces live here:
 *
 *   1. The read-only Anti-Hallucination Report client (Phase
 *      8.8B-REPORT / UI-REPORT-A): `getAntiHallucinationReport`.
 *
 *   2. The request-creation flow client (Phase UI-CREATE-FLOW-A):
 *      `listProjects`, `createProject`, `listProjectDocuments`,
 *      `uploadProjectDocument`, `createTask`.
 *
 * Both surfaces share the same typed error model — `ApiError` for a
 * non-2xx HTTP response and `ApiNetworkError` for a `fetch` failure —
 * and the same base-URL resolution.
 *
 * Read-only vs mutating: `getAntiHallucinationReport` is strictly a
 * derived-view read. The create-flow functions DO mutate backend
 * state (create project, upload document, create task). None of them
 * retries automatically; `createTask` carries an `Idempotency-Key`
 * so a manual retry after an ambiguous failure is safe.
 *
 * Routing note (PHASE_UI_CREATE_FLOW_PRE.md §6, PHASE UI-CREATE-FLOW-A
 * §6): the report client calls the backend directly because its only
 * caller is a Next.js server component (server-side fetch — no CORS
 * involved). The create-flow client, by contrast, runs inside a
 * browser client component; the backend ships no CORS middleware
 * (see `apps/api/app/main.py`), so a direct browser call would fail
 * the CORS preflight. The create-flow functions therefore target
 * SAME-ORIGIN Next.js route handlers under `/api/ef/*`, which proxy
 * to the real backend server-side. The backend is NOT modified.
 */

import type { AntiHallucinationReport } from "./reportTypes";
import type {
  CreateProjectInput,
  CreateTaskInput,
  DocumentListResponse,
  DocumentSummary,
  ProjectListResponse,
  ProjectSummary,
  TaskCreatedResponse,
} from "./apiTypes";

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
 * Same-origin base path for the create-flow proxy route handlers.
 *
 * The create-flow client calls these instead of `API_BASE_URL`
 * directly: a browser client component cannot reach the backend
 * cross-origin without CORS, and the backend ships none. The Next.js
 * route handlers under `apps/web/app/api/ef/` forward to the backend
 * server-side. An empty string keeps the fetch URL relative
 * ("/api/ef/projects"), i.e. same-origin in both the browser and
 * jsdom test environments.
 */
export const PROXY_BASE_PATH = "/api/ef";

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

// ===========================================================================
// Phase UI-CREATE-FLOW-A — request-creation flow client
// ===========================================================================
//
// The functions below back the `/requests/new` flow. They all target
// the same-origin proxy (`PROXY_BASE_PATH`) rather than `API_BASE_URL`
// directly — see the module header for the CORS rationale.
//
// Every function follows the same contract as
// `getAntiHallucinationReport`:
//   - non-2xx response  → throws `ApiError`   (status + parsed
//                          envelope + raw body);
//   - `fetch` failure   → throws `ApiNetworkError`;
//   - 2xx + malformed   → throws `ApiError(0, MALFORMED_RESPONSE)`.

/**
 * Shared low-level request helper for the create-flow client.
 *
 * Reads the body as text first (so the raw envelope survives a
 * non-2xx response), maps failures onto the typed error model, and
 * parses a 2xx JSON body. `init` is forwarded to `fetch` verbatim so
 * callers can pass a method, headers, or a body (including
 * `FormData` for the multipart upload).
 *
 * `cache: "no-store"` is forced: create-flow reads (project list,
 * document list) must reflect the latest DB state, and a stale read
 * after the user just created a project would be confusing.
 */
async function proxyRequest<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const url = `${PROXY_BASE_PATH}${path}`;

  let response: Response;
  try {
    response = await fetch(url, { cache: "no-store", ...init });
  } catch (e) {
    throw new ApiNetworkError(e, API_BASE_URL);
  }

  const raw = await response.text();

  if (!response.ok) {
    const envelope = tryParseErrorEnvelope(raw);
    throw new ApiError(response.status, envelope, raw);
  }

  // A 204 / empty 2xx body is unexpected for these endpoints (each
  // returns a JSON object), but guard against it rather than throwing
  // an opaque JSON.parse error.
  if (raw === "") {
    throw new ApiError(
      0,
      {
        code: "MALFORMED_RESPONSE",
        message: "Server returned 2xx with an empty body",
      },
      raw
    );
  }

  try {
    return JSON.parse(raw) as T;
  } catch (e) {
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

/**
 * List the projects of the dev tenant.
 *
 * Backend contract: `GET /api/v1/projects` → `{ items, next_cursor }`.
 * The flow consumes only the first page; `next_cursor` is surfaced in
 * the returned object for a future pagination block but is not used
 * by `/requests/new` today.
 */
export async function listProjects(): Promise<ProjectListResponse> {
  const body = await proxyRequest<ProjectListResponse>("/projects", {
    method: "GET",
  });
  return {
    items: Array.isArray(body.items) ? body.items : [],
    next_cursor: body.next_cursor ?? null,
  };
}

/**
 * Create a project.
 *
 * Backend contract: `POST /api/v1/projects` with `{ name }` →
 * `201 ProjectRead`. A duplicate name in the dev tenant yields
 * `409 RESOURCE_CONFLICT`; the caller is expected to catch the
 * resulting `ApiError` and render an inline, recoverable message.
 *
 * The `name` is trimmed here so a name that is only whitespace never
 * reaches the backend; the caller should still validate non-empty
 * before invoking this function for a fast inline error.
 */
export async function createProject(
  input: CreateProjectInput
): Promise<ProjectSummary> {
  const payload: Record<string, unknown> = { name: input.name.trim() };
  if (input.mode_default !== undefined) {
    payload.mode_default = input.mode_default;
  }
  return proxyRequest<ProjectSummary>("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * List the documents already attached to a project.
 *
 * Backend contract: `GET /api/v1/projects/{project_id}/documents` →
 * `{ items }`.
 */
export async function listProjectDocuments(
  projectId: string
): Promise<DocumentListResponse> {
  const body = await proxyRequest<DocumentListResponse>(
    `/projects/${encodeURIComponent(projectId)}/documents`,
    { method: "GET" }
  );
  return { items: Array.isArray(body.items) ? body.items : [] };
}

/**
 * Upload a single `.txt` / `.md` document to a project.
 *
 * Backend contract: `POST /api/v1/projects/{project_id}/documents`
 * with a multipart body whose field name is `file` → `201
 * DocumentRead`.
 *
 * The caller is expected to have already validated the extension and
 * non-emptiness client-side for a fast error; the backend still
 * validates defensively (unsupported extension / empty file →
 * `400 VALIDATION_ERROR`, oversize → `413
 * STORAGE_INLINE_TOO_LARGE`), and those surface here as `ApiError`.
 *
 * NOTE: when `body` is a `FormData`, `fetch` sets the multipart
 * `Content-Type` (with boundary) automatically — we deliberately do
 * NOT set a `Content-Type` header here, because doing so would
 * clobber the boundary and break the upload.
 */
export async function uploadProjectDocument(
  projectId: string,
  file: File
): Promise<DocumentSummary> {
  const form = new FormData();
  form.append("file", file);
  return proxyRequest<DocumentSummary>(
    `/projects/${encodeURIComponent(projectId)}/documents`,
    {
      method: "POST",
      body: form,
    }
  );
}

/**
 * Create a real task from a project, an objective and a set of
 * document ids.
 *
 * Backend contract: `POST /api/v1/tasks` with
 * `{ project_id, objective, mode: "closed_corpus", document_ids }`
 * → `201 TaskRead`.
 *
 * The `idempotencyKey` is sent as the `Idempotency-Key` header. It
 * MUST be generated once per submit attempt by the caller: a double
 * submit then returns the same task instead of creating two. After
 * an ambiguous network failure a retry with the SAME key is safe.
 *
 * `mode` is pinned to the literal `"closed_corpus"` here regardless
 * of the input object, because it is the only value the backend
 * accepts in MVP-0; this also guarantees the wire payload always
 * carries the correct mode.
 */
export async function createTask(
  input: CreateTaskInput,
  idempotencyKey: string
): Promise<TaskCreatedResponse> {
  const payload = {
    project_id: input.project_id,
    objective: input.objective,
    mode: "closed_corpus" as const,
    document_ids: input.document_ids,
  };
  return proxyRequest<TaskCreatedResponse>("/tasks", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });
}
