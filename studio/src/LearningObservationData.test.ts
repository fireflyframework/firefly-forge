/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  observationKey,
  readObservationMarker,
  readLearningObservation,
  retainObservation,
  type ObservationMarker,
} from "./LearningObservationData";
const pending: ObservationMarker = {
  version: 1,
  base: "http://127.0.0.1:60000/api/v1",
  actor: "a".repeat(32),
  repository: "b".repeat(32),
  project: "c".repeat(32),
  run: "d".repeat(32),
  record: "e".repeat(32),
  requestId: "f".repeat(32),
  projection: "1".repeat(32),
  submissionRequest: "2".repeat(32),
  workspace: "forge-learning-owned",
  source: "owned-source",
  targetRevision: "3".repeat(32),
  payloadDigest: "4".repeat(64),
  contentHash: "5".repeat(64),
};
const receipt = {
  event_id: "6".repeat(32),
  record_id: pending.record,
  repository_id: pending.repository,
  run_id: pending.run,
  request_id: pending.requestId,
  kind: "projection_observation",
  event_state: "verified",
  current_record_state: "verified",
  current_projection_state: "verified",
  actor_id: pending.actor,
  projection_id: pending.projection,
  submission_request_id: pending.submissionRequest,
  workspace_id: pending.workspace,
  source_id: pending.source,
  target_revision: pending.targetRevision,
  payload_digest: pending.payloadDigest,
  content_sha256: pending.contentHash,
  citations: [{ source_id: pending.source, chunk_id: "chunk:owned-1" }],
  created_at: "2026-10-15T06:00:00+00:00",
  sequence: 4,
  digest: "7".repeat(64),
};
it("retains the exact observation identity without credentials", () => {
  expect(readObservationMarker(JSON.stringify(pending), pending)).toEqual(
    pending,
  );
  expect(readObservationMarker(null, pending)).toBeNull();
  for (const field of [
    "base",
    "actor",
    "repository",
    "project",
    "run",
    "record",
  ] as const)
    expect(() =>
      readObservationMarker(
        JSON.stringify({ ...pending, [field]: "0".repeat(32) }),
        pending,
      ),
    ).toThrow();
  expect(observationKey(pending)).not.toBe(
    observationKey({ ...pending, actor: "0".repeat(32) }),
  );
  expect(observationKey(pending)).not.toBe(
    observationKey({ ...pending, base: "https://other/api/v1" }),
  );
});
it("blocks corrupt, expanded and unsafe pending packets", () => {
  for (const value of [
    "broken",
    "{}",
    JSON.stringify({ ...pending, token: "forbidden" }),
    JSON.stringify({ ...pending, source: "../private" }),
    JSON.stringify({ ...pending, requestId: "other" }),
    JSON.stringify({ ...pending, payloadDigest: "bad" }),
    JSON.stringify({
      ...pending,
      base: "http://name:credential@localhost/api/v1",
    }),
  ])
    expect(() => readObservationMarker(value, pending)).toThrow();
});
it("writes and reads back the marker before allowing an effect", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
  retainObservation(storage, pending);
  expect(
    readObservationMarker(storage.getItem(observationKey(pending)), pending),
  ).toEqual(pending);
  expect(() =>
    retainObservation(storage, { ...pending, requestId: "8".repeat(32) }),
  ).toThrow();
  expect(() =>
    retainObservation({ getItem: () => null, setItem: () => {} }, pending),
  ).toThrow();
});
it("requires the exact historical observation and distinguishes current states", () => {
  expect(readLearningObservation(receipt, pending)).toEqual(receipt);
  const changed = readLearningObservation(
    {
      ...receipt,
      current_record_state: "invalidated",
      current_projection_state: "unknown",
    },
    pending,
  );
  expect(changed.event_state).toBe("verified");
  expect(changed.current_record_state).toBe("invalidated");
  for (const field of [
    "record_id",
    "repository_id",
    "run_id",
    "request_id",
    "actor_id",
    "projection_id",
    "submission_request_id",
    "workspace_id",
    "source_id",
    "target_revision",
    "payload_digest",
    "content_sha256",
    "kind",
    "event_state",
  ])
    expect(() =>
      readLearningObservation({ ...receipt, [field]: "other" }, pending),
    ).toThrow();
});
it("does not reconcile new requests using legacy unknown provenance", () => {
  for (const field of [
    "actor_id",
    "projection_id",
    "submission_request_id",
    "workspace_id",
    "target_revision",
    "payload_digest",
  ])
    expect(() =>
      readLearningObservation({ ...receipt, [field]: null }, pending),
    ).toThrow(/provenance is incomplete/);
});
it("refuses missing or foreign citations, malformed identities and expanded receipts", () => {
  for (const value of [
    { ...receipt, citations: [] },
    { ...receipt, citations: Array(9).fill(receipt.citations[0]) },
    { ...receipt, citations: [{ source_id: "foreign", chunk_id: "owned" }] },
    {
      ...receipt,
      citations: [{ source_id: pending.source, chunk_id: "../private" }],
    },
    { ...receipt, event_id: "bad" },
    { ...receipt, digest: "bad" },
    { ...receipt, sequence: 0 },
    { ...receipt, created_at: "2026-10-15T06:00:00" },
    { ...receipt, token: "forbidden" },
  ])
    expect(() => readLearningObservation(value, pending)).toThrow();
});
it("validates the retained identity before trusting matching receipt fields", () => {
  const malformed = { ...pending, contentHash: "not-a-hash" };
  expect(() =>
    readLearningObservation(
      { ...receipt, content_sha256: malformed.contentHash },
      malformed,
    ),
  ).toThrow();
});
