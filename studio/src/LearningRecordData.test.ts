/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  readLearningRecords,
  readLearningProjection,
} from "./LearningRecordData";
import { record } from "../tests/learning-record-fixture";

it("retains native source and receipt provenance without inventing eligibility", () => {
  const result = readLearningRecords(
    [{ ...record, unexpected_private_field: "fixture-hidden" }],
    "a".repeat(32),
  );
  expect(result).toHaveLength(1);
  expect(result[0]).toMatchObject({
    state: "verified",
    envelope: {
      context: { repository_id: "a".repeat(32) },
      receipts: [{ sequence: 6 }],
    },
  });
  expect(result[0]).not.toHaveProperty("eligible");
  expect(result[0]).not.toHaveProperty("unexpected_private_field");
  expect(result[0]).not.toHaveProperty("observed_at");
});
it.each([
  {
    ...record,
    envelope: {
      ...record.envelope,
      context: { ...record.envelope.context, repository_id: "9".repeat(32) },
    },
  },
  { ...record, state: "eligible" },
  { ...record, expires_at: "not-a-date" },
  {
    ...record,
    envelope: {
      ...record.envelope,
      source: { ...record.envelope.source, source_tree: "1".repeat(40) },
    },
  },
  {
    ...record,
    envelope: {
      ...record.envelope,
      receipts: [{ ...record.envelope.receipts[0], sequence: "6" }],
    },
  },
])(
  "refuses malformed or foreign-scope evidence instead of displaying it as saved authority",
  (value) =>
    expect(() => readLearningRecords([value], "a".repeat(32))).toThrow(
      /learning records/i,
    ),
);
it("cannot present a verified projection without its exact source and citation provenance", () => {
  expect(() =>
    readLearningProjection(
      { id: "1".repeat(32), record_id: record.id, state: "verified" },
      record.id,
    ),
  ).toThrow();
});
it("preserves only safe matching projection provenance", () => {
  const result = readLearningProjection(
    {
      id: "1".repeat(32),
      record_id: record.id,
      state: "verified",
      workspace_id: "owned-learning",
      source_id: "owned-source",
      content_sha256: "2".repeat(64),
      citations: [{ source_id: "owned-source", chunk_id: "owned-chunk" }],
      target: { revision: "3".repeat(32), credential: "fixture-hidden" },
      payload: { secret: "fixture-hidden" },
    },
    record.id,
  );
  expect(result.citations).toEqual([
    { source_id: "owned-source", chunk_id: "owned-chunk" },
  ]);
  expect(result.target_revision).toBe("3".repeat(32));
  expect(result).not.toHaveProperty("target");
  expect(result).not.toHaveProperty("payload");
  expect(() =>
    readLearningProjection({ ...result, record_id: "9".repeat(32) }, record.id),
  ).toThrow();
  expect(() =>
    readLearningProjection(
      {
        ...result,
        citations: [{ source_id: "foreign-source", chunk_id: "owned-chunk" }],
      },
      record.id,
    ),
  ).toThrow();
});
