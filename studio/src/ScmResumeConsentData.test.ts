/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import packet from "../tests/scm-resume-contract-fixture.json";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  readResumeReceipt,
  readResumeMarkers,
  resumeGrantReference,
  resumeInput,
  resumeMarkerBinding,
  retainResumeMarker,
  type ResumeLocks,
  type ResumeMarker,
  type ResumeReceipt,
} from "./ScmResumeConsentData";
import type { DelegationReceipt } from "./ScmDelegationData";
const binding = packet.binding as DelegationReceipt;
const scope = {
  base: "https://factory.example.test",
  actor: "f".repeat(32),
  project: binding.project_id,
  repository: binding.repository_id,
};
const marker: ResumeMarker = {
  ...scope,
  version: 2,
  requestId: packet.request.request_id,
  inputDigest: packet.receipt.request_digest,
  bindingRequestId: binding.request.request_id,
  bindingDigest: binding.digest,
  projectDigest: binding.project_binding_digest,
  expectedHead: null,
};
class MemoryStorage {
  data = new Map<string, string>();
  get length() {
    return this.data.size;
  }
  key(index: number) {
    return [...this.data.keys()][index] ?? null;
  }
  getItem(key: string) {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.data.set(key, value);
  }
}
function coordination(): ResumeLocks {
  let tail: Promise<unknown> = Promise.resolve();
  return {
    request<T>(
      _name: string,
      _options: { mode: "exclusive" },
      callback: () => T,
    ) {
      const next = tail.then(callback);
      tail = next.catch(() => {});
      return next;
    },
  };
}
async function reseal(value: ResumeReceipt) {
  value.request_digest = await attemptDigest(value.request);
  const { digest: ignored, ...rest } = value;
  void ignored;
  value.digest = await attemptDigest(rest);
  return value;
}
describe("separate V2 consent", () => {
  it("verifies the actual Python-model vector and its immutable V1 scope", async () => {
    expect(resumeInput(packet.request)).toEqual(packet.request);
    expect(resumeGrantReference(packet.receipt)).toEqual({
      requestId: binding.request.request_id,
      digest: binding.digest,
    });
    expect(
      await readResumeReceipt(packet.receipt, binding, scope, marker),
    ).toEqual(packet.receipt);
  });
  it.each([
    "active",
    "component",
    "binding",
    "head",
    "actor",
    "request",
    "unknown",
  ])("rejects a validly resealed mismatched %s receipt", async (change) => {
    const value = structuredClone(packet.receipt) as ResumeReceipt;
    if (change === "active") value.request.active = false;
    if (change === "component") value.request.component_ids = ["other"];
    if (change === "binding")
      value.request.expected_binding_digest = "a".repeat(64);
    if (change === "head") {
      value.request.expected_previous_digest = "a".repeat(64);
      value.previous_digest = "a".repeat(64);
      value.revision = 2;
    }
    if (change === "actor") value.administering_user_id = "a".repeat(32);
    if (change === "request") value.request.request_id = "a".repeat(32);
    if (change === "unknown") Object.assign(value, { unexpected: true });
    await expect(
      readResumeReceipt(await reseal(value), binding, scope, marker),
    ).rejects.toThrow();
  });
  it("can read a prior administrator's inactive receipt without claiming current execution authority", async () => {
    const value = structuredClone(packet.receipt) as ResumeReceipt;
    value.request.active = false;
    value.administering_user_id = "a".repeat(32);
    expect(
      await readResumeReceipt(await reseal(value), binding, scope),
    ).toEqual(value);
  });
  it("refuses unknown inputs, unacknowledged consent and capabilities outside resume", () => {
    for (const patch of [
      { allowed_actions: ["resume", "publish"] },
      { acknowledge_same_run_resume: false },
      { agent: "bridge" },
      { component_ids: ["api", "api"] },
    ])
      expect(() => resumeInput({ ...packet.request, ...patch })).toThrow();
  });
  it("retains minimal complete scope and holds an unknown request across reload", async () => {
    const storage = new MemoryStorage();
    expect(
      await retainResumeMarker(
        storage,
        marker,
        new Set(),
        coordination(),
        () => true,
      ),
    ).toEqual(marker);
    expect(readResumeMarkers(storage, scope)).toEqual([marker]);
    await expect(
      retainResumeMarker(
        storage,
        { ...marker, requestId: "a".repeat(32) },
        new Set(),
        coordination(),
        () => true,
      ),
    ).rejects.toThrow();
    expect(storage.length).toBe(1);
  });
  it("changed confirmed storage cannot authorize a replacement request", async () => {
    const storage = new MemoryStorage(),
      locks = coordination();
    await retainResumeMarker(storage, marker, new Set(), locks, () => true);
    const confirmed = new Set([resumeMarkerBinding(marker)]);
    storage.setItem(
      storage.key(0)!,
      JSON.stringify({ ...marker, bindingDigest: "a".repeat(64) }),
    );
    await expect(
      retainResumeMarker(
        storage,
        { ...marker, requestId: "b".repeat(32) },
        confirmed,
        locks,
        () => true,
      ),
    ).rejects.toThrow();
    expect(storage.length).toBe(1);
  });
  it("two tabs share one retained request and the contender is held", async () => {
    const storage = new MemoryStorage(),
      locks = coordination();
    const outcomes = await Promise.allSettled(
      [marker, { ...marker, requestId: "a".repeat(32) }].map((value) =>
        retainResumeMarker(storage, value, new Set(), locks, () => true),
      ),
    );
    expect(outcomes.map((value) => value.status)).toEqual([
      "fulfilled",
      "rejected",
    ]);
    expect(storage.length).toBe(1);
  });
  it("a new explicit request preserves the fully confirmed original", async () => {
    const storage = new MemoryStorage(),
      locks = coordination();
    await retainResumeMarker(storage, marker, new Set(), locks, () => true);
    const next = { ...marker, requestId: "b".repeat(32) };
    await retainResumeMarker(
      storage,
      next,
      new Set([resumeMarkerBinding(marker)]),
      locks,
      () => true,
    );
    expect(readResumeMarkers(storage, scope)).toEqual([marker, next]);
  });
  it("a queued tab whose context changed cannot retain a new request", async () => {
    const storage = new MemoryStorage();
    let release!: () => void;
    const wait = new Promise<void>((resolve) => {
      release = resolve;
    });
    const locks: ResumeLocks = {
      async request<T>(
        _name: string,
        _options: { mode: "exclusive" },
        callback: () => T,
      ) {
        await wait;
        return callback();
      },
    };
    let current = true;
    const pending = retainResumeMarker(
      storage,
      marker,
      new Set(),
      locks,
      () => current,
    );
    current = false;
    release();
    await expect(pending).rejects.toThrow();
    expect(storage.length).toBe(0);
  });
  it("origin-root APIs and other actors stay in separate storage contexts", async () => {
    const storage = new MemoryStorage();
    await retainResumeMarker(
      storage,
      marker,
      new Set(),
      coordination(),
      () => true,
    );
    expect(
      readResumeMarkers(storage, { ...scope, actor: "b".repeat(32) }),
    ).toEqual([]);
    expect(
      readResumeMarkers(storage, { ...scope, base: scope.base + "/api/v1" }),
    ).toEqual([]);
    expect(readResumeMarkers(storage, scope)).toEqual([marker]);
  });
  it("missing coordination, stale context and unreadable storage all refuse before a write", async () => {
    const storage = new MemoryStorage();
    await expect(
      retainResumeMarker(storage, marker, new Set(), undefined, () => true),
    ).rejects.toThrow();
    await expect(
      retainResumeMarker(
        storage,
        marker,
        new Set(),
        coordination(),
        () => false,
      ),
    ).rejects.toThrow();
    await expect(
      retainResumeMarker(
        {
          ...storage,
          length: 0,
          key: () => null,
          getItem: () => null,
          setItem: () => {},
        },
        marker,
        new Set(),
        coordination(),
        () => true,
      ),
    ).rejects.toThrow();
    expect(storage.length).toBe(0);
  });
});
