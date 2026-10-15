/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  delegationMarkerBinding,
  readDelegationMarkers,
  retainDelegationMarker,
  type DelegationMarker,
} from "./ScmDelegationData";

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
const marker: DelegationMarker = {
  version: 1,
  base: "https://factory.example.test/api/v1",
  actor: "a".repeat(32),
  project: "b".repeat(32),
  repository: "c".repeat(32),
  requestId: "d".repeat(32),
  inputDigest: "e".repeat(64),
};

describe("retained SCM delegation identity", () => {
  it("retains only the exact scope and input digest, with synchronous readback", () => {
    const storage = new MemoryStorage();
    expect(retainDelegationMarker(storage, marker, new Set())).toEqual(marker);
    expect(readDelegationMarkers(storage, marker)).toEqual([marker]);
    expect([...storage.data.values()]).toEqual([JSON.stringify(marker)]);
  });
  it("holds an unresolved identity instead of replacing or replaying it", () => {
    const storage = new MemoryStorage();
    retainDelegationMarker(storage, marker, new Set());
    expect(() =>
      retainDelegationMarker(
        storage,
        { ...marker, requestId: "f".repeat(32) },
        new Set(),
      ),
    ).toThrow();
    expect(readDelegationMarkers(storage, marker)).toEqual([marker]);
    expect(storage.length).toBe(1);
  });
  it("a changed confirmed digest cannot unlock another request", () => {
    const storage = new MemoryStorage();
    retainDelegationMarker(storage, marker, new Set());
    const confirmed = new Set([delegationMarkerBinding(marker)]);
    const key = storage.key(0)!;
    const changed = { ...marker, inputDigest: "f".repeat(64) };
    storage.setItem(key, JSON.stringify(changed));
    expect(() =>
      retainDelegationMarker(
        storage,
        { ...marker, requestId: "f".repeat(32) },
        confirmed,
      ),
    ).toThrow();
    expect(readDelegationMarkers(storage, marker)).toEqual([changed]);
    expect(storage.length).toBe(1);
  });
  it("a confirmed complete binding allows a new explicit identity and keeps the old one", () => {
    const storage = new MemoryStorage();
    retainDelegationMarker(storage, marker, new Set());
    const next = { ...marker, requestId: "f".repeat(32) };
    retainDelegationMarker(
      storage,
      next,
      new Set([delegationMarkerBinding(marker)]),
    );
    expect(readDelegationMarkers(storage, marker)).toEqual([marker, next]);
  });
  it("isolates API, actor, project and repository contexts including origin-root APIs", () => {
    const storage = new MemoryStorage();
    retainDelegationMarker(
      storage,
      { ...marker, base: "https://factory.example.test" },
      new Set(),
    );
    for (const patch of [
      { base: marker.base },
      { base: "https://other.example.test" },
      { actor: "f".repeat(32) },
      { project: "f".repeat(32) },
      { repository: "f".repeat(32) },
    ]) {
      const scope = {
        ...marker,
        base: "https://factory.example.test",
        ...patch,
      };
      expect(readDelegationMarkers(storage, scope)).toEqual([]);
    }
  });
  it("refuses storage read/write uncertainty and corrupt retained records", () => {
    const storage = new MemoryStorage();
    const refusing = {
      ...storage,
      length: 0,
      key: () => null,
      getItem: () => null,
      setItem: () => {},
    };
    expect(() => retainDelegationMarker(refusing, marker, new Set())).toThrow();
    retainDelegationMarker(storage, marker, new Set());
    storage.setItem(storage.key(0)!, "not-json");
    expect(() => readDelegationMarkers(storage, marker)).toThrow();
    expect(storage.length).toBe(1);
  });
});

