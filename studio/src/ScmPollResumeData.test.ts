/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import packet from "../tests/scm-poll-resume-contract-fixture.json";
import type { PollingBindingReceipt } from "./ScmPollingData";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  resumeInput as signedInput,
  readResumeMarkers,
} from "./ScmResumeConsentData";
import {
  pollResumeInput,
  readPollResumeReceipt,
  readPollResumeHeads,
  readPollResumeMarkers,
  retainPollResumeMarker,
  pollResumeMarkerBinding,
  type PollResumeMarker,
} from "./ScmPollResumeData";
const scope = {
  base: "https://factory.example.test",
  actor: packet.receipt.administering_user_id,
  project: packet.binding.project_id,
  repository: packet.binding.repository_id,
};
const marker: PollResumeMarker = {
  ...scope,
  version: 3,
  requestId: packet.request.request_id,
  inputDigest: packet.receipt.request_digest,
  bindingRequestId: packet.binding.request.request_id,
  bindingDigest: packet.binding.digest,
  projectDigest: packet.binding.project_binding_digest,
  expectedHead: null,
};
class MemoryStorage {
  data = new Map<string, string>();
  get length() {
    return this.data.size;
  }
  key(i: number) {
    return [...this.data.keys()][i] ?? null;
  }
  getItem(k: string) {
    return this.data.get(k) ?? null;
  }
  setItem(k: string, v: string) {
    this.data.set(k, v);
  }
}
const locks = {
  async request<T>(
    _name: string,
    _options: { mode: "exclusive" },
    callback: () => T,
  ) {
    return callback();
  },
};
it("verifies the Python polling-only consent vector and current head page", async () => {
  expect(pollResumeInput(packet.request)).toEqual(packet.request);
  expect(
    await readPollResumeReceipt(
      packet.receipt,
      packet.binding as PollingBindingReceipt,
      scope,
      marker,
    ),
  ).toEqual(packet.receipt);
  expect((await readPollResumeHeads(packet.heads, scope)).items).toEqual([
    packet.binding,
  ]);
});
it("keeps signed consent and polling consent in separate strict contracts", () => {
  expect(() => signedInput(packet.request)).toThrow();
  for (const patch of [
    { version: 2 },
    { transport: "signed_webhook_v1" },
    { allowed_actions: ["resume", "publish"] },
    { acknowledge_same_run_resume: false },
    { component_ids: ["api", "api"] },
  ])
    expect(() => pollResumeInput({ ...packet.request, ...patch })).toThrow();
});
it("rejects resealed consent that differs from the retained original request", async () => {
  for (const patch of [
    { active: false },
    { expected_binding_digest: "a".repeat(64) },
    { component_ids: ["other"] },
  ]) {
    const value = structuredClone(packet.receipt);
    Object.assign(value.request, patch);
    value.request_digest = await attemptDigest(value.request);
    const { digest: _ignored, ...body } = value;
    void _ignored;
    value.digest = await attemptDigest(body);
    await expect(
      readPollResumeReceipt(
        value,
        packet.binding as PollingBindingReceipt,
        scope,
        marker,
      ),
    ).rejects.toThrow();
  }
  await expect(
    readPollResumeReceipt(
      packet.receipt,
      packet.binding as PollingBindingReceipt,
      { ...scope, project: "a".repeat(32) },
      marker,
    ),
  ).rejects.toThrow();
});
it("retains unknown polling grants across reload and blocks replacement without exact readback", async () => {
  const storage = new MemoryStorage();
  await retainPollResumeMarker(storage, marker, new Set(), locks, () => true);
  expect(readPollResumeMarkers(storage, scope)).toEqual([marker]);
  expect(readResumeMarkers(storage, scope)).toEqual([]);
  await expect(
    retainPollResumeMarker(
      storage,
      { ...marker, requestId: "a".repeat(32) },
      new Set(),
      locks,
      () => true,
    ),
  ).rejects.toThrow();
  const confirmed = new Set([pollResumeMarkerBinding(marker)]);
  storage.setItem(
    storage.key(0)!,
    JSON.stringify({ ...marker, bindingDigest: "c".repeat(64) }),
  );
  await expect(
    retainPollResumeMarker(
      storage,
      { ...marker, requestId: "b".repeat(32) },
      confirmed,
      locks,
      () => true,
    ),
  ).rejects.toThrow();
  expect(storage.length).toBe(1);
});
it("refuses foreign, duplicate or malformed current binding heads", async () => {
  for (const patch of [
    { project_id: "f".repeat(32) },
    { heads_only: false },
    { items: [packet.binding, packet.binding] },
    { next_after: "not-a-key" },
  ])
    await expect(
      readPollResumeHeads({ ...packet.heads, ...patch }, scope),
    ).rejects.toThrow();
});
