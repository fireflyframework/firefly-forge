/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EvidenceView, journalDisposition } from "./ExecutionPresentation";

const payload = {
  version: 1,
  monitor_id: "a".repeat(32),
  owner_epoch: "b".repeat(32),
  request: { argv: ["python", "server.py"], lease_seconds: 60 },
};
it("shows a process handoff as running rather than a completed command", () => {
  const entry = {
    kind: "runner_monitor_started",
    task_id: "implement",
    payload,
  };
  expect(journalDisposition(entry, [entry])).toBe(
    "Process started; cleanup pending",
  );
  const html = renderToStaticMarkup(
    <EvidenceView value={payload} kind={entry.kind} advanced={false} />,
  );
  expect(html).toContain("Process started");
  expect(html).toContain("server.py");
  expect(html).not.toContain("&quot;monitor_id&quot;:");
});
it("reports a nonzero exit without claiming task success", () => {
  const entry = {
    kind: "runner_monitor_terminal",
    task_id: "implement",
    payload: {
      ...payload,
      status: "exited",
      exit_code: 3,
      process_group_drained: true,
    },
  };
  expect(journalDisposition(entry, [entry])).toBe(
    "Exit observed; cleanup recorded",
  );
  const html = renderToStaticMarkup(
    <EvidenceView value={entry.payload} kind={entry.kind} advanced={false} />,
  );
  expect(html).toContain("Exit code");
  expect(html).toContain("3");
  expect(html).not.toContain("Succeeded");
});
it("keeps missing drain proof and malformed monitor identities explicit", () => {
  const entry = {
    kind: "runner_monitor_terminal",
    task_id: "implement",
    payload: { ...payload, status: "exited", exit_code: 0 },
  };
  expect(journalDisposition(entry, [entry])).toBe(
    "Cleanup not confirmed; reconciliation required",
  );
  const html = renderToStaticMarkup(
    <EvidenceView
      value={{ ...payload, monitor_id: "foreign" }}
      kind={entry.kind}
      advanced={false}
    />,
  );
  expect(html).toContain("Process evidence is incomplete");
});
