/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { EvidenceView } from "./ExecutionPresentation";
import { verificationInProgress } from "./VerificationHandoff";
import { ContinueRun } from "./ExecutionRecord";
import type { ForgeRun } from "./api";

const handoff = {
  version: 1,
  id: "a".repeat(32),
  request_id: "verification-needed",
  status: "pending",
  continuation: 1,
  max_continuations: 1,
  commands_count: 7,
  completed_commands: 0,
  failed_commands: 0,
  source_digest: "1".repeat(64),
  commands_digest: "2".repeat(64),
  policy_digest: "3".repeat(64),
  provider_digest: "4".repeat(64),
};
const render = (kind: string, value: unknown) =>
  renderToStaticMarkup(<EvidenceView kind={kind} value={value} />);
const command = {
  version: 1,
  id: handoff.id,
  request_id: handoff.request_id,
  execution_id: "b".repeat(32),
};

describe("automatic verification evidence", () => {
  it("explains a factory-owned check request without asking a person to run it", () => {
    const html = render("verification_handoff_intent", handoff);
    expect(html).toContain("Automatic verification requested");
    expect(html).toContain("approved check commands");
    expect(html).toContain("verification-needed");
    expect(html).not.toContain("Run these commands yourself");
  });

  it("keeps a failed check failed despite a contradictory outcome label", () => {
    const html = render("verification_handoff_command", {
      ...command,
      ordinal: 1,
      command_index: 1,
      exit_code: 2,
      outcome: "succeeded",
    });
    expect(html).toContain("Check command 1");
    expect(html).toContain("Failed (exit 2)");
    expect(html).not.toContain("Passed");
  });

  it("distinguishes an unknown command outcome from a passed result", () => {
    const html = render("verification_handoff_command", {
      ...command,
      ordinal: 1,
      command_index: 1,
      exit_code: null,
      outcome: "unknown",
    });
    expect(html).toContain("Result not confirmed");
    expect(html).not.toContain("Passed");
  });

  it("reports recorded failures without presenting the product as accepted", () => {
    const html = render("verification_handoff_outcome", {
      ...handoff,
      status: "completed",
      completed_commands: 7,
      failed_commands: 2,
    });
    expect(html).toContain("Automatic verification results");
    expect(html).toContain("2 check commands failed");
    expect(html).toContain("Product acceptance remains separate");
    expect(html).not.toContain("Product accepted");
  });

  it("holds incomplete outcomes without claiming a continuation", () => {
    const html = render("verification_handoff_outcome", {
      ...handoff,
      status: "held",
      completed_commands: 2,
    });
    expect(html).toContain("Review needed");
    expect(html).toContain("before forge can continue");
    expect(html).not.toContain("Development resumed");
  });

  it("refuses inconsistent counters and never exposes unrelated raw payloads", () => {
    const html = render("verification_handoff_outcome", {
      ...handoff,
      completed_commands: 8,
      argv: ["do-not-render-command"],
      output: "do-not-render-private-output",
    });
    expect(html).toContain("Verification evidence is incomplete");
    expect(html).not.toContain("do-not-render");
  });

  it("does not treat an unrecognized record as authority to hide manual controls", () => {
    expect(verificationInProgress(undefined)).toBe(false);
    expect(verificationInProgress(handoff)).toBe(true);
    expect(verificationInProgress({ ...handoff, id: "unrecognized" })).toBe(
      false,
    );
    expect(verificationInProgress({ ...handoff, max_continuations: 4 })).toBe(
      false,
    );
    expect(verificationInProgress({ ...handoff, status: "held" })).toBe(false);
  });

  it("withholds a command result from an unsupported evidence version", () => {
    const html = render("verification_handoff_command", {
      ...command,
      version: 2,
      ordinal: 1,
      command_index: 1,
      exit_code: 0,
      outcome: "completed",
    });
    expect(html).toContain("Verification evidence is incomplete");
    expect(html).not.toContain("Passed");
  });

  it("explains an admission refusal without inventing a command execution", () => {
    const html = render("verification_handoff_refused", {
      version: 1,
      request_id: "verification-needed",
      request_digest: "a".repeat(64),
      status: "held",
      reason: "admission_refused",
      message: "do-not-echo-unknown-details",
    });
    expect(html).toContain("Automatic verification needs review");
    expect(html).toContain("checks were not started");
    expect(html).not.toContain("do-not-echo");
  });
});

describe("verification admission and continuation records", () => {
  it("explains explicit legacy adoption without exposing policy or identity payloads", () => {
    const html = render("verification_handoff_adoption", {
      version: 1,
      request: {
        request_id: "a".repeat(32),
        agent: "codex",
        pending_request_digest: "b".repeat(64),
        source_digest: "c".repeat(64),
        commands_digest: "d".repeat(64),
        policy_digest: "e".repeat(64),
        provider_digest: "f".repeat(64),
      },
      body_digest: "1".repeat(64),
      policy_snapshot: { private_extra: "do-not-render-policy" },
      actor: { private_extra: "do-not-render-identity" },
    });
    expect(html).toContain("Verification adoption recorded");
    expect(html).toContain("Codex");
    expect(html).not.toContain("do-not-render");
    expect(html).not.toContain("Checks passed");
  });

  it("distinguishes a continuation intent from actual provider execution", () => {
    const html = render("verification_continuation_intent", {
      ...command,
      parent_round: 1,
      next_round: 2,
      provider_digest: "a".repeat(64),
      private_extra: "do-not-render-provider-configuration",
    });
    expect(html).toContain("Coding continuation recorded");
    expect(html).toContain("Coding attempt 2");
    expect(html).toContain("provider execution has its own receipt");
    expect(html).not.toContain("do-not-render");
  });
});

describe("automatic verification continuation controls", () => {
  const run = {
    id: "c".repeat(32),
    repository_id: "d".repeat(32),
    brief: "Fictional CRM",
    agent: "codex",
    status: "paused",
    pause: {
      kind: "worker_request",
      message: "Verification needed",
      automatic: false,
    },
    worker_request: {
      id: handoff.request_id,
      kind: "verification",
      status: "pending",
    },
    verification_handoff: handoff,
  };
  const view = (value: ForgeRun) =>
    renderToStaticMarkup(
      <ContinueRun
        run={value}
        connection={{ base: "http://127.0.0.1:8787/api/v1", token: "" }}
        onUpdated={() => {}}
      />,
    );

  it("prevents a competing manual resume while the same request is being handled", () => {
    expect(view(run)).not.toContain("Resume run");
    expect(view(run)).not.toContain("Hand off run");
  });

  it("keeps a recovering handoff owned by the factory until its result is known", () => {
    expect(
      view({
        ...run,
        status: "needs_reconciliation",
        verification_handoff: { ...handoff, status: "running" },
      }),
    ).not.toContain("Resume run");
  });

  it("keeps review controls when automatic handling is held", () => {
    expect(
      view({ ...run, verification_handoff: { ...handoff, status: "held" } }),
    ).toContain("Resume run");
  });

  it("never hides a new human question behind an earlier handoff", () => {
    expect(
      view({
        ...run,
        worker_request: {
          id: "product-choice",
          kind: "approval",
          status: "pending",
        },
      }),
    ).toContain("Resume run");
  });

  it("preserves review of an unknown provider result", () => {
    expect(
      view({
        ...run,
        agent: "bridge",
        bridge_completion_hold: { reason: "unknown" },
      }),
    ).toContain("Execution result needs review");
  });
});
