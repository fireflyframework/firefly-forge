/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  candidate,
  recoveryInput,
  recoveryStatus,
  markerKey,
  readMarker,
  sendOnce,
  type RecoveryContext,
} from "./InvalidReadRecoveryData";
const hash = "a".repeat(64),
  request = "b".repeat(32);
const context: RecoveryContext = {
  base: "https://factory.example/api/v1",
  actor: "1".repeat(32),
  authentication: "local_bootstrap",
  project: "2".repeat(32),
  repository: "3".repeat(32),
  run: "4".repeat(32),
  request,
};
export const fixture = () => ({
  version: 1,
  kind: "unclaimed_invalid_read_recovery_candidate",
  run_id: context.run,
  request_id: request,
  digest: hash,
  admission: "READY",
  holds: [],
  execution_authorized: false,
  original_admission_seal: hash,
  failed_event_digest: hash,
  original_settlement_digest: hash,
  original_claim_digest: hash,
  checkpoint_digest: hash,
  source_digest: hash,
  attempt: 2,
  current_profile: { id: "qualified" },
  current_runtime_contract: hash,
  policy_digest: hash,
  maximum_window_seconds: 300,
  frozen_timeout_seconds: 600,
  original_lease: {},
  usage: {
    requests: 1,
    request_limit: 8,
    tool_calls: 2,
    tool_calls_limit: 20,
    commands: 11,
    command_limit: 22,
  },
});
const status = (id: string | null = null) => ({
  version: 1,
  kind: "unclaimed_invalid_read_recovery_status",
  run_id: context.run,
  request_id: request,
  recovery_id: id,
  recovery_record_digest: id ? hash : null,
  status: id ? "queued" : "not_recovered",
  original_admission_seal: hash,
  lease: id ? {} : null,
  current_profile: { id: "qualified" },
});
describe("failed preclaim recovery contract", () => {
  it("binds exact request and keeps opaque identities in the automatic body", () => {
    const c = candidate(fixture(), context);
    expect(c.maximum_window_seconds).toBe(300);
    expect(recoveryInput(c)).toEqual({
      version: 1,
      candidate_digest: hash,
      expected_admission_seal: hash,
      expected_failed_event_digest: hash,
      valid_for_seconds: 300,
      acknowledge_never_claimed: true,
      acknowledge_same_request_and_attempt: true,
      acknowledge_preserved_consumption_and_budgets: true,
      acknowledge_renewed_same_attempt_lease: true,
    });
  });
  it.each([
    { run_id: "5".repeat(32) },
    { request_id: "6".repeat(32) },
    { execution_authorized: true },
    { version: 2 },
    { admission: "READY", holds: ["current_profile_unqualified"] },
    { maximum_window_seconds: 601 },
    { digest: "wrong" },
    { claimed: true },
    { attempt: 0 },
    { usage: { requests: -1, tool_calls: 2, commands: 11 } },
  ])("refuses mismatched or unsafe candidate %j", (change) => {
    expect(() => candidate({ ...fixture(), ...change }, context)).toThrow();
  });
  it.each([
    "current_profile_unqualified",
    "lease_policy_disabled",
    "unavailable_observation_policy_disabled",
    "active_execution_budget_expired",
  ])("keeps %s held", (hold) => {
    const c = candidate(
      {
        ...fixture(),
        admission: "HOLD",
        holds: [hold],
        maximum_window_seconds: 0,
      },
      context,
    );
    expect(() => recoveryInput(c)).toThrow();
  });
  it("accepts same action readback and rejects another action or absent result", () => {
    const id = "7".repeat(32);
    expect(recoveryStatus(status(id), context, id, hash).status).toBe("queued");
    expect(() =>
      recoveryStatus(status("8".repeat(32)), context, id, hash),
    ).toThrow();
    expect(() => recoveryStatus(status(), context, id, hash)).toThrow();
    expect(() =>
      recoveryStatus(
        { ...status(id), original_admission_seal: "c".repeat(64) },
        context,
        id,
        hash,
      ),
    ).toThrow();
  });
  it("retains one identity before POST and refuses retries after a lost response", async () => {
    const data = new Map<string, string>();
    const storage = {
      getItem: (key: string) => data.get(key) ?? null,
      setItem: (key: string, value: string) => {
        data.set(key, value);
      },
    };
    const marker = {
      version: 1 as const,
      context,
      recovery: "7".repeat(32),
      body: recoveryInput(candidate(fixture(), context)),
    };
    let calls = 0;
    await expect(
      sendOnce(storage, marker, async () => {
        calls++;
        expect(
          readMarker(storage.getItem(markerKey(context)), context),
        ).toEqual(marker);
        throw new Error("lost");
      }),
    ).rejects.toThrow("lost");
    await expect(
      sendOnce(storage, marker, async () => {
        calls++;
      }),
    ).rejects.toThrow();
    expect(calls).toBe(1);
    expect(() =>
      readMarker(storage.getItem(markerKey(context)), {
        ...context,
        actor: "9".repeat(32),
      }),
    ).toThrow();
  });
  it("refuses storage failure before submission", async () => {
    let calls = 0;
    const marker = {
      version: 1 as const,
      context,
      recovery: "7".repeat(32),
      body: recoveryInput(candidate(fixture(), context)),
    };
    await expect(
      sendOnce(
        {
          getItem: () => null,
          setItem: () => {
            throw Error("denied");
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
});
