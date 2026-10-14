/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ToolEvidence } from "./ExecutionPresentation";

describe("broker tool evidence", () => {
  it("keeps the exact machine identity in a collapsed disclosure with full copy", () => {
    const tool = "external_mcp:github:get_file_contents";
    const html = renderToStaticMarkup(
      <ToolEvidence kind="tool_intent" value={{ tool }} />,
    );
    expect(html).toContain("<summary>Tool identity</summary>");
    expect(html).toContain('class="record-tool-identity-text"');
    expect(html).toContain("Copy tool identity");
    expect(html.split("Tool identity")[0]).not.toContain(tool);
  });
  it("presents native intent arguments and source while keeping frozen authority in Advanced", () => {
    const payload = Object.freeze({
      tool: "external_mcp:github:get_file_contents",
      correlation_id: "1".repeat(32),
      arguments: {
        connector: "github",
        tool: "get_file_contents",
        arguments: { owner: "team", repo: "product", path: "src/check.py" },
      },
      source: { head: "a".repeat(40), source_tree: "b".repeat(40) },
      configuration: { private_extension: "frozen-authority-only" },
    });
    const html = renderToStaticMarkup(
      <ToolEvidence kind="tool_intent" value={payload} />,
    );
    expect(html).toContain("Tool request recorded");
    expect(html).toContain("GitHub · Read repository files");
    expect(html).toContain("src/check.py");
    expect(html).toContain("Source tree");
    expect(html.split("Advanced record")[0]).not.toContain(
      "frozen-authority-only",
    );
    expect(html).toContain("frozen-authority-only");
    expect(payload.configuration.private_extension).toBe(
      "frozen-authority-only",
    );
  });
  it("bounds actual remote text and hostile tool headings, preserving originals in copy and Advanced", () => {
    const payload = {
      tool: "<img src=x onerror=alert(1)>" + "x".repeat(200000),
      correlation_id: "1".repeat(32),
      status: "succeeded",
      result: {
        untrusted: true,
        text: ["<script>attack()</script>" + "x".repeat(200000)],
      },
    };
    const html = renderToStaticMarkup(
      <ToolEvidence kind="tool_receipt" value={payload} />,
    );
    expect(html.length).toBeLessThan(50000);
    expect(html).toContain("Succeeded receipt recorded");
    expect(html).toContain("Untrusted provider output");
    expect(html).toContain("Copy full output");
    expect(html).toContain("more characters");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script");
    expect(payload.result.text[0].length).toBeGreaterThan(200000);
  });
  it("keeps quota rejection, unknown outcome and explicit abandonment distinct", () => {
    const rejected = renderToStaticMarkup(
      <ToolEvidence
        kind="tool_receipt"
        value={{
          tool: "internet_search",
          status: "rejected",
          error: {
            kind: "quota",
            status_code: 402,
            message: "Provider quota exhausted",
          },
        }}
      />,
    );
    expect(rejected).toContain("Rejected receipt recorded");
    expect(rejected).toContain("quota");
    expect(rejected).not.toContain("Succeeded");
    const unknown = renderToStaticMarkup(
      <ToolEvidence
        kind="tool_unknown"
        value={{
          status: "unknown",
          error: { message: "Tool outcome was not admitted" },
        }}
      />,
    );
    expect(unknown).toContain("Outcome unknown");
    const abandoned = renderToStaticMarkup(
      <ToolEvidence
        kind="tool_reconciled"
        value={{
          correlation_id: "1".repeat(32),
          decision: "abandoned_without_replay",
          actor: { actor_id: "operator-7" },
        }}
      />,
    );
    expect(abandoned).toContain("Abandoned without replay");
    expect(abandoned).toContain("operator-7");
    expect(abandoned).not.toContain("Succeeded");
  });
});
