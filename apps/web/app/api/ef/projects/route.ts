/**
 * Same-origin proxy route handler for project list/create
 * (Phase UI-CREATE-FLOW-A).
 *
 * Why this file exists
 * --------------------
 * The `/requests/new` flow runs in a browser client component and
 * must issue POST / multipart requests to the Evidence-First backend.
 * The backend (`apps/api/app/main.py`) ships NO CORS middleware, so a
 * direct cross-origin browser call would fail the CORS preflight.
 *
 * PHASE UI-CREATE-FLOW-A §6 permits a minimal same-origin Next.js
 * route/proxy when direct browser fetch is not viable, provided the
 * backend is not modified and the proxy stays inside `apps/web`. This
 * handler is exactly that: it runs server-side (same origin as the
 * page from the browser's point of view), forwards the request to the
 * real backend, and streams the response back verbatim.
 *
 * It is a DUMB forwarder:
 *   - it does not interpret, reshape, or fake any response;
 *   - it preserves the backend status code and body verbatim, so the
 *     typed `ApiError` / envelope handling in `lib/api.ts` works
 *     unchanged;
 *   - on a backend connection failure it returns 502 with a
 *     normalized envelope so the client still sees a structured error
 *     (the client maps a thrown `fetch` to `ApiNetworkError`; a 502
 *     here is surfaced as an `ApiError`, which the flow renders too).
 *
 * Endpoints proxied:
 *   - GET  /api/ef/projects  →  GET  {API}/api/v1/projects
 *   - POST /api/ef/projects  →  POST {API}/api/v1/projects
 */

import { NextRequest } from "next/server";
import { backendBaseUrl, forwardJson } from "../_proxy";

// This route forwards a live request to the backend; it must never be
// statically optimized or cached.
export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return forwardJson({
    method: "GET",
    url: `${backendBaseUrl()}/api/v1/projects`,
  });
}

export async function POST(request: NextRequest): Promise<Response> {
  const body = await request.text();
  return forwardJson({
    method: "POST",
    url: `${backendBaseUrl()}/api/v1/projects`,
    body,
    contentType: request.headers.get("content-type"),
  });
}
