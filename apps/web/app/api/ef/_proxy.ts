/**
 * Shared helpers for the Phase UI-CREATE-FLOW-A same-origin proxy
 * route handlers (`apps/web/app/api/ef/*`).
 *
 * See `apps/web/app/api/ef/projects/route.ts` for the rationale: the
 * backend ships no CORS middleware, so create-flow POST / multipart
 * requests from the browser are routed through these server-side
 * Next.js route handlers instead. The helpers here keep every
 * handler a thin, uniform forwarder.
 *
 * Design rules:
 *   - Forward the backend status code and body VERBATIM. The browser
 *     client (`lib/api.ts`) relies on the normalized error envelope
 *     and the exact HTTP status; reshaping them would break the
 *     typed `ApiError` handling and the inline error states.
 *   - Never fabricate a success. If the backend cannot be reached,
 *     return a 502 with a normalized-shaped envelope so the client
 *     still gets a structured, honest error.
 *   - Stay inside `apps/web`; do not modify the backend.
 */

/**
 * Resolve the backend base URL for server-side forwarding.
 *
 * Resolution order mirrors `lib/api.ts#API_BASE_URL`:
 *   1. `NEXT_PUBLIC_API_BASE_URL` (also readable server-side);
 *   2. fallback `http://localhost:8000`.
 *
 * No trailing slash.
 */
export function backendBaseUrl(): string {
  return (
    (typeof process !== "undefined" &&
      process.env &&
      process.env.NEXT_PUBLIC_API_BASE_URL) ||
    "http://localhost:8000"
  );
}

/**
 * Build a 502 Response carrying a normalized error envelope.
 *
 * Used when the upstream `fetch` to the backend throws (connection
 * refused, DNS failure, etc.). The shape mirrors the backend's
 * `{ "error": { code, message, details } }` envelope so the browser
 * client parses it through the exact same `tryParseErrorEnvelope`
 * path and surfaces a structured `ApiError`.
 */
function upstreamUnreachable(cause: unknown): Response {
  const message =
    cause instanceof Error ? cause.message : String(cause);
  const envelope = {
    error: {
      code: "UPSTREAM_UNREACHABLE",
      message: `Backend API unreachable: ${message}`,
      details: { base_url: backendBaseUrl() },
    },
  };
  return new Response(JSON.stringify(envelope), {
    status: 502,
    headers: { "content-type": "application/json" },
  });
}

/**
 * Forward a JSON (or bodyless) request to the backend and relay the
 * response verbatim.
 *
 * - `method`: HTTP method.
 * - `url`: fully-qualified backend URL.
 * - `body`: optional request body string (omit for GET).
 * - `contentType`: optional Content-Type to forward (defaults to
 *   `application/json` when a body is present).
 * - `extraHeaders`: optional extra request headers (e.g.
 *   `Idempotency-Key`).
 *
 * The backend status code and body are relayed unchanged.
 */
export async function forwardJson(opts: {
  method: string;
  url: string;
  body?: string;
  contentType?: string | null;
  extraHeaders?: Record<string, string>;
}): Promise<Response> {
  const headers: Record<string, string> = { ...(opts.extraHeaders ?? {}) };
  if (opts.body !== undefined) {
    headers["Content-Type"] = opts.contentType || "application/json";
  }

  let upstream: Response;
  try {
    upstream = await fetch(opts.url, {
      method: opts.method,
      headers,
      body: opts.body,
      cache: "no-store",
    });
  } catch (e) {
    return upstreamUnreachable(e);
  }

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "content-type":
        upstream.headers.get("content-type") || "application/json",
    },
  });
}

/**
 * Forward a raw-body request (multipart upload) to the backend and
 * relay the response verbatim.
 *
 * The body is forwarded as an `ArrayBuffer` together with the
 * original `Content-Type` header — which carries the multipart
 * boundary — so the backend receives a byte-identical payload.
 */
export async function forwardRaw(opts: {
  method: string;
  url: string;
  body: ArrayBuffer;
  contentType?: string | null;
}): Promise<Response> {
  const headers: Record<string, string> = {};
  if (opts.contentType) {
    headers["Content-Type"] = opts.contentType;
  }

  let upstream: Response;
  try {
    upstream = await fetch(opts.url, {
      method: opts.method,
      headers,
      body: opts.body,
      cache: "no-store",
    });
  } catch (e) {
    return upstreamUnreachable(e);
  }

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "content-type":
        upstream.headers.get("content-type") || "application/json",
    },
  });
}
