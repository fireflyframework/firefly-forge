/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type DeliveryContext = {
  base: string;
  actor: string;
  project: string;
  campaign: string;
};
export type DeliveryMarker = DeliveryContext & {
  version: 1;
  requestId: string;
  inputDigest: string;
  previousDigest: string | null;
  projectDigest: string;
  campaignDigest: string;
};
type MarkerStorage = Pick<Storage, "length" | "key" | "getItem" | "setItem">;
const id = /^[a-f0-9]{32}$/;
const hash = /^[a-f0-9]{64}$/;
const error = () =>
  new Error(
    "The reporting-plan request identity could not be retained or read. Keep the original request for review; do not send another plan save.",
  );
function context(value: DeliveryContext) {
  let url: URL;
  try {
    url = new URL(value.base);
  } catch {
    throw error();
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.href.replace(/\/$/, "") !== value.base ||
    ![value.actor, value.project, value.campaign].every(
      (v) => typeof v === "string" && id.test(v),
    )
  )
    throw error();
  return `forge.campaign-delivery-plan.v1:${encodeURIComponent(value.base)}:${value.actor}:${value.project}:${value.campaign}:`;
}
function read(value: unknown): DeliveryMarker {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw error();
  const item = value as DeliveryMarker;
  if (
    Object.keys(item).sort().join(",") !==
      "actor,base,campaign,campaignDigest,inputDigest,previousDigest,project,projectDigest,requestId,version" ||
    item.version !== 1 ||
    typeof item.requestId !== "string" ||
    !id.test(item.requestId) ||
    typeof item.inputDigest !== "string" ||
    !hash.test(item.inputDigest) ||
    typeof item.projectDigest !== "string" ||
    !hash.test(item.projectDigest) ||
    typeof item.campaignDigest !== "string" ||
    !hash.test(item.campaignDigest) ||
    (item.previousDigest !== null &&
      (typeof item.previousDigest !== "string" ||
        !hash.test(item.previousDigest)))
  )
    throw error();
  context(item);
  return item;
}
export function deliveryMarkerKey(marker: DeliveryMarker): string {
  read(marker);
  return context(marker) + marker.requestId;
}
export function deliveryMarkerBinding(marker: DeliveryMarker): string {
  return (
    deliveryMarkerKey(marker) +
    ":" +
    JSON.stringify([
      marker.inputDigest,
      marker.previousDigest,
      marker.projectDigest,
      marker.campaignDigest,
    ])
  );
}
export function readDeliveryMarkers(
  storage: MarkerStorage,
  scope: DeliveryContext,
): DeliveryMarker[] {
  const prefix = context(scope);
  if (storage.length > 4096) throw error();
  const result: DeliveryMarker[] = [];
  for (let i = 0; i < storage.length; i++) {
    const key = storage.key(i);
    if (!key?.startsWith(prefix)) continue;
    const raw = storage.getItem(key);
    let item: DeliveryMarker;
    try {
      item = read(JSON.parse(raw ?? "null"));
    } catch {
      throw error();
    }
    if (deliveryMarkerKey(item) !== key || context(item) !== prefix)
      throw error();
    result.push(item);
    if (result.length > 50) throw error();
  }
  return result;
}
export function retainDeliveryMarker(
  storage: MarkerStorage,
  value: DeliveryMarker,
  confirmedBindings: ReadonlySet<string>,
): DeliveryMarker {
  const marker = read(value);
  const existing = readDeliveryMarkers(storage, marker);
  const key = deliveryMarkerKey(marker);
  if (
    existing.length >= 50 ||
    existing.some(
      (item) => !confirmedBindings.has(deliveryMarkerBinding(item)),
    ) ||
    storage.getItem(key) !== null
  )
    throw error();
  const raw = JSON.stringify(marker);
  storage.setItem(key, raw);
  if (storage.getItem(key) !== raw) throw error();
  return read(JSON.parse(raw));
}
