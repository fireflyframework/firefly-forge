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
  inputDigest: "e".repeat(64),
  previousDigest: null,
  projectDigest: "1".repeat(64),
  campaignDigest: "2".repeat(64),
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
  const modulePath = "./CampaignDeliveryPlanMarker";
  const api = await import(/* @vite-ignore */ modulePath).catch(() => null);
  expect(
    api,
    "Durable request-scoped reporting-plan identity storage is missing",
  ).not.toBeNull();
  const storage = new StorageFixture();
  expect(api!.retainDeliveryMarker(storage, marker, new Set())).toEqual(marker);
  const original = storage.getItem(api!.deliveryMarkerKey(marker));
  const next = {
    ...marker,
    requestId: "f".repeat(32),
    inputDigest: "9".repeat(64),
  };
  expect(() => api!.retainDeliveryMarker(storage, next, new Set())).toThrow();
  expect(
    api!.retainDeliveryMarker(
      storage,
      next,
      new Set([api!.deliveryMarkerBinding(marker)]),
    ),
  ).toEqual(next);
  expect(storage.getItem(api!.deliveryMarkerKey(marker))).toBe(original);
  expect(api!.readDeliveryMarkers(storage, context)).toEqual([marker, next]);
  expect(() =>
    api!.retainDeliveryMarker(
      storage,
      marker,
      new Set([
        api!.deliveryMarkerBinding(marker),
        api!.deliveryMarkerBinding(next),
      ]),
    ),
  ).toThrow();
});
it("a changed confirmed marker does not authorize another identity", async () => {
  const path = "./CampaignDeliveryPlanMarker";
  const api = await import(/* @vite-ignore */ path);
  const storage = new StorageFixture();
  api.retainDeliveryMarker(storage, marker, new Set());
  const confirmed = new Set([api.deliveryMarkerBinding(marker)]);
  for (const change of [
    { inputDigest: "9".repeat(64) },
    { previousDigest: "9".repeat(64) },
    { projectDigest: "9".repeat(64) },
    { campaignDigest: "9".repeat(64) },
  ]) {
    storage.setItem(
      api.deliveryMarkerKey(marker),
      JSON.stringify({ ...marker, ...change }),
    );
    expect(() =>
      api.retainDeliveryMarker(
        storage,
        { ...marker, requestId: "8".repeat(32) },
        confirmed,
      ),
    ).toThrow();
    expect(storage.length).toBe(1);
  }
});
it("storage failures and private body fields refuse dispatch without replacing a held identity", async () => {
  const path = "./CampaignDeliveryPlanMarker",
    api = await import(/* @vite-ignore */ path);
  const storage = new StorageFixture();
  expect(() =>
    api.retainDeliveryMarker(
      storage,
      { ...marker, token: "private-canary" },
      new Set(),
    ),
  ).toThrow();
  storage.getItem = () => null;
  expect(() => api.retainDeliveryMarker(storage, marker, new Set())).toThrow();
  const oversized = new StorageFixture();
  Object.defineProperty(oversized, "length", { get: () => 4097 });
  expect(() => api.readDeliveryMarkers(oversized, context)).toThrow();
});
it("rejects non-string hash bindings instead of coercing stored arrays", async () => {
  const path = "./CampaignDeliveryPlanMarker",
    api = await import(/* @vite-ignore */ path);
  for (const change of [
    { projectDigest: [marker.projectDigest] },
    { campaignDigest: [marker.campaignDigest] },
    { previousDigest: [marker.inputDigest] },
  ])
    expect(() =>
      api.retainDeliveryMarker(
        new StorageFixture(),
        { ...marker, ...change },
        new Set(),
      ),
    ).toThrow();
});
