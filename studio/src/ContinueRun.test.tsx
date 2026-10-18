/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ContinueRun } from "./ExecutionRecord";
import type { ForgeRun } from "./api";

function render(status: string, kind: string) {
  return renderToStaticMarkup(
    <ContinueRun
      run={
        {
          id: "a".repeat(32),
          status,
          agent: "bridge",
          pause: {
            kind,
            message: "The original Bridge submission has no confirmed outcome.",
            automatic: false,
          },
        } as ForgeRun
      }
      connection={{ base: "https://forge.invalid", token: "fictional" }}
      onUpdated={() => undefined}
    />,
  );
}

describe("retained run continuation", () => {
  it.each([
    "needs_reconciliation",
    "paused",
    "cancelled",
    "failed",
    "interrupted",
  ])(
    "does not offer resume or handoff for an unknown Bridge outcome in %s",
    (status) => {
      const html = render(status, "provider_outcome_unknown");
      expect(html).toContain("Execution result needs review");
      expect(html).toContain("original request");
      expect(html).not.toContain("Resume run");
      expect(html).not.toContain("Hand off run");
      expect(html).not.toContain("<button");
    },
  );
  it("keeps explicit continuation available for an ordinary operator pause", () => {
    const html = render("paused", "quota");
    expect(html).toContain("Resume run");
    expect(html).toContain("Hand off run");
  });
});

it("points a held checkpoint to its original result flow and suppresses generic continuation", () => {
  const html = renderToStaticMarkup(
    <ContinueRun
      run={{
        id: "a".repeat(32),
        repository_id: "b".repeat(32),
        brief: "",
        status: "needs_reconciliation",
        agent: "bridge",
        bridge_completion_hold: { reason: "graph_checkpoint_unavailable" },
      }}
      connection={{ base: "https://forge.invalid", token: "fictional" }}
      onUpdated={() => undefined}
    />,
  );
  expect(html).toContain("Original model result");
  expect(html).not.toContain("Resume run");
  expect(html).not.toContain("Hand off run");
});
