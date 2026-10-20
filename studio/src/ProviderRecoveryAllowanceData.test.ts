/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  allowanceCandidate,
  allowanceInput,
  allowanceMarkerKey,
  allowanceReceipt,
  allowanceReadback,
  existingAllowanceId,
  readAllowanceMarker,
  retainAllowance,
} from "./ProviderRecoveryAllowanceData";
import {
  candidate,
  context,
  receipt,
} from "../tests/provider-recovery-allowance-fixture";

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
}
const locks = {
  request: async <T>(
    _key: string,
    _options: { mode: "exclusive" },
    call: () => Promise<T>,
  ) => call(),
};

it("reviews only the native fifth candidate and keeps server digests opaque with large native nanoseconds", () => {
  const view = allowanceCandidate(candidate, context);
  expect(view.executablePath).toBe("/opt/firefly/codex");
  expect(view.sourceDigest).toBe("c".repeat(64));
  for (const change of [
    { run_id: "0".repeat(32) },
    { agent: "claude" },
    { next_attempt: 6 },
    { dispatches_used: 3 },
    { process_evidence: "legacy_operator_attestation_required" },
    { native_process_digest: null },
    {
      executable_adoption: {
        ...candidate.executable_adoption,
        path: "relative",
      },
    },
    {
      executable_adoption: {
        ...candidate.executable_adoption,
        path: "/opt/firefly/codex\u0000",
      },
    },
    { extra: "unknown" },
  ])
    expect(() =>
      allowanceCandidate({ ...candidate, ...change }, context),
    ).toThrow();
});
it("requires bounded reason and exact mitigation fingerprint without adding a budget or guessed proof", () => {
  const view = allowanceCandidate(candidate, context);
  expect(
    allowanceInput(
      view,
      "a".repeat(32),
      "Reviewed a finite local mitigation.",
      "e".repeat(64),
    ),
  ).toEqual({
    request_id: "a".repeat(32),
    candidate_digest: candidate.digest,
    native_process_digest: candidate.native_process_digest,
    agent: "codex",
    reason: "Reviewed a finite local mitigation.",
    mitigation_evidence_digest: "e".repeat(64),
    adopt_current_executable: true,
  });
  for (const [reason, hash] of [
    [" ", "e".repeat(64)],
    ["x".repeat(2001), "e".repeat(64)],
    ["Reviewed", "not-sha"],
  ])
    expect(() => allowanceInput(view, "a".repeat(32), reason, hash)).toThrow();
});
it("saves exact body under a cross-tab lock before transport and never replaces a retained identity", async () => {
  const store = storage(),
    view = allowanceCandidate(candidate, context);
  const body = allowanceInput(
    view,
    "a".repeat(32),
    "Reviewed locally.",
    "e".repeat(64),
  );
  const marker = await retainAllowance(store, locks, context, view, body);
  expect(
    await readAllowanceMarker(
      store.getItem(allowanceMarkerKey(context)),
      context,
    ),
  ).toEqual(marker);
  expect(marker.body).toEqual(body);
  await expect(
    retainAllowance(store, locks, context, view, {
      ...body,
      request_id: "b".repeat(32),
    }),
  ).rejects.toThrow();
  const raw = store.getItem(allowanceMarkerKey(context))!;
  await expect(
    readAllowanceMarker(raw.replace("Reviewed locally.", "Tampered"), context),
  ).rejects.toThrow();
  await expect(
    readAllowanceMarker(raw, { ...context, actor: "d".repeat(32) }),
  ).rejects.toThrow();
});
it("failed storage or missing Web Locks prevents a reservation from being retained", async () => {
  const view = allowanceCandidate(candidate, context);
  const body = allowanceInput(
    view,
    "a".repeat(32),
    "Reviewed locally.",
    "e".repeat(64),
  );
  await expect(
    retainAllowance(
      { getItem: () => null, setItem: () => undefined },
      locks,
      context,
      view,
      body,
    ),
  ).rejects.toThrow();
  await expect(
    retainAllowance(storage(), undefined, context, view, body),
  ).rejects.toThrow();
});
it("confirms exact receipt identity and refuses unrelated operator, task, attestation or body", async () => {
  const view = allowanceCandidate(candidate, context);
  const body = allowanceInput(
    view,
    "a".repeat(32),
    "Reviewed locally.",
    "e".repeat(64),
  );
  const marker = await retainAllowance(storage(), locks, context, view, body);
  const value = await receipt(body);
  expect(await allowanceReceipt(value, marker)).toEqual({
    requestId: body.request_id,
    attempt: 5,
  });
  for (const change of [
    { actor: { ...value.actor, user_id: "0".repeat(32) } },
    { task_key: "0".repeat(32) },
    { request: { ...body, reason: "Changed" } },
    {
      mitigation_evidence: { provenance: "server_qa", digest: "e".repeat(64) },
    },
    { candidate: { ...candidate, digest: "0".repeat(64) } },
  ])
    await expect(
      allowanceReceipt({ ...value, ...change }, marker),
    ).rejects.toThrow();
  expect(await attemptDigest(body)).toHaveLength(64);
});
it("reads a server reservation without inventing a retained request and rejects foreign actor, protocol or ID", async () => {
  const requestId = "a".repeat(32);
  const runMarker = {
    allowance_id: requestId,
    request_id: requestId,
    allowance_digest: "c".repeat(64),
    attempt: 5,
  };
  expect(existingAllowanceId(runMarker)).toBe(requestId);
  expect(existingAllowanceId({ ...runMarker, allowance_id: "bad" })).toBeNull();
  expect(existingAllowanceId({ ...runMarker, attempt: 6 })).toBeNull();
  expect(
    existingAllowanceId({ ...runMarker, request_id: "b".repeat(32) }),
  ).toBeNull();
  const body = allowanceInput(
    allowanceCandidate(candidate, context),
    requestId,
    "Reviewed locally.",
    "e".repeat(64),
  );
  const value = await receipt(body);
  const original = JSON.stringify(value);
  expect(await allowanceReadback(value, context, requestId)).toEqual({
    requestId,
    attempt: 5,
  });
  expect(JSON.stringify(value)).toBe(original);
  for (const [packet, id] of [
    [
      { ...value, actor: { ...value.actor, user_id: "0".repeat(32) } },
      requestId,
    ],
    [{ ...value, protocol: "legacy" }, requestId],
    [value, "b".repeat(32)],
  ] as const)
    await expect(allowanceReadback(packet, context, id)).rejects.toThrow();
});
