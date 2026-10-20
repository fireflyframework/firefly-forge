/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import fixture from "../tests/coding-check-grant-fixture.json";
import {
  codingCandidate,
  codingInput,
  codingReceipt,
  codingCurrent,
  retainCodingGrant,
  readCodingMarker,
  retainCodingAllowance,
  codingPort,
  codingUnclaimed,
} from "./CodingCheckGrantData";
import {
  allowanceCandidate,
  allowanceInput,
  allowanceMarkerKey,
  retainAllowance,
} from "./ProviderRecoveryAllowanceData";
import { candidate as finalCandidate } from "../tests/provider-recovery-allowance-fixture";
import { attemptDigest } from "./LearningObservationAttemptData";
const context = {
  base: "https://factory.example/api/v1",
  actor: "a".repeat(32),
  authentication: "session",
  run: "c".repeat(32),
  repository: "b".repeat(32),
  project: null,
  agent: "codex" as const,
};
const recoveryId = "d".repeat(32),
  grantId = "3".repeat(32);
const candidate = () =>
  codingCandidate(fixture.candidate, context, recoveryId, 11447);
const input = () =>
  codingInput(
    candidate(),
    grantId,
    [0, 1],
    2,
    900,
    "Reviewed configured checks.",
  );
const locks = {
  request: async <T>(
    _key: string,
    _options: { mode: "exclusive" },
    call: () => Promise<T>,
  ) => call(),
};
function storage() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
  };
}
it("reads only the reviewed candidate and rejects altered typed boundaries", () => {
  expect(candidate().channel.bind_port).toBe(11447);
  for (const change of [
    { run_id: "0".repeat(32) },
    { task_attempt: 6 },
    { revision: true },
    { commands: [{ command_index: 20, argv: ["test"], timeout: 1 }] },
    { command_indices: [true] },
    { shared_commands_remaining: 100 },
    { extra: true },
  ])
    expect(() =>
      codingCandidate(
        { ...fixture.candidate, ...change },
        context,
        recoveryId,
        11447,
      ),
    ).toThrow();
  for (const port of [true, 1, 1023, 65536, "11447", 1.5])
    expect(() => codingPort(port)).toThrow();
  for (const port of [0, 1024, 11447, 65535])
    expect(codingPort(port)).toBe(port);
});
it("requires an exact unclaimed native current status before preparing another identity", () => {
  const none = {
    protocol: "native-coding-configured-check-v1",
    run_id: context.run,
    grant_id: null,
    revision: 0,
    status: "none",
    receipt_digest: null,
    expires_at_ms: null,
  };
  expect(() => codingUnclaimed(none, context)).not.toThrow();
  for (const change of [
    { status: "active" },
    { status: "revoked" },
    { status: "expired" },
    { status: "unknown" },
    { revision: true },
    { grant_id: grantId },
    { run_id: "0".repeat(32) },
    { extra: true },
  ])
    expect(() => codingUnclaimed({ ...none, ...change }, context)).toThrow();
});
it("supports index19 without inventing a product-specific check count", () => {
  const value = structuredClone(fixture.candidate);
  value.commands = Array.from({ length: 20 }, (_, i) => ({
    command_index: i,
    argv: ["check", String(i)],
    timeout: 30,
  }));
  value.command_indices = value.commands.map((c) => c.command_index);
  value.command_count = 20;
  value.independent_final_check_count = 20;
  value.shared_command_limit = 50;
  value.shared_commands_used = 0;
  value.shared_commands_remaining = 50;
  value.runner.configuration.command_limit = 50;
  Object.assign(value, { interactive_commands_remaining: 30 });
  const parsed = codingCandidate(value, context, recoveryId, 11447);
  expect(
    codingInput(parsed, grantId, [19], 1, 30, "Index nineteen.")
      .command_indices,
  ).toEqual([19]);
  for (const indices of [[20], [true], [19, 19], []])
    expect(() =>
      codingInput(parsed, grantId, indices as number[], 1, 30, "Reason"),
    ).toThrow();
});
it("preserves independent final verification capacity and rejects excess, boolean or expired bounds", () => {
  for (const values of [
    [8, 900],
    [true, 900],
    [2, 29],
    [2, 3601],
    [2, 1.5],
  ])
    expect(() =>
      codingInput(
        candidate(),
        grantId,
        [0, 1],
        values[0] as number,
        values[1] as number,
        "Reason",
      ),
    ).toThrow();
});
it("retains the exact future recovery ID and full grant body before any effect, excluding legacy reservations", async () => {
  const store = storage(),
    body = input();
  const marker = await retainCodingGrant(
    store,
    locks,
    context,
    candidate(),
    body,
  );
  expect(
    await readCodingMarker(store.getItem(allowanceMarkerKey(context)), context),
  ).toEqual(marker);
  expect(marker.grant.recovery_request_id).toBe(recoveryId);
  await expect(
    retainCodingGrant(store, locks, context, candidate(), {
      ...body,
      request_id: "4".repeat(32),
    }),
  ).rejects.toThrow();
  const view = allowanceCandidate(
    { ...finalCandidate, run_id: context.run },
    context,
  );
  await expect(
    retainAllowance(
      store,
      locks,
      context,
      view,
      allowanceInput(view, "5".repeat(32), "Reason", "e".repeat(64)),
    ),
  ).rejects.toThrow();
  const broken = { ...store, setItem: () => {} };
  await expect(
    retainCodingGrant(broken, locks, context, candidate(), body),
  ).rejects.toThrow();
});
it("confirms exact original receipt and current revision/seal/expiry, never historical authority alone", async () => {
  const marker = await retainCodingGrant(
    storage(),
    locks,
    context,
    candidate(),
    input(),
  );
  const value = {
    ...fixture.immutable_grant_receipt,
    request: marker.grant,
    candidate: marker.candidate,
  };
  const seal = await attemptDigest(value);
  const parsed = await codingReceipt(
    { ...value, receipt_digest: seal },
    marker,
  );
  const current = {
    ...fixture.current_active,
    receipt_digest: seal,
    expires_at_ms: Date.now() + 900000,
  };
  const receipt = { ...parsed, expires_at_ms: current.expires_at_ms };
  expect(() => codingCurrent(current, context, receipt)).not.toThrow();
  for (const change of [
    { status: "revoked" },
    { status: "expired" },
    { revision: 2 },
    { receipt_digest: "0".repeat(64) },
    { grant_id: "0".repeat(32) },
    { expires_at_ms: 1 },
  ])
    expect(() =>
      codingCurrent({ ...current, ...change }, context, receipt),
    ).toThrow();
  await expect(
    codingReceipt(
      {
        ...value,
        request: { ...marker.grant, call_limit: 1 },
        receipt_digest: seal,
      },
      marker,
    ),
  ).rejects.toThrow();
  await expect(
    codingReceipt(
      {
        ...value,
        actor: { ...value.actor, user_id: "0".repeat(32) },
        receipt_digest: seal,
      },
      marker,
    ),
  ).rejects.toThrow();
});
it("persists only a grant-linked final body using the original recovery identity, with no replacement", async () => {
  const store = storage(),
    marker = await retainCodingGrant(
      store,
      locks,
      context,
      candidate(),
      input(),
    );
  const view = allowanceCandidate(
    { ...finalCandidate, run_id: context.run },
    context,
  );
  const body = {
    ...allowanceInput(view, recoveryId, "Reviewed mitigation.", "e".repeat(64)),
    coding_check_grant_id: grantId,
  };
  const updated = await retainCodingAllowance(store, locks, marker, view, body);
  expect(updated.allowance?.body).toEqual(body);
  expect(
    await readCodingMarker(store.getItem(allowanceMarkerKey(context)), context),
  ).toEqual(updated);
  await expect(
    retainCodingAllowance(store, locks, updated, view, {
      ...body,
      request_id: "f".repeat(32),
    }),
  ).rejects.toThrow();
  await expect(
    retainCodingAllowance(store, locks, updated, view, body),
  ).rejects.toThrow();
});

it("reserves independent policy checks beyond the configured index count", () => {
  const value = {
    ...fixture.candidate,
    independent_final_check_count: 3,
    interactive_commands_remaining: 6,
  };
  expect(
    codingCandidate(value, context, recoveryId, 11447)
      .independent_final_check_count,
  ).toBe(3);
});
it("permits bounded repeated calls to one selected check without widening the index grant", () => {
  expect(
    codingInput(candidate(), grantId, [0], 2, 900, "Permit a bounded rerun.")
      .call_limit,
  ).toBe(2);
});
it("supports native grant revisions beyond twenty without accepting unsafe integers", () => {
  expect(
    codingCandidate(
      { ...fixture.candidate, revision: 39 },
      context,
      recoveryId,
      11447,
    ).revision,
  ).toBe(39);
  expect(() =>
    codingCandidate(
      { ...fixture.candidate, revision: Number.MAX_SAFE_INTEGER + 1 },
      context,
      recoveryId,
      11447,
    ),
  ).toThrow();
});
