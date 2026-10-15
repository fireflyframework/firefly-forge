/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
const context = {
  base: "https://factory.invalid/api/v1",
  actor: "a".repeat(32),
  project: "b".repeat(32),
  campaign: "c".repeat(32),
};
const marker = {
  ...context,
  version: 1,
  requestId: "d".repeat(32),
  reportDigest: "e".repeat(64),
};
class StorageFixture {
  values = new Map<string, string>();
  get length() {
    return this.values.size;
  }
  key(i: number) {
    return [...this.values.keys()][i] ?? null;
  }
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.values.set(key, value);
  }
}
it("retains each identity before dispatch and never overwrites an unresolved or confirmed request", async () => {
  const modulePath = "./CampaignReportArchiveMarker";
  const api = await import(/* @vite-ignore */ modulePath).catch(() => null);
  expect(
    api,
    "Durable request-scoped archive identity storage is missing",
  ).not.toBeNull();
  const storage = new StorageFixture();
  expect(api!.retainArchiveMarker(storage, marker, new Set())).toEqual(marker);
  const original = storage.getItem(api!.archiveMarkerKey(marker));
  const next = {
    ...marker,
    requestId: "f".repeat(32),
    reportDigest: "9".repeat(64),
  };
  expect(() => api!.retainArchiveMarker(storage, next, new Set())).toThrow();
  expect(
    api!.retainArchiveMarker(
      storage,
      next,
      new Set([api!.archiveMarkerBinding(marker)]),
    ),
  ).toEqual(next);
  expect(storage.getItem(api!.archiveMarkerKey(marker))).toBe(original);
  expect(api!.readArchiveMarkers(storage, context)).toEqual([marker, next]);
  expect(() =>
    api!.retainArchiveMarker(
      storage,
      marker,
      new Set([
        api!.archiveMarkerBinding(marker),
        api!.archiveMarkerBinding(next),
      ]),
    ),
  ).toThrow();
});
it("rejects malformed context, oversized enumeration, private body fields and failed readback", async () => {
  const modulePath = "./CampaignReportArchiveMarker";
  const api = await import(/* @vite-ignore */ modulePath).catch(() => null);
  expect(api).not.toBeNull();
  for (const change of [
    { base: "https://user:secret@factory.invalid" },
    { actor: "foreign" },
    { reportDigest: "bad" },
    { token: "private-canary" },
    { report: { private: "private-canary" } },
  ]) {
    expect(() =>
      api!.retainArchiveMarker(
        new StorageFixture(),
        { ...marker, ...change },
        new Set(),
      ),
    ).toThrow();
  }
  const blocked = new StorageFixture();
  blocked.getItem = () => null;
  expect(() => api!.retainArchiveMarker(blocked, marker, new Set())).toThrow();
  const oversized = new StorageFixture();
  Object.defineProperty(oversized, "length", { get: () => 4097 });
  expect(() => api!.readArchiveMarkers(oversized, context)).toThrow();
});
it("does not treat an identity stored under this context as foreign ignorable data", async () => {
  const modulePath = "./CampaignReportArchiveMarker";
  const api = await import(/* @vite-ignore */ modulePath).catch(() => null);
  expect(api).not.toBeNull();
  const storage = new StorageFixture();
  storage.setItem(
    api!.archiveMarkerKey(marker),
    JSON.stringify({ ...marker, actor: "f".repeat(32) }),
  );
  expect(() => api!.readArchiveMarkers(storage, context)).toThrow();
  expect(storage.length).toBe(1);
});
it("does not reuse a confirmation after its persisted digest changes", async () => {
  const modulePath = "./CampaignReportArchiveMarker";
  const api = await import(/* @vite-ignore */ modulePath);
  const storage = new StorageFixture();
  api.retainArchiveMarker(storage, marker, new Set());
  const confirmed = new Set([api.archiveMarkerBinding(marker)]);
  const changed = { ...marker, reportDigest: "9".repeat(64) };
  const raw = JSON.stringify(changed);
  storage.setItem(api.archiveMarkerKey(marker), raw);
  expect(() =>
    api.retainArchiveMarker(
      storage,
      { ...marker, requestId: "f".repeat(32) },
      confirmed,
    ),
  ).toThrow();
  expect(storage.length).toBe(1);
  expect(storage.getItem(api.archiveMarkerKey(marker))).toBe(raw);
});
