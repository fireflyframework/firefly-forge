/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  finalCandidate,
  finalInput,
  finalStatus,
  finalMarkerKey,
  readFinalMarker,
  sendFinalOnce,
} from "./FinalOutcomeData";
import type { RecoveryContext } from "./InvalidReadRecoveryData";
const hash = "a".repeat(64);
const context: RecoveryContext = {
  base: "https://factory.example/api/v1/",
  actor: "1".repeat(32),
  authentication: "local_bootstrap",
  project: "2".repeat(32),
  repository: "3".repeat(32),
  run: "4".repeat(32),
  request: "5".repeat(32),
};
export function finalFixture() {
  return {
    version: 1,
    kind: "final_outcome_continuation_candidate",
    run_id: context.run,
    request_id: context.request,
    admission: "READY",
    holds: [],
    execution_authorized: false,
    original_admission_seal: hash,
    attempt: 2,
    ...Object.fromEntries(
      [
        "ticket_digest",
        "events_digest",
        "journal_digest",
        "run_digest",
        "task_digest",
        "checkpoint_record_digest",
        "response_record_digest",
        "consume_record_digest",
        "report_digest",
        "final_outcome_digest",
        "source_digest",
        "current_runtime_contract",
        "policy_digest",
        "digest",
      ].map((name) => [name, hash]),
    ),
    current_profile: { id: "fictional-qualified" },
    maximum_window_seconds: 1800,
    admission_window_purpose: "claim_only",
    outcome: "blocked",
    followup_kind: "verification",
    usage: { requests: 17, tool_calls: 11 },
    limits: { request_limit: 100, tool_calls_limit: 300 },
  };
}
const receipt = () => ({
  version: 1,
  kind: "final_outcome_continuation_status",
  run_id: context.run,
  request_id: context.request,
  status: "completed",
  action_id: "6".repeat(32),
  witness_digest: hash,
  original_admission_seal: hash,
  lease: {},
  run_status: "paused",
});
it("retains blocked verification and distinguishes acceptance from run completion", () => {
  const candidate = finalCandidate(finalFixture(), context);
  expect(candidate.outcome).toBe("blocked");
  expect(candidate.followup_kind).toBe("verification");
  expect(finalStatus(receipt(), context).run_status).toBe("paused");
  expect(finalInput(candidate)).toEqual({
    version: 1,
    candidate_digest: hash,
    valid_for_seconds: 1800,
    acknowledge_already_consumed_final_output: true,
    acknowledge_no_completed_effect_replay: true,
    acknowledge_same_request_attempt_and_budgets: true,
    acknowledge_finite_ownership_lease: true,
  });
});
it.each([
  { version: true },
  { run_id: "7".repeat(32) },
  { request_id: "7".repeat(32) },
  { consume_record_digest: "unknown" },
  { report_digest: [] },
  { execution_authorized: true },
  { admission: "READY", holds: ["policy_disabled"] },
  { maximum_window_seconds: 0 },
  { admission_window_purpose: "job_duration" },
  { outcome: "success" },
  { current_profile: null },
  { usage: { requests: -1, tool_calls: 11 } },
  { extra: true },
])("refuses a changed or unsafe final-result record %j", (change) => {
  expect(() =>
    finalCandidate({ ...finalFixture(), ...change }, context),
  ).toThrow();
});
it("allows a readable hold without minting an action", () => {
  const candidate = finalCandidate(
    {
      ...finalFixture(),
      admission: "HOLD",
      holds: ["final_outcome_continuation_disabled"],
    },
    context,
  );
  expect(() => finalInput(candidate)).toThrow();
});
it("refuses mismatched action and original-admission identities", () => {
  expect(() => finalStatus(receipt(), context, "7".repeat(32), hash)).toThrow();
  expect(() =>
    finalStatus(receipt(), context, "6".repeat(32), "b".repeat(64)),
  ).toThrow();
  expect(() =>
    finalStatus({ ...receipt(), status: "not_recovered" }, context),
  ).toThrow();
});
it("persists one scoped action before dispatch and never retries after a lost response", async () => {
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
    seal: hash,
    body: finalInput(finalCandidate(finalFixture(), context)),
  };
  let sends = 0;
  const send = async () => {
    sends++;
    expect(storage.getItem(finalMarkerKey(context))).not.toBeNull();
    throw Error("Lost response");
  };
  await expect(sendFinalOnce(storage, marker, send)).rejects.toThrow(
    "Lost response",
  );
  expect(
    readFinalMarker(storage.getItem(finalMarkerKey(context)), context),
  ).toEqual(marker);
  await expect(sendFinalOnce(storage, marker, send)).rejects.toThrow();
  expect(sends).toBe(1);
  expect(() =>
    readFinalMarker(storage.getItem(finalMarkerKey(context)), {
      ...context,
      actor: "8".repeat(32),
    }),
  ).toThrow();
});