import fixture from "../tests/scm-delegation-contract-fixture.json";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  readDelegationReceipt,
  readDelegationCandidates,
  readDelegationHeads,
} from "./ScmDelegationData";
const scope = {
  project: fixture.receipt.project_id,
  repository: fixture.receipt.repository_id,
};
const expected = {
  ...scope,
  requestId: fixture.request.request_id,
  actor: fixture.receipt.administering_user_id,
  inputDigest: fixture.receipt.request_digest,
};
async function seal(row: Record<string, unknown>) {
  const { digest: ignored, ...body } = row;
  void ignored;
  return { ...body, digest: await attemptDigest(body) };
}
describe("strict Python-modeled delegation receipts", () => {
  it("accepts the actual Python canonical vectors and exact recorded receipt", async () => {
    expect(await attemptDigest(fixture.unicode_vector.input)).toBe(
      fixture.unicode_vector.digest,
    );
    expect(await readDelegationReceipt(fixture.receipt, expected)).toEqual(
      fixture.receipt,
    );
  });
  it("allows authorized historical reads without pretending the reader was the original administrator", async () => {
    expect(await readDelegationReceipt(fixture.receipt, scope)).toEqual(
      fixture.receipt,
    );
  });
  it.each(["administering_user_id", "project_id", "repository_id"] as const)(
    "refuses a validly resealed foreign %s",
    async (key) => {
      const row = await seal({ ...fixture.receipt, [key]: "9".repeat(32) });
      await expect(readDelegationReceipt(row, expected)).rejects.toThrow();
    },
  );
  it("refuses a changed request even when its envelope and request hashes are recomputed", async () => {
    const request = { ...fixture.request, allowed_actions: ["cancel"] };
    const row = await seal({
      ...fixture.receipt,
      request,
      request_digest: await attemptDigest(request),
    });
    await expect(readDelegationReceipt(row, expected)).rejects.toThrow();
  });
  it("refuses inconsistent project, verified-reference and binding-key causal anchors", async () => {
    for (const patch of [
      { project_binding_digest: "9".repeat(64) },
      { binding_key: "9".repeat(64) },
      {
        verified_reference: {
          ...fixture.receipt.verified_reference,
          payload_digest: "9".repeat(64),
        },
      },
    ])
      await expect(
        readDelegationReceipt(
          await seal({ ...fixture.receipt, ...patch }),
          scope,
        ),
      ).rejects.toThrow();
  });
  it("rejects extra fields, nonliteral acknowledgment and oversized envelopes", async () => {
    for (const row of [
      { ...fixture.receipt, additional: true },
      {
        ...fixture.receipt,
        request: {
          ...fixture.request,
          acknowledge_webhook_asserted_identity: 1,
        },
      },
      { ...fixture.receipt, prepared_at: "x".repeat(65536) },
    ])
      await expect(readDelegationReceipt(row, scope)).rejects.toThrow();
  });
  it("verifies signed-reference candidate hashes and the reviewed stored-project cut", async () => {
    expect(await readDelegationCandidates(fixture.candidates, scope)).toEqual(
      fixture.candidates,
    );
    const row = structuredClone(fixture.candidates);
    row.items[0].verified_reference.payload_digest = "9".repeat(64);
    await expect(readDelegationCandidates(row, scope)).rejects.toThrow();
  });
  it("treats only explicit current-head metadata as current, not arbitrary history order", async () => {
    expect(await readDelegationHeads(fixture.current_heads, scope)).toEqual(
      fixture.current_heads,
    );
    await expect(
      readDelegationHeads(
        { ...fixture.current_heads, heads_only: false },
        scope,
      ),
    ).rejects.toThrow();
    await expect(
      readDelegationHeads(
        {
          ...fixture.current_heads,
          items: [{ ...fixture.current_heads.items[0], current_head: false }],
        },
        scope,
      ),
    ).rejects.toThrow();
  });
  it("rejects modified summaries, duplicate heads and unusable cursors", async () => {
    for (const patch of [
      {
        items: [
          { ...fixture.current_heads.items[0], native_user_id: "9".repeat(32) },
        ],
      },
      {
        items: [fixture.current_heads.items[0], fixture.current_heads.items[0]],
      },
      { next_cursor: "9".repeat(32) },
    ])
      await expect(
        readDelegationHeads({ ...fixture.current_heads, ...patch }, scope),
      ).rejects.toThrow();
  });
});
