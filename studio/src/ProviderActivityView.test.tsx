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
