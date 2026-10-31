/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it, vi } from "vitest";
import {
  consumedTerminalRequest,
  parseReconciliationCandidate,
  parseReconciliationReceipt,
  readReconciliationMarker,
  reconciliationKey,
  sendReconciliationOnce,
  type ReconciliationContext,
} from "./ConsumedTerminalData";

const h = "a".repeat(64);
const context: ReconciliationContext = {
  base: "https://factory.example/api/v1",
  actor: "1".repeat(32),
  authentication: "oidc",
  project: "2".repeat(32),
  repository: "3".repeat(32),
  run: "4".repeat(32),
  request: "5".repeat(32),
  recovery: h,
  claim: h,
  attempt: 3,
};
const candidate = {
  version: 1,
  run_id: context.run,
  request_id: context.request,
  same_task_attempt: 3,
  ...Object.fromEntries(
    [
      "owner_digest",
      "actor_digest",
      "policy_digest",
      "runtime_contract",
      "profile_digest",
      "run_digest",
      "task_digest",
      "journal_digest",
      "recovery_digest",
      "claim_digest",
      "source_digest",
      "index_digest",
      "digest",
    ].map((key) => [key, h]),
  ),
};
const terminalCandidate = {
  version: 2,
  run_id: context.run,
  actor_digest: h,
  policy_digest: h,
  run_digest: h,
  journal_digest: h,
  digest: h,
  proof: {
    version: 2,
    run_id: context.run,
    task_id: "implement",
    task_attempt: 3,
    task_key: "7".repeat(32),
    completion_count: 1,
    completions: [
      {
        request_id: "fbr1_" + "8".repeat(32),
        intent_digest: h,
        checkpoint_record_digest: h,
        response_record_digest: h,
        consume_record_digest: h,
      },
    ],
    verification: "consumed_terminal_output_known_only",
    ...Object.fromEntries(
      [
        "terminal_report_sha256",
        "final_response_sha256",
        "recorded_runtime_contract",
        "current_runtime_contract",
        "current_profile_digest",
        "source_digest",
        "parent_final_digest",
        "handoff_custody_digest",
        "usage_before_digest",
        "usage_after_digest",
        "digest",
      ].map((key) => [key, h]),
    ),
  },
};
const terminalInput = {
  version: 2,
  candidate_digest: h,
  policy_digest: h,
  expected_run_digest: h,
  journal_digest: h,
  proof_digest: h,
  acknowledge_known_worker_output: true,
  acknowledge_no_execution_grant: true,
  acknowledge_local_postprocessing: true,
};
it("recognizes only the original claimed request in a stopped run", () => {
  const run = {
    status: "paused",
    consumed_terminal_recovery: {
      state: "claimed",
      request_id: context.request,
    },
  };
  expect(consumedTerminalRequest(run)).toBe(context.request);
  expect(consumedTerminalRequest({ ...run, status: "running" })).toBeNull();
  expect(
    consumedTerminalRequest({
      ...run,
      consumed_terminal_recovery: {
        state: "queued",
        request_id: context.request,
      },
    }),
  ).toBeNull();
});
it("rejects a foreign run, request, stale attempt, claim or recovery witness", () => {
  expect(
    parseReconciliationCandidate(candidate, context).same_task_attempt,
  ).toBe(3);
  for (const altered of [
    { run_id: "6".repeat(32) },
    { request_id: "6".repeat(32) },
    { same_task_attempt: 4 },
    { claim_digest: "b".repeat(64) },
    { recovery_digest: "b".repeat(64) },
    { source_digest: "not-a-hash" },
    { raw_report: "must never be displayed" },
  ])
    expect(() =>
      parseReconciliationCandidate({ ...candidate, ...altered }, context),
    ).toThrow();
});
it("scopes durable markers to the factory, actor, authentication and original action", () => {
  const marker = {
    version: 1 as const,
    context,
    candidate_digest: h,
    actor_digest: h,
  };
  expect(readReconciliationMarker(JSON.stringify(marker), context)).toEqual(
    marker,
  );
  for (const altered of [
    { actor: "9".repeat(32) },
    { base: "https://other.example/api/v1" },
    { authentication: "local_bootstrap" },
    { request: "9".repeat(32) },
  ]) {
    const foreign = { ...context, ...altered };
    expect(reconciliationKey(foreign)).not.toBe(reconciliationKey(context));
    expect(() =>
      readReconciliationMarker(JSON.stringify(marker), foreign),
    ).toThrow();
  }
});
it("persists the original action before POST and never resubmits after network loss", async () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
  const marker = {
    version: 1 as const,
    context,
    candidate_digest: h,
    actor_digest: h,
  };
  const post = vi.fn(async () => {
    expect(values.size).toBe(1);
    throw Error("connection lost");
  });
  await expect(sendReconciliationOnce(storage, marker, post)).rejects.toThrow(
    "connection lost",
  );
  await expect(sendReconciliationOnce(storage, marker, post)).rejects.toThrow();
  expect(post).toHaveBeenCalledTimes(1);
});
it("does not POST when durable browser custody is unavailable", async () => {
  const post = vi.fn();
  const storage = {
    getItem: () => null,
    setItem: () => {
      throw Error("storage blocked");
    },
  };
  await expect(
    sendReconciliationOnce(
      storage,
      { version: 1, context, candidate_digest: h, actor_digest: h },
      post,
    ),
  ).rejects.toThrow();
  expect(post).not.toHaveBeenCalled();
});
it("rejects a foreign operator receipt, a changed original action and any execution grant", () => {
  const marker = {
    version: 1 as const,
    context,
    candidate_digest: h,
    actor_digest: h,
  };
  const receipt = {
    version: 1,
    candidate,
    request: {
      version: 1,
      candidate_digest: h,
      acknowledge_same_claimed_action: true,
      acknowledge_no_execution_grant: true,
      acknowledge_local_postprocessing: true,
    },
    record_digest: h,
    settlement: {
      version: 2,
      kind: "consumed_terminal_recovery",
      run_id: context.run,
      request_id: context.request,
      record_digest: h,
      candidate: terminalCandidate,
      request: terminalInput,
      state: "claimed",
      run_status: "paused",
      same_task_attempt: 3,
      no_execution_grant: true,
    },
  };
  expect(
    parseReconciliationReceipt(receipt, context, marker).settlement.state,
  ).toBe("claimed");
  expect(() =>
    parseReconciliationReceipt(
      { ...receipt, candidate: { ...candidate, actor_digest: "b".repeat(64) } },
      context,
      marker,
    ),
  ).toThrow();
  for (const altered of [
    { request_id: "9".repeat(32) },
    { record_digest: "b".repeat(64) },
    { no_execution_grant: false },
  ])
    expect(() =>
      parseReconciliationReceipt(
        { ...receipt, settlement: { ...receipt.settlement, ...altered } },
        context,
        marker,
      ),
    ).toThrow();
  for (const altered of [
    { candidate: { ...terminalCandidate, run_id: "9".repeat(32) } },
    { candidate: { ...terminalCandidate, version: 1 } },
    {
      candidate: {
        ...terminalCandidate,
        proof: { ...terminalCandidate.proof, run_id: "9".repeat(32) },
      },
    },
    {
      candidate: {
        ...terminalCandidate,
        proof: { ...terminalCandidate.proof, task_attempt: 4 },
      },
    },
    { request: { ...terminalInput, candidate_digest: "b".repeat(64) } },
    { request: { ...terminalInput, proof_digest: "b".repeat(64) } },
    { request: { ...terminalInput, acknowledge_no_execution_grant: false } },
  ])
    expect(() =>
      parseReconciliationReceipt(
        { ...receipt, settlement: { ...receipt.settlement, ...altered } },
        context,
        marker,
      ),
    ).toThrow();
});
