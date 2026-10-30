/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  nativeProjection,
  nativeRecord,
  nativeVersion,
  nativeVerifiedProjection,
  nativeReceipt,
  nativeFailureProjection,
  nativeFailureReceipt,
} from "../tests/native-learning-fixture";
import {
  readLearningProjection,
  readLearningRecords,
} from "./LearningRecordData";
import * as native from "./NativeLearningData";

it("keeps native version and scope evidence instead of reducing it to a V1 citation", () => {
  const projection = readLearningProjection(
    nativeVerifiedProjection,
    nativeRecord.id,
  );
  expect(projection).toMatchObject({
    native: {
      version: 2,
      kind: "local_outcome",
      upstreamAuthority: "unverified",
      tenant: "fixture-tenant",
      scope: nativeProjection.scope,
      sourceVersions: [nativeVersion],
    },
  });
});

it("retains only an explicit recorded provider hint while unknown records stay unknown", () => {
  const [record] = readLearningRecords(
    [nativeRecord],
    nativeRecord.envelope.context.repository_id,
  );
  expect(record).toMatchObject({ knowledge_provider: "koinon" });
  const [unknown] = readLearningRecords(
    [{ ...nativeRecord, knowledge_provider: null }],
    nativeRecord.envelope.context.repository_id,
  );
  expect(unknown).toMatchObject({ knowledge_provider: null });
});

it("refuses false known-empty versions, foreign scope and a private memory review response", () => {
  for (const value of [
    { ...nativeProjection, source_versions: [], version_state: "recorded" },
    { ...nativeProjection, workspace_id: "foreign-workspace" },
    {
      ...nativeProjection,
      operational_evidence: {
        ...nativeProjection.operational_evidence,
        canonical_memory: true,
      },
    },
    { ...nativeProjection, state: "review_required" },
  ])
    expect(() => readLearningProjection(value, nativeRecord.id)).toThrow();
});

it("binds native evidence to the selected local record before showing its scope", () => {
  const [record] = readLearningRecords(
    [nativeRecord],
    nativeRecord.envelope.context.repository_id,
  );
  const foreign = {
    ...nativeProjection,
    operational_evidence: {
      ...nativeProjection.operational_evidence,
      envelope: {
        ...nativeRecord.envelope,
        context: {
          ...nativeRecord.envelope.context,
          repository_id: "9".repeat(32),
        },
      },
    },
  };
  expect(() =>
    readLearningProjection(foreign, nativeRecord.id, record),
  ).toThrow();
});

const context = {
  base: "https://factory.example/api/v1/",
  actor: "f".repeat(32),
  repository: nativeRecord.envelope.context.repository_id,
  project: nativeRecord.envelope.context.project_id,
  run: nativeRecord.envelope.run_id,
  record: nativeRecord.id,
};
it("keeps the exact native body and scope in a separately named marker without overwriting intent", async () => {
  const projection = readLearningProjection(nativeProjection, nativeRecord.id);
  const marker = await native.makeNativeObservationMarker(
    context,
    projection,
    nativeProjection.source_id,
    "a".repeat(32),
  );
  const entries = new Map<string, string>();
  const storage = {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => {
      entries.set(key, value);
    },
  };
  await native.retainNativeObservation(storage, marker);
  expect(native.nativeObservationBody(marker)).toEqual({
    request_id: "a".repeat(32),
    source_id: "inventory-source",
  });
  expect(await native.exactNativeObservation(storage, marker)).toEqual(marker);
  await expect(
    native.retainNativeObservation(storage, marker),
  ).rejects.toThrow();
  const key = native.nativeObservationKey(context);
  entries.set(key, JSON.stringify({ ...marker, bodyDigest: "0".repeat(64) }));
  await expect(
    native.exactNativeObservation(storage, marker),
  ).rejects.toThrow();
});

it("confirms only the exact request, actor, source scope and native citation versions", async () => {
  const marker = await native.makeNativeObservationMarker(
    context,
    readLearningProjection(nativeProjection, nativeRecord.id),
    nativeProjection.source_id,
    "a".repeat(32),
  );
  const receipt = nativeReceipt(marker.requestId);
  expect(
    native.readNativeLearningReceipt(receipt, marker).source_versions,
  ).toEqual([nativeVersion]);
  for (const value of [
    { ...receipt, request_id: "b".repeat(32) },
    { ...receipt, actor_id: "e".repeat(32) },
    {
      ...receipt,
      citations: [
        { ...receipt.citations[0], source_version_id: "foreign-version" },
      ],
    },
    {
      ...receipt,
      citations: [{ ...receipt.citations[0], project_id: "foreign-project" }],
    },
    { ...receipt, kind: "projection_observation" },
    { ...receipt, nonce: "unsupported-private-field" },
  ])
    expect(() => native.readNativeLearningReceipt(value, marker)).toThrow();
});

it("never turns a native failure hint into a verified local outcome through receipt metadata", async () => {
  const marker = await native.makeNativeObservationMarker(
    { ...context, record: nativeFailureProjection.record_id },
    readLearningProjection(
      nativeFailureProjection,
      nativeFailureProjection.record_id,
    ),
    nativeFailureProjection.source_id,
    "a".repeat(32),
  );
  const value = nativeFailureReceipt(marker.requestId);
  expect(
    native.readNativeLearningReceipt(value, marker).current_record_state,
  ).toBe("candidate");
  expect(() =>
    native.readNativeLearningReceipt(
      { ...value, current_record_state: "verified" },
      marker,
    ),
  ).toThrow();
});
