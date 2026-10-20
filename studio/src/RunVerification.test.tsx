/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ForgeRun } from "./api";
import { VerificationResults } from "./RunVerification";
const id = "a".repeat(32),
  request = "technical-evidence";
const run: ForgeRun = {
  id: "b".repeat(32),
  repository_id: "c".repeat(32),
  brief: "Fictional CRM",
  status: "failed",
  verification: [],
  verification_handoff: {
    version: 1,
    id,
    request_id: request,
    status: "completed",
    continuation: 1,
    max_continuations: 1,
    commands_count: 7,
    completed_commands: 7,
    failed_commands: 5,
  },
};
const journal = {
  valid: true,
  entries: Array.from({ length: 7 }, (_, index) => {
    const execution = String(index + 1).repeat(32),
      code = index < 5 ? 1 : 0;
    return [
      {
        task_id: "implement",
        kind: "verification_handoff_command",
        payload: {
          version: 1,
          id,
          request_id: request,
          command_index: index + 1,
          ordinal: index + 1,
          execution_id: execution,
          exit_code: code,
          outcome: "completed",
        },
      },
      {
        task_id: "implement",
        kind: "sandbox_intent",
        payload: {
          execution_id: execution,
          effect: "start",
          request: {
            ordinal: index + 1,
            request: { argv: ["check", String(index + 1)] },
          },
        },
      },
      {
        task_id: "implement",
        kind: "sandbox_receipt",
        payload: {
          execution_id: execution,
          effect: "start",
          evidence: {
            exit_code: code,
            output_excerpt: `Observed check ${index + 1}`,
          },
        },
      },
    ];
  }).flat(),
};
it("shows all seven factory check receipts separately from absent final acceptance", () => {
  const html = renderToStaticMarkup(
    <VerificationResults run={run} journal={journal} />,
  );
  expect(html).toContain("Factory verification checks");
  expect(html).toContain("5 check commands failed");
  expect(html.match(/class="record-outcome failed"/g)).toHaveLength(5);
  expect(html).toContain("Observed check 7");
  expect(html).toContain("No final run verification results recorded");
  expect(html).not.toContain("Add verification commands");
});
it("does not borrow foreign or unverified journal command output", () => {
  const foreign = structuredClone(journal);
  foreign.entries[2].payload.execution_id = "f".repeat(32);
  const html = renderToStaticMarkup(
    <VerificationResults run={run} journal={foreign} />,
  );
  expect(html).not.toContain("Observed check 1");
  expect(html).toContain("Observed check 2");
  const invalid = renderToStaticMarkup(
    <VerificationResults run={run} journal={{ ...journal, valid: false }} />,
  );
  expect(invalid).not.toContain("Observed check 2");
  expect(invalid).toContain("Command receipts are unavailable");
});
it("retains ordinary final results without relabeling them factory handoff checks", () => {
  const html = renderToStaticMarkup(
    <VerificationResults
      run={{
        ...run,
        verification_handoff: undefined,
        verification: [
          {
            argv: ["final-check"],
            exit_code: 0,
            output: "Final source checked",
          },
        ],
      }}
    />,
  );
  expect(html).toContain("Final run checks");
  expect(html).toContain("Final source checked");
  expect(html).not.toContain("Factory verification checks");
});
