/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import fixture from "../../tests/fixtures/activity-codex-mcp-v1.json";
import { ProviderActivityView } from "./ProviderActivityView";
import { activityRecord } from "./ProviderActivityData";
import { ToolEvidence } from "./ExecutionPresentation";

it("renders actual finite-client MCP observations without promoting provider claims to native verification", () => {
  for (const record of fixture.records) {
    const html = renderToStaticMarkup(
      <ProviderActivityView event={record.event} runId={fixture.run_id} />,
    );
    expect(html).toContain("Provider tool activity");
    expect(html).toContain("<strong>Run configured project check</strong>");
    expect(html).toContain("<summary>Tool identity</summary>");
    expect(html).toContain("forge_coding");
    expect(html).toContain("run_configured_check");
    expect(html).not.toContain("without a supported display");
    expect(html).toContain("Read full recorded event");
    expect(html).toContain(
      "Native runner receipts and final verification are tracked separately",
    );
    expect(html).not.toContain("Passed");
  }
  const completed = renderToStaticMarkup(
    <ProviderActivityView
      event={fixture.records[1].event}
      runId={fixture.run_id}
    />,
  );
  expect(completed).toContain("Provider reported completion");
  expect(completed).toContain("Reported exit 0");
  expect(completed).toContain("NATIVE_PREREQUISITE_SAFE_RESULT");
  expect(completed).toContain("Copy output preview");
  expect(completed).not.toContain("Copy full output");
  expect(completed).not.toContain("structured_content");
});

it("refuses malformed observed MCP fields at the typed UI boundary", () => {
  expect(
    activityRecord(fixture.records[1].event, fixture.run_id)?.summary
      ?.tool_status,
  ).toBe("completed");
  for (const delta of [
    { tool_status: "success" },
    { tool_server: [] },
    { tool_exit_code: true },
    { provider: "unknown" },
    { tool_status: ["completed"] },
    { tool_status: "in_progress" },
  ]) {
    const event = {
      ...fixture.records[1].event,
      activity_summary: {
        ...fixture.records[1].event.activity_summary,
        ...delta,
      },
    };
    expect(() => activityRecord(event, fixture.run_id)).toThrow();
  }
});

it("preserves strict provider discriminators for existing non-MCP summaries", () => {
  const original = fixture.records[0].event;
  expect(() =>
    activityRecord(
      {
        ...original,
        activity_summary: {
          version: 1,
          event_type: "item.completed",
          item_type: "agent_message",
          provider: ["claude"],
          preview_omitted: false,
          sanitized_message_bytes:
            original.activity_content.sanitized_message_bytes,
          sanitized_message_sha256:
            original.activity_content.sanitized_message_sha256,
        },
      },
      fixture.run_id,
    ),
  ).toThrow();
});

it("keeps provider errors and hostile bounded output separate from native success", () => {
  const original = fixture.records[1].event;
  const { tool_exit_code: _, ...summary } = original.activity_summary;
  void _;
  const html = renderToStaticMarkup(
    <ProviderActivityView
      runId={fixture.run_id}
      event={{
        ...original,
        activity_summary: {
          ...summary,
          tool_status: "failed",
          output_preview: "<script>untrusted()</script>",
        },
      }}
    />,
  );
  expect(html).toContain("Provider reported a failure");
  expect(html).toContain("&lt;script&gt;");
  expect(html).not.toContain("<script>");
  expect(html).not.toContain("Reported exit");
  expect(html).not.toContain("Succeeded receipt");
});

it("keeps unknown tool identities exact without borrowing the configured-check title", () => {
  const original = fixture.records[0].event;
  const html = renderToStaticMarkup(
    <ProviderActivityView
      runId={fixture.run_id}
      event={{
        ...original,
        activity_summary: {
          ...original.activity_summary,
          tool_server: "other_server",
          tool_name: "run_configured_check",
        },
      }}
    />,
  );
  expect(html).toContain(
    "<strong>other_server · run_configured_check</strong>",
  );
  expect(html).not.toContain("Run configured project check");
});

it("labels the native configured-check tool in a controlled WAL presentation fixture", () => {
  // Controlled envelope, not a retained native C5 WAL receipt.
  const html = renderToStaticMarkup(
    <ToolEvidence
      kind="tool_intent"
      value={{
        tool: "coding_configured_check",
        arguments: { command_index: 0 },
      }}
    />,
  );
  expect(html).toContain("Run configured project check");
  expect(html).toContain("Tool request recorded");
  expect(html).not.toContain("Succeeded");
});
