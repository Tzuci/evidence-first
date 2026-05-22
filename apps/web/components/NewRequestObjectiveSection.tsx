"use client";

import * as React from "react";

import {
  helperTextStyle,
  labelStyle,
  sectionHeaderStyle,
  sectionStepLabelStyle,
  sectionStyle,
  textAreaStyle,
} from "./newRequestShared";

/**
 * Section 3 of the `/requests/new` flow — Request
 * (Phase UI-CREATE-FLOW-A §7.3).
 *
 * The user enters the objective: the question or a description of
 * the answer they need. This maps to `TaskCreate.objective`.
 *
 * Semantic guardrails (PHASE UI-CREATE-FLOW-A §9, §7.3):
 *   - the field is labelled "Request"; it is NEVER labelled
 *     "truth verification";
 *   - the helper copy stays sober — it does not promise factual
 *     certainty. It uses the prescribed safe wording: the system
 *     uses the available sources attached to the request and may
 *     hold publication when support is insufficient.
 *
 * The objective value is lifted to the parent flow via
 * `onObjectiveChange`; the parent owns the cross-section state and
 * the create-task gating.
 */
export interface NewRequestObjectiveSectionProps {
  /** Current objective text (owned by the parent flow). */
  objective: string;
  /** Called whenever the objective text changes. */
  onObjectiveChange: (value: string) => void;
}

export default function NewRequestObjectiveSection(
  props: NewRequestObjectiveSectionProps
): React.ReactElement {
  const { objective, onObjectiveChange } = props;

  return (
    <section
      aria-labelledby="new-request-objective-heading"
      data-testid="section-objective"
      style={sectionStyle}
    >
      <span style={sectionStepLabelStyle}>Step 3</span>
      <h2
        id="new-request-objective-heading"
        style={sectionHeaderStyle}
      >
        Request
      </h2>
      <p style={helperTextStyle}>
        Ask a question or describe the answer you need. Evidence-First
        will use the available sources attached to the request and may
        hold publication when support is insufficient.
      </p>

      <label htmlFor="objective-input" style={labelStyle}>
        Request
      </label>
      <textarea
        id="objective-input"
        data-testid="objective-input"
        value={objective}
        onChange={(e) => onObjectiveChange(e.target.value)}
        placeholder="e.g. What was the total 2024 revenue reported in the attached sources?"
        style={textAreaStyle}
      />
      {objective.trim().length === 0 ? (
        <p
          data-testid="objective-empty-note"
          style={{ marginTop: 6, fontSize: 12, color: "#666" }}
        >
          Enter a request before creating the task.
        </p>
      ) : null}
    </section>
  );
}
