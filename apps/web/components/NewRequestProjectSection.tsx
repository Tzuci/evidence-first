"use client";

import * as React from "react";

import {
  createProject,
  listProjects,
} from "../lib/api";
import type { ProjectSummary } from "../lib/apiTypes";
import {
  DescribedError,
  InlineError,
  describeError,
  disabledButtonStyle,
  helperTextStyle,
  isApiErrorCode,
  labelStyle,
  primaryButtonStyle,
  secondaryButtonStyle,
  sectionHeaderStyle,
  sectionStepLabelStyle,
  sectionStyle,
  selectStyle,
  textInputStyle,
} from "./newRequestShared";

/**
 * Section 1 of the `/requests/new` flow — Project
 * (Phase UI-CREATE-FLOW-A §7.1).
 *
 * The user either selects an existing project or creates a new one.
 * On mount the section lists existing projects via the real
 * `GET /api/v1/projects`. "Create a new project" is the secondary
 * action; if the project list is empty the create form is shown
 * first (the flow discovers the empty state rather than assuming a
 * seed project).
 *
 * Error handling (PHASE UI-CREATE-FLOW-A §10):
 *   - a failing project list shows an inline API/network error and
 *     does NOT silently hide the rest of the page;
 *   - `RESOURCE_CONFLICT` on create is shown inline and is
 *     recoverable — the user can pick another name or select the
 *     existing project;
 *   - a validation / network error on create is shown inline too.
 *
 * The selected project id is lifted to the parent flow via
 * `onProjectSelected`; the parent owns the cross-section state.
 */
export interface NewRequestProjectSectionProps {
  /** Currently selected project id (owned by the parent flow). */
  selectedProjectId: string | null;
  /** Called whenever the selected project changes (id + summary). */
  onProjectSelected: (project: ProjectSummary | null) => void;
}

export default function NewRequestProjectSection(
  props: NewRequestProjectSectionProps
): React.ReactElement {
  const { selectedProjectId, onProjectSelected } = props;

  const [projects, setProjects] = React.useState<ProjectSummary[]>([]);
  const [listLoading, setListLoading] = React.useState<boolean>(true);
  const [listError, setListError] =
    React.useState<DescribedError | null>(null);

  const [mode, setMode] = React.useState<"select" | "create">("select");
  const [newName, setNewName] = React.useState<string>("");
  const [createPending, setCreatePending] =
    React.useState<boolean>(false);
  const [createError, setCreateError] =
    React.useState<DescribedError | null>(null);

  // ---- Load the project list once on mount -------------------------------
  const loadProjects = React.useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const res = await listProjects();
      setProjects(res.items);
      // If there are no projects yet, surface the create form first:
      // the flow discovers the empty state instead of assuming a
      // seeded project exists.
      if (res.items.length === 0) {
        setMode("create");
      }
    } catch (err) {
      setListError(describeError(err));
    } finally {
      setListLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  // ---- Select an existing project ----------------------------------------
  function handleSelectChange(
    e: React.ChangeEvent<HTMLSelectElement>
  ): void {
    const id = e.target.value;
    if (!id) {
      onProjectSelected(null);
      return;
    }
    const project = projects.find((p) => p.id === id) ?? null;
    onProjectSelected(project);
  }

  // ---- Create a new project ----------------------------------------------
  async function handleCreate(): Promise<void> {
    const trimmed = newName.trim();
    if (!trimmed) {
      setCreateError({
        headline: "Project name required",
        message: "Enter a non-empty project name.",
        code: null,
        envelope: null,
      });
      return;
    }
    setCreatePending(true);
    setCreateError(null);
    try {
      const created = await createProject({ name: trimmed });
      // Add the new project to the list, select it, and switch back
      // to the select view so the new project is the active choice.
      setProjects((prev) => {
        const exists = prev.some((p) => p.id === created.id);
        return exists ? prev : [created, ...prev];
      });
      onProjectSelected(created);
      setNewName("");
      setMode("select");
    } catch (err) {
      if (isApiErrorCode(err, "RESOURCE_CONFLICT")) {
        setCreateError({
          headline: "Project name already in use",
          message:
            `A project named "${trimmed}" already exists. Pick a ` +
            "different name, or select the existing project above.",
          code: "RESOURCE_CONFLICT",
          envelope: null,
        });
        // A conflict means the project exists — refresh the list so
        // the user can select it without leaving the section.
        void loadProjects();
      } else {
        setCreateError(describeError(err));
      }
    } finally {
      setCreatePending(false);
    }
  }

  // ---- Render ------------------------------------------------------------
  return (
    <section
      aria-labelledby="new-request-project-heading"
      data-testid="section-project"
      style={sectionStyle}
    >
      <span style={sectionStepLabelStyle}>Step 1</span>
      <h2
        id="new-request-project-heading"
        style={sectionHeaderStyle}
      >
        Project
      </h2>
      <p style={helperTextStyle}>
        Choose an existing project or create a new one. The request
        and its sources belong to this project.
      </p>

      {listError ? (
        <InlineError error={listError} testId="project-list-error" />
      ) : null}

      {listLoading ? (
        <p style={{ fontSize: 13, color: "#666" }}>
          Loading projects…
        </p>
      ) : null}

      {/* Select-existing view */}
      {!listLoading && mode === "select" ? (
        <div>
          <label htmlFor="project-select" style={labelStyle}>
            Existing project
          </label>
          <select
            id="project-select"
            data-testid="project-select"
            value={selectedProjectId ?? ""}
            onChange={handleSelectChange}
            style={selectStyle}
          >
            <option value="">— Select a project —</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          {projects.length === 0 ? (
            <p style={{ ...helperTextStyle, marginTop: 8 }}>
              No projects yet. Create one below.
            </p>
          ) : null}
          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              data-testid="show-create-project"
              onClick={() => {
                setMode("create");
                setCreateError(null);
              }}
              style={secondaryButtonStyle}
            >
              Create a new project
            </button>
          </div>
        </div>
      ) : null}

      {/* Create-new view */}
      {!listLoading && mode === "create" ? (
        <div>
          <label htmlFor="new-project-name" style={labelStyle}>
            New project name
          </label>
          <input
            id="new-project-name"
            data-testid="new-project-name"
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="e.g. Q4 revenue review"
            autoComplete="off"
            style={textInputStyle}
          />
          <div
            style={{
              display: "flex",
              gap: 8,
              marginTop: 12,
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              data-testid="create-project-button"
              onClick={() => void handleCreate()}
              disabled={createPending}
              style={{
                ...primaryButtonStyle,
                ...(createPending ? disabledButtonStyle : {}),
              }}
            >
              {createPending ? "Creating…" : "Create project"}
            </button>
            {projects.length > 0 ? (
              <button
                type="button"
                data-testid="cancel-create-project"
                onClick={() => {
                  setMode("select");
                  setCreateError(null);
                }}
                style={secondaryButtonStyle}
              >
                Select an existing project instead
              </button>
            ) : null}
          </div>
          {createError ? (
            <InlineError
              error={createError}
              testId="project-create-error"
            />
          ) : null}
        </div>
      ) : null}

      {/* Confirmation of the current selection */}
      {selectedProjectId ? (
        <p
          data-testid="project-selected-note"
          style={{
            marginTop: 12,
            fontSize: 13,
            color: "#1f5d3a",
          }}
        >
          Selected project:{" "}
          {projects.find((p) => p.id === selectedProjectId)?.name ??
            selectedProjectId}
        </p>
      ) : null}
    </section>
  );
}
