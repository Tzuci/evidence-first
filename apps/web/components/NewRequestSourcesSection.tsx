"use client";

import * as React from "react";

import {
  listProjectDocuments,
  uploadProjectDocument,
} from "../lib/api";
import type { DocumentSummary } from "../lib/apiTypes";
import {
  DescribedError,
  InlineError,
  describeError,
  disabledButtonStyle,
  helperTextStyle,
  labelStyle,
  monoStyle,
  primaryButtonStyle,
  sectionHeaderStyle,
  sectionStepLabelStyle,
  sectionStyle,
} from "./newRequestShared";

/**
 * Section 2 of the `/requests/new` flow — Sources
 * (Phase UI-CREATE-FLOW-A §7.2).
 *
 * The user attaches one or more `.txt` / `.md` documents to the
 * selected project. They can:
 *   - see the documents already in the project (real
 *     `GET /api/v1/projects/{id}/documents`);
 *   - select one or more of them;
 *   - upload new `.txt` / `.md` files (real
 *     `POST /api/v1/projects/{id}/documents`), which are added to the
 *     selected set.
 *
 * Client-side validation (PHASE UI-CREATE-FLOW-A §7.2, §10):
 *   - only `.txt` and `.md` extensions are accepted;
 *   - an empty file is rejected before the request;
 *   - the backend still validates defensively — an unsupported
 *     extension, empty file, oversize file or other validation error
 *     surfaces as an inline `ApiError`.
 *
 * The combined set of selected + uploaded document ids is lifted to
 * the parent flow via `onDocumentIdsChange`.
 */
export interface NewRequestSourcesSectionProps {
  /** The selected project id, or null when no project is chosen yet. */
  projectId: string | null;
  /** The currently selected document ids (owned by the parent flow). */
  selectedDocumentIds: string[];
  /** Called whenever the selected document id set changes. */
  onDocumentIdsChange: (ids: string[]) => void;
}

/** Allowed upload extensions (lower-case, without the dot). */
const ALLOWED_EXTENSIONS = ["txt", "md"];

/** Extract the lower-case extension of a filename, or "" when none. */
function fileExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0 || dot === filename.length - 1) {
    return "";
  }
  return filename.slice(dot + 1).toLowerCase();
}

