/**
 * Idempotency-key generation for the request-creation flow
 * (Phase UI-CREATE-FLOW-A).
 *
 * `POST /api/v1/tasks` accepts an optional `Idempotency-Key` header.
 * The flow generates ONE key per submit attempt: a double click, or
 * a retry after an ambiguous network failure, then reuses the same
 * key, and the backend returns the existing task instead of creating
 * a duplicate (see PHASE_UI_CREATE_FLOW_PRE.md §8, §10).
 *
 * No new dependency is introduced. `crypto.randomUUID()` is used when
 * available (every browser the app targets, and the Next.js server
 * runtime); a deterministic-enough fallback covers exotic
 * environments without it. The fallback does NOT need to be
 * cryptographically strong — the key only has to be unique per
 * submit attempt within one project.
 */

/**
 * Generate a fresh idempotency key. Call this ONCE per submit attempt
 * and reuse the returned value across any retries of that attempt.
 */
export function generateIdempotencyKey(): string {
  const c: Crypto | undefined =
    typeof globalThis !== "undefined"
      ? (globalThis.crypto as Crypto | undefined)
      : undefined;

  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }

  // Fallback: timestamp + random segments. Uniqueness, not
  // unpredictability, is what matters for an idempotency key.
  const rand = () => Math.random().toString(16).slice(2, 10);
  return `idem-${Date.now().toString(16)}-${rand()}-${rand()}`;
}
