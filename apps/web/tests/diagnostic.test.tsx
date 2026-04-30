import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

function StaticDiagnostic({ db, redis, storage }: { db: string; redis: string; storage: string }) {
  return (
    <table>
      <tbody>
        {([["db", db], ["redis", redis], ["storage", storage]] as const).map(([k, v]) => (
          <tr key={k}>
            <td>{k}</td>
            <td aria-label={`${k} ${v === "ok" ? "ok" : "not ok"}`}>{v === "ok" ? "OK" : v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

describe("Diagnostic table (smoke)", () => {
  it("renders OK for all three components", () => {
    render(<StaticDiagnostic db="ok" redis="ok" storage="ok" />);
    expect(screen.getByLabelText("db ok")).toHaveTextContent("OK");
    expect(screen.getByLabelText("redis ok")).toHaveTextContent("OK");
    expect(screen.getByLabelText("storage ok")).toHaveTextContent("OK");
  });

  it("renders the literal status when not ok", () => {
    render(<StaticDiagnostic db="ok" redis="fail" storage="ok" />);
    expect(screen.getByLabelText("redis not ok")).toHaveTextContent("fail");
  });
});
