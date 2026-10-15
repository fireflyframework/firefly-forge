/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  invalidationKey,
  readInvalidationMarker,
  readLearningInvalidation,
  retainInvalidation,
  type InvalidationMarker,
} from "./LearningRecordData";
const pending: InvalidationMarker = {
  version: 1,
  base: "http://127.0.0.1:60000/api/v1",
  actor: "a".repeat(32),
  repository: "b".repeat(32),
  project: "c".repeat(32),
  run: "d".repeat(32),
  record: "e".repeat(32),
  requestId: "f".repeat(32),
  reason: "The dependency changed; this observation no longer applies.",
};
const receipt = {
  event_id: "1".repeat(32),
  record_id: pending.record,
  repository_id: pending.repository,
  run_id: pending.run,
  request_id: pending.requestId,
  kind: "invalidation",
  event_state: "invalidated",
  current_record_state: "invalidated",
  reason: pending.reason,
  actor_id: pending.actor,
  created_at: "2026-10-15T01:15:00+00:00",
  sequence: 3,
  digest: "2".repeat(64),
};
it("binds a retained request to its exact factory, actor and evidence scope", () => {
  expect(readInvalidationMarker(JSON.stringify(pending), pending)).toEqual(
    pending,
  );
  expect(readInvalidationMarker(null, pending)).toBeNull();
  for (const field of [
    "base",
    "actor",
    "repository",
    "project",
    "run",
    "record",
  ] as const) {
    expect(() =>
      readInvalidationMarker(
        JSON.stringify({ ...pending, [field]: "0".repeat(32) }),
        pending,
      ),
    ).toThrow();
  }
  expect(invalidationKey(pending)).not.toBe(
    invalidationKey({ ...pending, actor: "3".repeat(32) }),
  );
  expect(invalidationKey(pending)).not.toBe(
    invalidationKey({ ...pending, base: "http://other/api/v1" }),
  );
});
it("refuses corrupt or expanded pending packets instead of treating them as empty", () => {
  for (const packet of [
    "broken",
    "{}",
    JSON.stringify({ ...pending, reason: " " }),
    JSON.stringify({ ...pending, requestId: "wrong" }),
    JSON.stringify({ ...pending, token: "forbidden" }),
  ]) {
    expect(() => readInvalidationMarker(packet, pending)).toThrow();
  }
});
it("requires storage readback and refuses to replace an outstanding request", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
  retainInvalidation(storage, pending);
  expect(
    readInvalidationMarker(storage.getItem(invalidationKey(pending)), pending),
  ).toEqual(pending);
  expect(() =>
    retainInvalidation(storage, { ...pending, requestId: "9".repeat(32) }),
  ).toThrow();
  expect(() =>
    retainInvalidation({ getItem: () => null, setItem: () => {} }, pending),
  ).toThrow();
  expect(() =>
    retainInvalidation(
      {
        getItem: () => {
          throw new Error("Blocked");
        },
        setItem: () => {},
      },
      pending,
    ),
  ).toThrow();
});
it("admits only the exact original event while keeping current state separate", () => {
  expect(readLearningInvalidation(receipt, pending)).toEqual(receipt);
  expect(
    readLearningInvalidation(
      { ...receipt, current_record_state: "superseded" },
      pending,
    ).event_state,
  ).toBe("invalidated");
  for (const field of [
    "record_id",
    "repository_id",
    "run_id",
    "request_id",
    "actor_id",
    "reason",
    "kind",
    "event_state",
  ] as const) {
    expect(() =>
      readLearningInvalidation({ ...receipt, [field]: "other" }, pending),
    ).toThrow();
  }
});
it("does not close a new request using legacy unknown attribution or malformed receipt evidence", () => {
  expect(() =>
    readLearningInvalidation({ ...receipt, actor_id: null }, pending),
  ).toThrow(/attribution is unknown/);
  for (const value of [
    { ...receipt, digest: "bad" },
    { ...receipt, event_id: "bad" },
    { ...receipt, sequence: "3" },
    { ...receipt, sequence: 0 },
    { ...receipt, created_at: "yesterday" },
    { ...receipt, created_at: "2026-10-15T01:15:00" },
    { ...receipt, extra: true },
  ]) {
    expect(() => readLearningInvalidation(value, pending)).toThrow();
  }
});
