/**
 * Same-origin proxy route handler for project document list/upload
 * (Phase UI-CREATE-FLOW-A).
 *
 * See `apps/web/app/api/ef/projects/route.ts` for the full rationale
 * behind the proxy layer. This handler covers the per-project
 * document endpoints:
 *
 *   - GET  /api/ef/projects/{projectId}/documents
 *        → GET  {API}/api/v1/projects/{projectId}/documents
 *   - POST /api/ef/projects/{projectId}/documents
 *        → POST {API}/api/v1/projects/{projectId}/documents
 *
 * The POST is a multipart upload (field name `file`). The handler
 * forwards the request body as a `Blob` together with the original
 * `Content-Type` header — which carries the multipart boundary — so
 * the backend receives a byte-identical multipart payload. It does
 * NOT re-encode or re-parse the form.
 */

import { NextRequest } from "next/server";
import { backendBaseUrl, forwardJson, forwardRaw } from "../../../_proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ projectId: string }>;
}

export async function GET(
  _request: NextRequest,
  context: RouteContext
): Promise<Response> {
  const { projectId } = await context.params;
  return forwardJson({
    method: "GET",
    url: `${backendBaseUrl()}/api/v1/projects/${encodeURIComponent(
      projectId
    )}/documents`,
  });
}

export async function POST(
  request: NextRequest,
  context: RouteContext
): Promise<Response> {
  const { projectId } = await context.params;
  // Read the raw multipart body. Forwarding it as a Blob together
  // with the original Content-Type (which holds the boundary) keeps
  // the payload byte-identical for the backend's multipart parser.
  const body = await request.arrayBuffer();
  return forwardRaw({
    method: "POST",
    url: `${backendBaseUrl()}/api/v1/projects/${encodeURIComponent(
      projectId
    )}/documents`,
    body,
    contentType: request.headers.get("content-type"),
  });
}
