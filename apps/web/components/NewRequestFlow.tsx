"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { createTask } from "../lib/api";
import type { ProjectSummary } from "../lib/apiTypes";
import { generateIdempotencyKey } from "../lib/idempotency";
import {
  DescribedError,
  InlineError,
  describeError,
  disabledButtonStyle,
  helperTextStyle,
  primaryButtonStyle,
  sectionHeaderStyle,
  sectionStepLabelStyle,
  sectionStyle,
} from "./newRequestShared";
import NewRequestObjectiveSection from "./NewRequestObjectiveSection";
import NewRequestProjectSection from "./NewRequestProjectSection";
import NewRequestSourcesSection from "./NewRequestSourcesSection";

/**
 * Top-level client component for the `/requests/new` flow
 * (Phase UI-CREATE-FLOW-A).
 *
 * It owns the cross-section state — selected project, selected
 * document ids, objective text, and the create-task request state —
 * and renders the four ordered sections of the guided flow:
 *
 *   Step 1  Project   → NewRequestProjectSection
 *   Step 2  Sources   → NewRequestSourcesSection
 *   Step 3  Request   → NewRequestObjectiveSection
 *   Step 4  Create task (this component)
 *
 * Create-task behavior (PHASE UI-CREATE-FLOW-A §7.4, §10):
 *   - "Create task" is enabled ONLY when a project is selected, at
 *     least one document id is selected/uploaded, the objective is
 *     non-empty, and no create request is already pending;
 *   - on submit, ONE idempotency key is generated per attempt and
 *     `POST /api/v1/tasks` is called with
 *     `{ project_id, objective, mode: "closed_corpus", document_ids }`.
 *     The key is held in a ref and reused across retries of the same
 *     attempt: a retry after an ambiguous network/proxy/5xx failure
 *     sends the SAME `Idempotency-Key`, so the backend returns the
 *     existing task instead of creating a duplicate. The key is reset
 *     whenever the payload (project / document ids / objective)
 *     changes, since that is a new attempt, and cleared after a
 *     successful create;
 *   - on a real `201`, the browser navigates to
 *     `/tasks/<real returned id>`;
 *   - on any error, the flow STAYS on `/requests/new`, preserves all
 *     user input, and shows the error inline. It NEVER navigates to a
 *     task page after a failure and NEVER fabricates a task id.
 *
 * There is no fake task creation and no fake success screen anywhere:
 * the only "task created" outcome is a genuine backend `201`.
 */
