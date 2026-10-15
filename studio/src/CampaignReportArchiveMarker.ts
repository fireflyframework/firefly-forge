/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type ArchiveContext = {
  base: string;
  actor: string;
  project: string;
  campaign: string;
};
export type ArchiveMarker = ArchiveContext & {
  version: 1;
  requestId: string;
  reportDigest: string;
};
type MarkerStorage = Pick<Storage, "length" | "key" | "getItem" | "setItem">;
const id = /^[a-f0-9]{32}$/;
const hash = /^[a-f0-9]{64}$/;
const error = () =>
  new Error(
    "The saved request identity could not be retained or read. Keep the original request for review; do not send another save.",
  );
function context(value: ArchiveContext) {
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
  return `forge.campaign-report-archive.v1:${encodeURIComponent(value.base)}:${value.actor}:${value.project}:${value.campaign}:`;
}
function read(value: unknown): ArchiveMarker {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw error();
  const item = value as ArchiveMarker;
  if (
    Object.keys(item).sort().join(",") !==
      "actor,base,campaign,project,reportDigest,requestId,version" ||
    item.version !== 1 ||
    typeof item.requestId !== "string" ||
    !id.test(item.requestId) ||
    typeof item.reportDigest !== "string" ||
    !hash.test(item.reportDigest)
  )
    throw error();
  context(item);
  return item;
}
export function archiveMarkerKey(marker: ArchiveMarker): string {
  read(marker);
  return context(marker) + marker.requestId;
}
export function readArchiveMarkers(
  storage: MarkerStorage,
  scope: ArchiveContext,
): ArchiveMarker[] {
  const prefix = context(scope);
  if (storage.length > 4096) throw error();
  const result: ArchiveMarker[] = [];
  for (let i = 0; i < storage.length; i++) {
    const key = storage.key(i);
    if (!key?.startsWith(prefix)) continue;
    const raw = storage.getItem(key);
    let item: ArchiveMarker;
    try {
      item = read(JSON.parse(raw ?? "null"));
    } catch {
      throw error();
    }
    if (archiveMarkerKey(item) !== key || context(item) !== prefix)
      throw error();
    result.push(item);
    if (result.length > 100) throw error();
  }
  return result;
}
export function retainArchiveMarker(
  storage: MarkerStorage,
  value: ArchiveMarker,
  confirmedIds: ReadonlySet<string>,
): ArchiveMarker {
  const marker = read(value);
  const existing = readArchiveMarkers(storage, marker);
  const key = archiveMarkerKey(marker);
  if (
    existing.length >= 100 ||
    existing.some((item) => !confirmedIds.has(item.requestId)) ||
    storage.getItem(key) !== null
  )
    throw error();
  const raw = JSON.stringify(marker);
  storage.setItem(key, raw);
  if (storage.getItem(key) !== raw) throw error();
  return read(JSON.parse(raw));
}