export default function NewRequestSourcesSection(
  props: NewRequestSourcesSectionProps
): React.ReactElement {
  const { projectId, selectedDocumentIds, onDocumentIdsChange } = props;

  const [documents, setDocuments] = React.useState<DocumentSummary[]>(
    []
  );
  const [listLoading, setListLoading] = React.useState<boolean>(false);
  const [listError, setListError] =
    React.useState<DescribedError | null>(null);

  const [pendingFile, setPendingFile] = React.useState<File | null>(
    null
  );
  const [uploadPending, setUploadPending] =
    React.useState<boolean>(false);
  const [uploadError, setUploadError] =
    React.useState<DescribedError | null>(null);
  // Bumped after each successful upload to reset the <input type=file>.
  const [fileInputKey, setFileInputKey] = React.useState<number>(0);

  // ---- Load the project's documents whenever the project changes ---------
  const loadDocuments = React.useCallback(
    async (pid: string) => {
      setListLoading(true);
      setListError(null);
      try {
        const res = await listProjectDocuments(pid);
        setDocuments(res.items);
      } catch (err) {
        setListError(describeError(err));
      } finally {
        setListLoading(false);
      }
    },
    []
  );

  React.useEffect(() => {
    if (!projectId) {
      setDocuments([]);
      setListError(null);
      return;
    }
    void loadDocuments(projectId);
  }, [projectId, loadDocuments]);

  // ---- Toggle an existing document in/out of the selected set ------------
  function toggleDocument(id: string): void {
    if (selectedDocumentIds.includes(id)) {
      onDocumentIdsChange(
        selectedDocumentIds.filter((d) => d !== id)
      );
    } else {
      onDocumentIdsChange([...selectedDocumentIds, id]);
    }
  }

  // ---- Pick a file for upload --------------------------------------------
  function handleFileChange(
    e: React.ChangeEvent<HTMLInputElement>
  ): void {
    setUploadError(null);
    const file = e.target.files && e.target.files[0];
    if (!file) {
      setPendingFile(null);
      return;
    }
    const ext = fileExtension(file.name);
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      setPendingFile(null);
      setUploadError({
        headline: "Unsupported file type",
        message:
          `"${file.name}" is not a .txt or .md file. Only .txt ` +
          "and .md sources can be attached.",
        code: null,
        envelope: null,
      });
      return;
    }
    if (file.size === 0) {
      setPendingFile(null);
      setUploadError({
        headline: "Empty file",
        message: `"${file.name}" is empty. Choose a file with content.`,
        code: null,
        envelope: null,
      });
      return;
    }
    setPendingFile(file);
  }

  // ---- Upload the picked file --------------------------------------------
  async function handleUpload(): Promise<void> {
    if (!projectId || !pendingFile) {
      return;
    }
    setUploadPending(true);
    setUploadError(null);
    try {
      const created = await uploadProjectDocument(
        projectId,
        pendingFile
      );
      // Add the new document to the visible list and to the selected
      // set, so an uploaded source is attached to the task by default.
      setDocuments((prev) => {
        const exists = prev.some((d) => d.id === created.id);
        return exists ? prev : [created, ...prev];
      });
      if (!selectedDocumentIds.includes(created.id)) {
        onDocumentIdsChange([...selectedDocumentIds, created.id]);
      }
      setPendingFile(null);
      // Reset the file input so the same file can be re-picked later.
      setFileInputKey((k) => k + 1);
    } catch (err) {
      setUploadError(describeError(err));
    } finally {
      setUploadPending(false);
    }
  }

  // ---- Render ------------------------------------------------------------
  return (
    <section
      aria-labelledby="new-request-sources-heading"
      data-testid="section-sources"
      style={sectionStyle}
    >
      <span style={sectionStepLabelStyle}>Step 2</span>
      <h2
        id="new-request-sources-heading"
        style={sectionHeaderStyle}
      >
        Sources
      </h2>
      <p style={helperTextStyle}>
        Attach the available sources the request should use. Select
        existing documents and/or upload new .txt or .md files. At
        least one source is required.
      </p>

      {!projectId ? (
        <p
          data-testid="sources-need-project"
          style={{ fontSize: 13, color: "#666" }}
        >
          Select or create a project first to manage its sources.
        </p>
      ) : (
        <div>
          {/* Existing documents */}
          <div style={{ marginBottom: 16 }}>
            <span style={labelStyle}>Documents in this project</span>
            {listError ? (
              <InlineError
                error={listError}
                testId="documents-list-error"
              />
            ) : null}
            {listLoading ? (
              <p style={{ fontSize: 13, color: "#666" }}>
                Loading documents…
              </p>
            ) : null}
            {!listLoading && !listError && documents.length === 0 ? (
              <p style={{ fontSize: 13, color: "#666" }}>
                No documents in this project yet. Upload one below.
              </p>
            ) : null}
            {!listLoading && documents.length > 0 ? (
              <ul
                data-testid="documents-list"
                style={{
                  listStyle: "none",
                  margin: 0,
                  marginTop: 6,
                  padding: 0,
                }}
              >
                {documents.map((d) => {
                  const checked = selectedDocumentIds.includes(d.id);
                  return (
                    <li
                      key={d.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        padding: "6px 8px",
                        border: "1px solid #e6e9ed",
                        borderRadius: 4,
                        marginTop: 6,
                        background: checked ? "#f3f7fc" : "#fff",
                      }}
                    >
                      <input
                        type="checkbox"
                        data-testid={`document-checkbox-${d.id}`}
                        aria-label={`Select document ${d.filename}`}
                        checked={checked}
                        onChange={() => toggleDocument(d.id)}
                      />
                      <span style={{ fontSize: 13, color: "#222" }}>
                        {d.filename}
                      </span>
                      <span
                        style={{
                          ...monoStyle,
                          color: "#999",
                          marginLeft: "auto",
                        }}
                      >
                        {d.id.slice(0, 8)}…
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </div>

          {/* Upload */}
          <div>
            <label htmlFor="source-file-input" style={labelStyle}>
              Upload a new source (.txt or .md)
            </label>
            <input
              id="source-file-input"
              data-testid="source-file-input"
              key={fileInputKey}
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              onChange={handleFileChange}
              style={{ fontSize: 13 }}
            />
            <div style={{ marginTop: 10 }}>
              <button
                type="button"
                data-testid="upload-document-button"
                onClick={() => void handleUpload()}
                disabled={!pendingFile || uploadPending}
                style={{
                  ...primaryButtonStyle,
                  ...(!pendingFile || uploadPending
                    ? disabledButtonStyle
                    : {}),
                }}
              >
                {uploadPending ? "Uploading…" : "Upload document"}
              </button>
            </div>
            {uploadError ? (
              <InlineError
                error={uploadError}
                testId="document-upload-error"
              />
            ) : null}
          </div>

          {/* Selection summary */}
          <p
            data-testid="sources-selected-count"
            style={{
              marginTop: 14,
              fontSize: 13,
              color:
                selectedDocumentIds.length > 0 ? "#1f5d3a" : "#666",
            }}
          >
            {selectedDocumentIds.length === 0
              ? "No sources selected yet. Select or upload at least one."
              : `${selectedDocumentIds.length} source(s) selected.`}
          </p>
        </div>
      )}
    </section>
  );
}
