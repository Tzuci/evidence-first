/**
 * Same-origin proxy route handler for task creation
 * (Phase UI-CREATE-FLOW-A).
 *
 * See `apps/web/app/api/ef/projects/route.ts` for the full rationale
 * behind the proxy layer. This handler covers task creation:
 *
 *   - POST /api/ef/tasks  →  POST {API}/api/v1/tasks
 *
 * The `Idempotency-Key` request header is forwarded verbatim to the
 * backend. It MUST be: the backend uses it to deduplicate a double
 * submit (a repeated key returns the existing task instead of
 * creating a second one). Dropping it here would break safe retries.
 */

import { NextRequest } from "next/server";
import { backendBaseUrl, forwardJson } from "../_proxy";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest): Promise<Response> {
  const body = await request.text();
  const idempotencyKey = request.headers.get("idempotency-key");
  return forwardJson({
    method: "POST",
    url: `${backendBaseUrl()}/api/v1/tasks`,
    body,
    contentType: request.headers.get("content-type"),
    extraHeaders: idempotencyKey
      ? { "Idempotency-Key": idempotencyKey }
      : undefined,
  });
}
