/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  checkpointCandidate,
  checkpointInput,
  checkpointStatus,
  checkpointMarkerKey,
  readCheckpointMarker,
  sendCheckpointOnce,
} from "./CheckpointContinuationData";
import type { RecoveryContext } from "./InvalidReadRecoveryData";
const h = "a".repeat(64);
const context: RecoveryContext = {
  base: "https://factory.example/api/v1/",
  actor: "1".repeat(32),
  authentication: "local_bootstrap",
  project: "2".repeat(32),
  repository: "3".repeat(32),
  run: "4".repeat(32),
  request: "5".repeat(32),
};
const candidate = () => ({
  version: 1,
  kind: "checkpoint_continuation_candidate",
  run_id: context.run,
  request_id: context.request,
  digest: h,
  admission: "READY",
  holds: [],
  execution_authorized: false,
  original_admission_seal: h,
  attempt: 2,
  ticket_digest: h,
  events_digest: h,
  journal_digest: h,
  run_digest: h,
  task_digest: h,
  checkpoint_record_digest: h,
  response_record_digest: h,
  source_digest: h,
  current_runtime_contract: h,
  current_profile: { id: "fictional-qualified" },
  policy_digest: h,
  frozen_timeout_seconds: 600,
  maximum_window_seconds: 180,
  original_deadline_ms: 1793200000000,
  usage: {
    input_tokens: 0,
    cache_write_tokens: 0,
    cache_read_tokens: 0,
    output_tokens: 0,
    input_audio_tokens: 0,
    cache_audio_read_tokens: 0,
    output_audio_tokens: 0,
    audio_seconds: 0,
    details: {},
    cost: null,
    requests: 2,
    tool_calls: 2,
  },
  limits: {
    cost_limit: null,
    request_limit: 8,
    tool_calls_limit: 20,
    input_tokens_limit: null,
    output_tokens_limit: null,
    total_tokens_limit: null,
    per_request_input_tokens_limit: null,
    count_tokens_before_request: false,
  },
});
const status = (state = "executing") => ({
  version: 1,
  kind: "checkpoint_continuation_status",
  run_id: context.run,
  request_id: context.request,
  status: state,
  action_id: state === "not_recovered" ? null : "6".repeat(32),
  witness_digest: state === "not_recovered" ? null : h,
  original_admission_seal: h,
  lease: state === "not_recovered" ? null : {},
});
it("builds the exact acknowledged action from a reviewed time window", () => {
  const c = checkpointCandidate(candidate(), context, h);
  expect(checkpointInput(c, 60)).toEqual({
    version: 1,
    candidate_digest: h,
    valid_for_seconds: 60,
    acknowledge_retained_response_consumed_once: true,
    acknowledge_no_completed_effect_replay: true,
    acknowledge_same_request_attempt_and_budgets: true,
    acknowledge_finite_ownership_lease: true,
  });
  for (const seconds of [0, 181, NaN, 1.5])
    expect(() => checkpointInput(c, seconds)).toThrow();
});
it.each([
  { version: true },
  { request_id: "7".repeat(32) },
  { digest: [h] },
  { execution_authorized: true },
  { maximum_window_seconds: 601 },
  { holds: ["unknown"] },
  { admission: "READY", holds: ["checkpoint_continuation_disabled"] },
  { usage: { requests: -1, tool_calls: 2 } },
  { extra: true },
])("rejects an unsafe checkpoint candidate %j", (change) => {
  expect(() =>
    checkpointCandidate({ ...candidate(), ...change }, context),
  ).toThrow();
});
it("keeps policy holds readable but cannot build an admission from them", () => {
  const c = checkpointCandidate(
    {
      ...candidate(),
      admission: "HOLD",
      holds: ["checkpoint_continuation_disabled"],
    },
    context,
  );
  expect(c.holds).toEqual(["checkpoint_continuation_disabled"]);
  expect(() => checkpointInput(c, 60)).toThrow();
});
it.each(["queued", "executing", "completed", "failed", "needs_reconciliation"])(
  "reads saved %s only for the same action and admission",
  (state) => {
    expect(
      checkpointStatus(status(state), context, "6".repeat(32), h).status,
    ).toBe(state);
    expect(() =>
      checkpointStatus(status(state), context, "7".repeat(32), h),
    ).toThrow();
    expect(() =>
      checkpointStatus(status(state), context, undefined, "b".repeat(64)),
    ).toThrow();
  },
);
it("permits legacy discovery only from an exact absent generic action", () => {
  expect(checkpointStatus(status("not_recovered"), context).status).toBe(
    "not_recovered",
  );
  expect(() =>
    checkpointStatus(status("not_recovered"), context, "6".repeat(32)),
  ).toThrow();
  for (const change of [
    { action_id: "6".repeat(32) },
    { witness_digest: h },
    { lease: {} },
  ])
    expect(() =>
      checkpointStatus({ ...status("not_recovered"), ...change }, context),
    ).toThrow();
});
it.each([
  { status: ["queued"] },
  { action_id: ["6".repeat(32)] },
  { witness_digest: null },
  { request_id: "7".repeat(32) },
  { kind: "readonly_observation_recovery_status" },
  { lease: [] },
])("refuses malformed status %j", (change) => {
  expect(() => checkpointStatus({ ...status(), ...change }, context)).toThrow();
});
it("persists one identity before POST and refuses another submission after uncertainty", async () => {
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
    action: "6".repeat(32),
    seal: h,
    body: checkpointInput(checkpointCandidate(candidate(), context), 60),
  };
  let calls = 0;
  await expect(
    sendCheckpointOnce(storage, marker, async () => {
      calls++;
      expect(
        readCheckpointMarker(
          storage.getItem(checkpointMarkerKey(context)),
          context,
        ),
      ).toEqual(marker);
      throw Error("lost");
    }),
  ).rejects.toThrow("lost");
  await expect(
    sendCheckpointOnce(storage, marker, async () => {
      calls++;
    }),
  ).rejects.toThrow();
  expect(calls).toBe(1);
  expect(() =>
    readCheckpointMarker(storage.getItem(checkpointMarkerKey(context)), {
      ...context,
      actor: "8".repeat(32),
    }),
  ).toThrow();
  expect(() =>
    readCheckpointMarker(storage.getItem(checkpointMarkerKey(context)), {
      ...context,
      base: "https://other.example/api/v1/",
    }),
  ).toThrow();
});
it("refuses storage failure before submitting", async () => {
  const marker = {
    version: 1 as const,
    context,
    action: "6".repeat(32),
    seal: h,
    body: checkpointInput(checkpointCandidate(candidate(), context), 60),
  };
  let calls = 0;
  await expect(
    sendCheckpointOnce(
      {
        getItem: () => null,
        setItem: () => {
          throw Error("blocked");
        },
      },
      marker,
      async () => {
        calls++;
      },
    ),
  ).rejects.toThrow();
  expect(calls).toBe(0);
});
it.each([
  { current_profile: {} },
  { usage: { ...candidate().usage, cost: "0x10" } },
])("rejects incomplete qualification or nondecimal SDK cost %j", (change) => {
  expect(() =>
    checkpointCandidate({ ...candidate(), ...change }, context),
  ).toThrow();
});
