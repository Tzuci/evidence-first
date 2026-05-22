/**
 * TypeScript types for the request-creation flow API client
 * (Phase UI-CREATE-FLOW-A).
 *
 * These interfaces mirror the JSON shapes produced by the backend
 * routes consumed by `/requests/new`:
 *   - `POST /api/v1/projects`              → ProjectRead
 *   - `GET  /api/v1/projects`              → { items: ProjectRead[], next_cursor }
 *   - `POST /api/v1/projects/{id}/documents` → DocumentRead
 *   - `GET  /api/v1/projects/{id}/documents` → { items: DocumentRead[] }
 *   - `POST /api/v1/tasks`                 → TaskRead
 *
 * They are intentionally manual TypeScript declarations (no codegen,
 * no Pydantic import) — a wrapper the UI can evolve independently
 * from the backend shape if needed, exactly as `reportTypes.ts` does
 * for the report viewer.
 *
 * Semantic note (preserved from PHASE_UI_CREATE_FLOW_PRE.md §12):
 *   - Creating a task only enqueues a `task.created` event. The task
 *     advances only if the worker is running. The UI does NOT block
 *     on the worker; it navigates to the task summary, which honestly
 *     renders "Not ready yet" until the worker has done its work.
 */

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

/**
 * A project row as returned by `POST /api/v1/projects` and listed by
 * `GET /api/v1/projects`. Mirrors the backend `ProjectRead` schema.
 *
 * Only the fields the flow actually consumes are typed strictly; the
 * index signature keeps the type forward-compatible with extra
 * backend fields.
 */
export interface ProjectSummary {
  id: string;
  tenant_id: string | null;
  name: string;
  mode_default: string | null;
  created_by: string | null;
  created_at: string | null;
}

/**
 * Response envelope of `GET /api/v1/projects`. The backend paginates
 * with a cursor; the flow only consumes the first page (project
 * counts in MVP-0 dev mode are small) but the `next_cursor` field is
 * surfaced so a future block can wire pagination without a type
 * change.
 */
export interface ProjectListResponse {
  items: ProjectSummary[];
  next_cursor: string | null;
}

/**
 * Input for `createProject`. Only `name` is required; `mode_default`
 * is optional and omitted by the flow (the backend applies its own
 * default).
 */
export interface CreateProjectInput {
  name: string;
  mode_default?: string;
}

// ---------------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------------

/**
 * A document row as returned by `POST /api/v1/projects/{id}/documents`
 * and listed by `GET /api/v1/projects/{id}/documents`. Mirrors the
 * backend `DocumentRead` schema.
 */
export interface DocumentSummary {
  id: string;
  tenant_id: string | null;
  project_id: string | null;
  filename: string;
  content_hash: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  tier: string | null;
  language: string | null;
  created_by: string | null;
  created_at: string | null;
}

/**
 * Response envelope of `GET /api/v1/projects/{id}/documents`.
 */
export interface DocumentListResponse {
  items: DocumentSummary[];
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

/**
 * Input for `createTask`. `mode` is always the literal
 * `"closed_corpus"` — the only value the backend accepts in MVP-0.
 * `document_ids` must contain at least one id (a task with zero
 * documents goes `analyzing → blocked` and produces no useful
 * answer; the flow enforces this before enabling "Create task").
 */
export interface CreateTaskInput {
  project_id: string;
  objective: string;
  mode: "closed_corpus";
  document_ids: string[];
}

/**
 * A task row as returned by `POST /api/v1/tasks`. Mirrors the backend
 * `TaskRead` schema. The flow only needs `id` to navigate, but the
 * rest of the shape is typed for completeness.
 */
export interface TaskCreatedResponse {
  id: string;
  tenant_id: string | null;
  project_id: string | null;
  mode: string | null;
  objective: string | null;
  status: string | null;
  policy: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}
