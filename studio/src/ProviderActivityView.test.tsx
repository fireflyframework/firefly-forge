/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProviderActivityView } from "./ProviderActivityView";
const run = "1".repeat(32),
  eventId = "2".repeat(32);
function event() {
  return {
    id: eventId,
    stage: "stdout",
    message:
      '{"type":"item.completed","item":{"aggregated_output":"' +
      "x".repeat(1000),
    activity_content: {
      version: 1,
      status: "available",
      run_id: run,
      event_id: eventId,
      stage: "stdout",
      encoding: "utf-8",
      sanitized_message_bytes: 90000,
      sanitized_message_sha256: "a".repeat(64),
      descriptor_sha256: "b".repeat(64),
    },
    activity_summary: {
      version: 1,
      event_type: "item.completed",
      item_type: "command_execution",
      command_preview: "python -m pytest",
      output_preview: "<img src=x>\nRecorded output",
      exit_code: 0,
      preview_omitted: true,
      sanitized_message_bytes: 90000,
      sanitized_message_sha256: "a".repeat(64),
    },
  };
}
const props = {
  runId: run,
  actorId: "3".repeat(32),
  connection: { base: "/api/v1", token: "fixture-only-token" },
};
it("presents the complete-event projection when the legacy message is incomplete JSON", () => {
  const html = renderToStaticMarkup(
    <ProviderActivityView {...props} event={event()} />,
  );
  expect(html).toContain("Provider command finished");
  expect(html).toContain("Exit 0");
  expect(html).toContain("python -m pytest");
  expect(html).toContain("&lt;img");
  expect(html).not.toContain("<img");
  expect(html).not.toContain("aggregated_output");
  expect(html).toContain("Read full recorded event");
  expect(html).toContain("Factory verification is tracked separately");
  expect(html).toContain("Preview");
});
it("does not turn an unobserved exit into success", () => {
  const row = event();
  const { exit_code: _, ...summary } = row.activity_summary;
  void _;
  const html = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{ ...row, activity_summary: summary }}
    />,
  );
  expect(html).toContain("Outcome not recorded");
  expect(html).not.toContain(">Passed<");
});
it("shows a capture-limit refusal without offering a nonexistent full payload", () => {
  const row = event();
  const html = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{
        ...row,
        activity_content: {
          ...row.activity_content,
          status: "budget_exceeded",
        },
      }}
    />,
  );
  expect(html).toContain("archive limit");
  expect(html).not.toContain("Read full recorded event");
});
it("does not render a projection bound to another run or event", () => {
  const row = event();
  const html = renderToStaticMarkup(
    <ProviderActivityView {...props} event={{ ...row, id: "4".repeat(32) }} />,
  );
  expect(html).toContain("Recorded event unavailable");
  expect(html).not.toContain("Exit 0");
  expect(html).not.toContain("Read full recorded event");
});

it("labels a Claude tool call as a request without claiming it started or passed", () => {
  const row = event();
  const html = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{
        ...row,
        activity_summary: {
          ...row.activity_summary,
          provider: "claude",
          event_type: "claude.assistant",
          item_type: "tool_use",
          tool_name: "Bash",
          item_id: "tool-1",
          exit_code: undefined,
        },
      }}
    />,
  );
  expect(html).toContain("Tool request recorded");
  expect(html).toContain("Bash");
  expect(html).toContain("python -m pytest");
  expect(html).not.toContain("command started");
  expect(html).not.toContain("Exit 0");
  expect(html).toContain("not an execution receipt");
});
it("presents Claude tool errors and provider results without inferring an exit or factory approval", () => {
  const row = event();
  const summary = {
    ...row.activity_summary,
    provider: "claude",
    exit_code: undefined,
    command_preview: undefined,
  };
  const tool = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{
        ...row,
        activity_summary: {
          ...summary,
          event_type: "claude.user",
          item_type: "tool_result",
          tool_outcome: "reported_error",
        },
      }}
    />,
  );
  expect(tool).toContain("Tool result reported");
  expect(tool).toContain("Provider reported an error");
  expect(tool).not.toContain("Exit 0");
  const report = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{
        ...row,
        activity_summary: {
          ...summary,
          event_type: "claude.result",
          item_type: "agent_message",
        },
      }}
    />,
  );
  expect(report).toContain("Provider result reported");
  expect(report).not.toContain("Task completed");
  expect(report).toContain("Factory verification is tracked separately");
});
it.each(["available", "budget_exceeded"] as const)(
  "renders mixed Claude omissions honestly when the archive is %s",
  (status) => {
    const row = event();
    const html = renderToStaticMarkup(
      <ProviderActivityView
        {...props}
        event={{
          ...row,
          activity_content: { ...row.activity_content, status },
          activity_summary: {
            ...row.activity_summary,
            provider: "claude",
            event_type: "claude.assistant",
            item_type: "provider_message",
            content_blocks: [
              {
                item_type: "agent_message",
                output_preview: "Reviewing contacts",
              },
              { item_type: "tool_use", item_id: "tool-1", tool_name: "Read" },
            ],
            omitted_blocks: 3,
            exit_code: undefined,
            command_preview: undefined,
            output_preview: undefined,
          },
        }}
      />,
    );
    expect(html).toContain("Reviewing contacts");
    expect(html).toContain("Read");
    expect(html).toContain("3 additional content blocks");
    if (status === "budget_exceeded") {
      expect(html).not.toContain("available in the full recorded event");
      expect(html).toContain("omitted from this preview");
    }
  },
);
it("keeps unknown and malformed structured previews out of the default card while preserving plain logs", () => {
  const row = event();
  const { activity_summary: _, ...unknown } = row;
  void _;
  for (const message of [
    '{"new_protocol":"OPAQUE_JSON"}',
    '{"incomplete":"OPAQUE_JSON',
    '[{"x":"OPAQUE_JSON"}]',
  ]) {
    const html = renderToStaticMarkup(
      <ProviderActivityView {...props} event={{ ...unknown, message }} />,
    );
    expect(html).toContain("Structured provider output recorded");
    expect(html).not.toContain("OPAQUE_JSON");
    expect(html).toContain("Read full recorded event");
  }
  const html = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{ ...unknown, message: "[INFO] Loading source" }}
    />,
  );
  expect(html).toContain("[INFO] Loading source");
});

it("does not direct readers to unavailable full content after an archive refusal", () => {
  const row = event();
  const { activity_summary: _, ...unknown } = row;
  void _;
  const html = renderToStaticMarkup(
    <ProviderActivityView
      {...props}
      event={{
        ...unknown,
        message: '{"unknown":"structured"}',
        activity_content: {
          ...row.activity_content,
          status: "budget_exceeded",
        },
      }}
    />,
  );
  expect(html).toContain("full event was not recorded");
  expect(html).not.toContain("Open the full recorded event");
});
