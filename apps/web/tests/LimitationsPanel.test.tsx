import * as React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import LimitationsPanel from "../components/LimitationsPanel";
import { standardLimitations } from "./fixtures/reportFixtures";

describe("LimitationsPanel", () => {
  it("renders every limitation verbatim", () => {
    const lines = standardLimitations();
    render(<LimitationsPanel limitations={lines} />);
    for (const line of lines) {
      expect(screen.getByText(line)).toBeInTheDocument();
    }
  });

  it("renders the fallback message when the list is empty", () => {
    render(<LimitationsPanel limitations={[]} />);
    expect(
      screen.getByText(
        /No limitations were returned by the report\. Treat this as unexpected\./
      )
    ).toBeInTheDocument();
  });

  it("renders the section heading even when empty", () => {
    render(<LimitationsPanel limitations={[]} />);
    expect(
      screen.getByRole("heading", { name: "Limitations" })
    ).toBeInTheDocument();
  });

  it("preserves Italian disclaimer wording verbatim", () => {
    // The backend emits the disclaimers in Italian; the panel must not
    // translate or paraphrase.
    const italian = ["Una fonte citata non implica un claim vero."];
    render(<LimitationsPanel limitations={italian} />);
    expect(
      screen.getByText("Una fonte citata non implica un claim vero.")
    ).toBeInTheDocument();
  });
});