export default function NewRequestFlow(): React.ReactElement {
  const router = useRouter();

  // Cross-section state.
  const [project, setProject] = React.useState<ProjectSummary | null>(
    null
  );
  const [documentIds, setDocumentIds] = React.useState<string[]>([]);
  const [objective, setObjective] = React.useState<string>("");

  // Create-task request state.
  const [createPending, setCreatePending] =
    React.useState<boolean>(false);
  const [createError, setCreateError] =
    React.useState<DescribedError | null>(null);

  // Idempotency key for the CURRENT submit attempt.
  //
  // The key must be generated exactly once per submit attempt and
  // reused across retries of that same attempt: after an ambiguous
  // network/proxy failure the user can click "Create task" again and
  // the backend, seeing the same `Idempotency-Key`, returns the
  // existing task instead of creating a duplicate.
  //
  // The key is held in a ref (not state) on purpose: changing it must
  // not trigger a re-render, and `handleCreateTask` must read the
  // freshest value synchronously within one click. It is reset to
  // null whenever the payload changes (see the effect below) — a
  // different payload is a different attempt and must NOT reuse the
  // previous key — and after a successful create + navigation.
  const idempotencyKeyRef = React.useRef<string | null>(null);

  // When the selected project changes, document selection from the
  // previous project is no longer valid — reset it.
  const handleProjectSelected = React.useCallback(
    (next: ProjectSummary | null) => {
      setProject((prev) => {
        if ((prev?.id ?? null) !== (next?.id ?? null)) {
          setDocumentIds([]);
        }
        return next;
      });
    },
    []
  );

  const objectiveTrimmed = objective.trim();

  // The payload identity for the create-task request: project id,
  // selected document ids, and the trimmed objective. When any of
  // these changes the previous submit attempt is no longer valid, so
  // the idempotency key is cleared and the next click generates a
  // fresh one. `documentIds` is joined into a stable string so the
  // effect re-runs on content changes, not array-identity changes.
  const documentIdsKey = documentIds.join(",");
  React.useEffect(() => {
    idempotencyKeyRef.current = null;
  }, [project?.id, documentIdsKey, objectiveTrimmed]);

  const canCreate =
    project !== null &&
    documentIds.length > 0 &&
    objectiveTrimmed.length > 0 &&
    !createPending;

  async function handleCreateTask(): Promise<void> {
    // Defensive: the button is disabled in this state, but guard
    // anyway so a programmatic call cannot send an invalid payload.
    if (!project || documentIds.length === 0 || !objectiveTrimmed) {
      return;
    }
    setCreatePending(true);
    setCreateError(null);

    // One idempotency key per submit attempt. The key is generated
    // only when the ref is empty; a retry after an ambiguous failure
    // (network/proxy/5xx) reuses the SAME key, so the backend returns
    // the existing task instead of creating a duplicate. The ref is
    // cleared by the payload-change effect above whenever the project,
    // document selection, or objective changes.
    if (idempotencyKeyRef.current === null) {
      idempotencyKeyRef.current = generateIdempotencyKey();
    }
    const idempotencyKey = idempotencyKeyRef.current;

    try {
      const task = await createTask(
        {
          project_id: project.id,
          objective: objectiveTrimmed,
          mode: "closed_corpus",
          document_ids: documentIds,
        },
        idempotencyKey
      );
      if (!task || typeof task.id !== "string" || task.id === "") {
        // A 2xx without a usable id is a contract violation; treat it
        // as an error rather than navigating to a fabricated URL.
        // The key is intentionally NOT cleared: a retry of this same
        // attempt must reuse it.
        setCreateError({
          headline: "Unexpected response",
          message:
            "The task was created but the response did not include " +
            "a task id. Not navigating; please check the backend.",
          code: null,
          envelope: null,
        });
        return;
      }
      // Real 201 with a real id → the attempt succeeded. Clear the
      // key so it cannot leak into an unrelated future attempt, then
      // navigate to the task summary.
      idempotencyKeyRef.current = null;
      router.push(`/tasks/${encodeURIComponent(task.id)}`);
    } catch (err) {
      // Stay on the page, preserve every input, show the error. The
      // key is intentionally NOT cleared here: clicking "Create task"
      // again without changing the input must reuse the same key.
      setCreateError(describeError(err));
    } finally {
      setCreatePending(false);
    }
  }

  // Human-readable list of what is still missing, shown when the
  // button is disabled so the user knows what to do next.
  const missing: string[] = [];
  if (!project) {
    missing.push("select or create a project");
  }
  if (documentIds.length === 0) {
    missing.push("select or upload at least one source");
  }
  if (objectiveTrimmed.length === 0) {
    missing.push("enter a request");
  }

  return (
    <div>
      <NewRequestProjectSection
        selectedProjectId={project?.id ?? null}
        onProjectSelected={handleProjectSelected}
      />

      <NewRequestSourcesSection
        projectId={project?.id ?? null}
        selectedDocumentIds={documentIds}
        onDocumentIdsChange={setDocumentIds}
      />

      <NewRequestObjectiveSection
        objective={objective}
        onObjectiveChange={setObjective}
      />

      {/* Step 4 — Create task */}
      <section
        aria-labelledby="new-request-create-heading"
        data-testid="section-create"
        style={sectionStyle}
      >
        <span style={sectionStepLabelStyle}>Step 4</span>
        <h2
          id="new-request-create-heading"
          style={sectionHeaderStyle}
        >
          Create task
        </h2>
        <p style={helperTextStyle}>
          Creating the task starts evidence-based processing. You will
          be taken to the task summary, which shows the processing
          status. The technical report stays available as a secondary,
          audit-oriented view.
        </p>

        <button
          type="button"
          data-testid="create-task-button"
          onClick={() => void handleCreateTask()}
          disabled={!canCreate}
          style={{
            ...primaryButtonStyle,
            ...(canCreate ? {} : disabledButtonStyle),
          }}
        >
          {createPending ? "Creating task…" : "Create task"}
        </button>

        {!canCreate && !createPending && missing.length > 0 ? (
          <p
            data-testid="create-task-missing"
            style={{ marginTop: 8, fontSize: 12, color: "#666" }}
          >
            Before creating the task, {missing.join(", ")}.
          </p>
        ) : null}

        {createError ? (
          <InlineError
            error={createError}
            testId="create-task-error"
          />
        ) : null}
      </section>
    </div>
  );
}
