/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  attemptDigest,
  attemptInput,
  attemptKey,
  readAttemptMarker,
  retainAttempt,
  exactAttemptMarker,
  readAttemptReceipt,
  archiveRefusedAttempt,
} from "./LearningObservationAttemptData";
import {
  observationKey,
  type ObservationMarker,
} from "./LearningObservationData";
const pending: ObservationMarker = {
  version: 1,
  base: "https://factory.example/api/v1",
  actor: "1".repeat(32),
  repository: "2".repeat(32),
  project: "3".repeat(32),
  run: "4".repeat(32),
  record: "5".repeat(32),
  requestId: "a".repeat(32),
  projection: "b".repeat(32),
  submissionRequest: "c".repeat(32),
  workspace: "forge-learning-owned",
  source: "owned-source",
  targetRevision: "d".repeat(32),
  payloadDigest: "e".repeat(64),
  contentHash: "f".repeat(64),
};
function storage() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
}
async function setup() {
  const saved = storage(),
    marker = await retainAttempt(saved, pending);
  const unsigned = {
    version: 2,
    attempt_id: "obs2_" + marker.requestId,
    record_id: marker.record,
    repository_id: marker.repository,
    project_id: marker.project,
    run_id: marker.run,
    actor_id: marker.actor,
    input_digest: marker.inputDigest,
    source_digest: marker.sourceDigest,
    projection_id: marker.projection,
    submission_request_id: marker.submissionRequest,
    workspace_id: marker.workspace,
    target_revision: marker.targetRevision,
    payload_digest: marker.payloadDigest,
    content_sha256: marker.contentHash,
    state: "not_dispatched",
    disposition: "local_admission_refused",
    created_at: "2026-10-15T06:00:00Z",
    finished_at: "2026-10-15T06:00:01Z",
    observation: null,
  };
  return {
    saved,
    marker,
    receipt: { ...unsigned, digest: await attemptDigest(unsigned) },
  };
}
it("matches backend canonical input/source vectors", async () => {
  expect(await attemptDigest(attemptInput(pending))).toBe(
    "26d27b2ea636c7f2fc608861df338fc280cbcf6c1d6feddf0bcc19cc7ea3e263",
  );
  expect(await attemptDigest({ source_id: pending.source })).toBe(
    "569c2816bc705d38b9cb571e9d3482ed3ddec5420ccb3a958731ba914f9652e6",
  );
  expect(
    await attemptDigest({
      ...attemptInput(pending),
      source_id: "owned.source-1_2",
    }),
  ).toBe("68c1d7541909356099f639f93121ace4f227f97fd56eeda576c6ce5640581afd");
  expect(
    await attemptDigest({ ...attemptInput(pending), source_id: null }),
  ).toBe("a3d54e0f2fcb0154a76804201ee8602adffe1ba2d2dfd6139f06dfbd57e2cee3");
});
it("persists only source digest and verifies storage before dispatch", async () => {
  const { saved, marker } = await setup(),
    packet = saved.getItem(attemptKey(pending))!;
  expect(packet).not.toContain(pending.source);
  expect(JSON.parse(packet)).not.toHaveProperty("source");
  expect(readAttemptMarker(packet, pending)).toEqual(marker);
  expect(await exactAttemptMarker(saved, marker)).toEqual(marker);
  await expect(retainAttempt(saved, pending)).rejects.toThrow();
  await expect(
    retainAttempt({ getItem: () => null, setItem: () => {} }, pending),
  ).rejects.toThrow();
});
it("holds v1 identities and corrupt or expanded v2 packets", async () => {
  const { saved, marker } = await setup();
  for (const extra of [
    { source: pending.source },
    { actor: "9".repeat(32) },
    { sourceDigest: "bad" },
    { token: "private" },
  ])
    expect(() =>
      readAttemptMarker(JSON.stringify({ ...marker, ...extra }), pending),
    ).toThrow();
  saved.values.set(observationKey(pending), JSON.stringify(pending));
  await expect(exactAttemptMarker(saved, marker)).rejects.toThrow();
});
it("binds every authority/input anchor and source digest", async () => {
  const { marker, receipt } = await setup();
  expect((await readAttemptReceipt(receipt, marker)).state).toBe(
    "not_dispatched",
  );
  for (const field of [
    "actor_id",
    "record_id",
    "repository_id",
    "project_id",
    "run_id",
    "attempt_id",
    "input_digest",
    "source_digest",
    "projection_id",
    "submission_request_id",
    "workspace_id",
    "target_revision",
    "payload_digest",
    "content_sha256",
  ]) {
    const unsigned = { ...receipt, [field]: "0".repeat(64) };
    Reflect.deleteProperty(unsigned, "digest");
    await expect(
      readAttemptReceipt(
        { ...unsigned, digest: await attemptDigest(unsigned) },
        marker,
      ),
    ).rejects.toThrow();
  }
});
it("refuses tampered and contradictory terminal receipts", async () => {
  const { marker, receipt } = await setup();
  await expect(
    readAttemptReceipt(
      { ...receipt, finished_at: "2026-10-15T06:00:02Z" },
      marker,
    ),
  ).rejects.toThrow();
  for (const fields of [
    { state: "not_dispatched", disposition: null },
    { state: "not_dispatched", finished_at: null },
    { state: "dispatched", disposition: null },
    { state: "verified", disposition: "verified", observation: null },
  ]) {
    const unsigned = { ...receipt, ...fields };
    Reflect.deleteProperty(unsigned, "digest");
    await expect(
      readAttemptReceipt(
        { ...unsigned, digest: await attemptDigest(unsigned) },
        marker,
      ),
    ).rejects.toThrow();
  }
});
it("archives terminal evidence before removing the exact marker", async () => {
  const { saved, marker, receipt } = await setup();
  await archiveRefusedAttempt(
    saved,
    marker,
    await readAttemptReceipt(receipt, marker),
  );
  expect(saved.getItem(attemptKey(marker))).toBeNull();
  const archive = [...saved.values.values()][0];
  expect(archive).not.toContain(pending.source);
  expect(JSON.parse(archive).receipt.digest).toBe(receipt.digest);
});
it("never clears unknown/dispatched/preparing attempts", async () => {
  for (const state of ["unknown", "dispatched", "preparing"]) {
    const { saved, marker, receipt } = await setup();
    const unsigned = {
      ...receipt,
      state,
      disposition: null,
      finished_at: state === "unknown" ? receipt.finished_at : null,
    };
    Reflect.deleteProperty(unsigned, "digest");
    const valid = await readAttemptReceipt(
      { ...unsigned, digest: await attemptDigest(unsigned) },
      marker,
    );
    await expect(archiveRefusedAttempt(saved, marker, valid)).rejects.toThrow();
    expect(saved.getItem(attemptKey(marker))).not.toBeNull();
  }
});
it("retains the active marker on archival storage failure", async () => {
  const { saved, marker, receipt } = await setup();
  await expect(
    archiveRefusedAttempt(
      { ...saved, setItem: () => {} },
      marker,
      await readAttemptReceipt(receipt, marker),
    ),
  ).rejects.toThrow();
  expect(saved.getItem(attemptKey(marker))).not.toBeNull();
});
it("does not overwrite another attempt while asynchronous hashing is pending", async () => {
  const saved = storage();
  const results = await Promise.allSettled([
    retainAttempt(saved, pending),
    retainAttempt(saved, { ...pending, requestId: "8".repeat(32) }),
  ]);
  expect(
    results.filter((result) => result.status === "fulfilled"),
  ).toHaveLength(1);
});
