/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { InvalidationContext } from "./LearningRecordData";
import {
  observationKey,
  readObservationMarker,
  type ObservationMarker,
} from "./LearningObservationData";

export type AttemptMarker = Omit<ObservationMarker, "version" | "source"> & {
  version: 2;
  inputDigest: string;
  sourceDigest: string;
};
export type AttemptReceipt = {
  version: 2;
  attempt_id: string;
  record_id: string;
  repository_id: string;
  project_id: string | null;
  run_id: string;
  actor_id: string;
  input_digest: string;
  source_digest: string;
  projection_id: string;
  submission_request_id: string;
  workspace_id: string;
  target_revision: string;
  payload_digest: string;
  content_sha256: string;
  state: "preparing" | "dispatched" | "not_dispatched" | "verified" | "unknown";
  disposition: "local_admission_refused" | "verified" | null;
  created_at: string;
  finished_at: string | null;
  observation: { event_id: string; request_id: string; digest: string } | null;
  digest: string;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const problem = () =>
  new Error(
    "The exact attempt receipt could not be confirmed. Its identity is retained.",
  );
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw problem();
  return value as Record<string, unknown>;
}
function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  const row = object(value);
  return (
    "{" +
    Object.keys(row)
      .sort()
      .map((key) => JSON.stringify(key) + ":" + canonical(row[key]))
      .join(",") +
    "}"
  );
}
export async function attemptDigest(value: unknown): Promise<string> {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonical(value)),
  );
  return [...new Uint8Array(bytes)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}
export function attemptInput(marker: ObservationMarker) {
  return {
    attempt_id: "obs2_" + marker.requestId,
    source_id: marker.source,
    projection_id: marker.projection,
    submission_request_id: marker.submissionRequest,
    workspace_id: marker.workspace,
    target_revision: marker.targetRevision,
    payload_digest: marker.payloadDigest,
    content_sha256: marker.contentHash,
  };
}
export function attemptKey(context: InvalidationContext): string {
  return observationKey(context).replace(".v1:", ".v2:");
}
export function readAttemptMarker(
  value: string | null,
  context: InvalidationContext,
): AttemptMarker | null {
  if (value === null) return null;
  try {
    if (value.length > 10000) throw problem();
    const row = object(JSON.parse(value));
    if (
      Object.keys(row).sort().join(",") !==
      "actor,base,contentHash,inputDigest,payloadDigest,project,projection,record,repository,requestId,run,sourceDigest,submissionRequest,targetRevision,version,workspace"
    )
      throw problem();
    const { inputDigest, sourceDigest, ...rest } = row;
    if (
      row.version !== 2 ||
      typeof inputDigest !== "string" ||
      !hash.test(inputDigest) ||
      typeof sourceDigest !== "string" ||
      !hash.test(sourceDigest)
    )
      throw problem();
    const marker = readObservationMarker(
      JSON.stringify({ ...rest, version: 1, source: "digest-only" }),
      context,
    );
    if (!marker) throw problem();
    const safe = { ...marker };
    Reflect.deleteProperty(safe, "source");
    return { ...safe, version: 2, inputDigest, sourceDigest };
  } catch {
    throw problem();
  }
}
export function legacyShape(
  marker: AttemptMarker,
  source: string,
): ObservationMarker {
  const original = { ...marker, version: 1 as const, source };
  Reflect.deleteProperty(original, "inputDigest");
  Reflect.deleteProperty(original, "sourceDigest");
  return original;
}
export async function retainAttempt(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: ObservationMarker,
): Promise<AttemptMarker> {
  const key = attemptKey(marker);
  if (
    storage.getItem(key) !== null ||
    storage.getItem(observationKey(marker)) !== null
  )
    throw problem();
  readObservationMarker(JSON.stringify(marker), marker);
  const { source, ...safe } = marker;
  const packet: AttemptMarker = {
    ...safe,
    version: 2,
    inputDigest: await attemptDigest(attemptInput(marker)),
    sourceDigest: await attemptDigest({ source_id: source }),
  };
  const saved = JSON.stringify(packet);
  readAttemptMarker(saved, marker);
  if (
    storage.getItem(key) !== null ||
    storage.getItem(observationKey(marker)) !== null
  )
    throw problem();
  storage.setItem(key, saved);
  if (storage.getItem(key) !== saved) throw problem();
  return packet;
}
export async function exactAttemptMarker(
  storage: Pick<Storage, "getItem">,
  marker: AttemptMarker,
): Promise<AttemptMarker> {
  if (storage.getItem(observationKey(marker)) !== null) throw problem();
  const saved = readAttemptMarker(storage.getItem(attemptKey(marker)), marker);
  if (!saved || canonical(saved) !== canonical(marker)) throw problem();
  return saved;
}
export async function readAttemptReceipt(
  value: unknown,
  marker: AttemptMarker,
): Promise<AttemptReceipt> {
  readAttemptMarker(JSON.stringify(marker), marker);
  const row = object(value);
  if (
    Object.keys(row).sort().join(",") !==
      "actor_id,attempt_id,content_sha256,created_at,digest,disposition,finished_at,input_digest,observation,payload_digest,project_id,projection_id,record_id,repository_id,run_id,source_digest,state,submission_request_id,target_revision,version,workspace_id" ||
    row.version !== 2 ||
    row.attempt_id !== "obs2_" + marker.requestId ||
    row.record_id !== marker.record ||
    row.repository_id !== marker.repository ||
    row.project_id !== marker.project ||
    row.run_id !== marker.run ||
    row.actor_id !== marker.actor ||
    row.source_digest !== marker.sourceDigest ||
    row.input_digest !== marker.inputDigest ||
    row.projection_id !== marker.projection ||
    row.submission_request_id !== marker.submissionRequest ||
    row.workspace_id !== marker.workspace ||
    row.target_revision !== marker.targetRevision ||
    row.payload_digest !== marker.payloadDigest ||
    row.content_sha256 !== marker.contentHash ||
    typeof row.digest !== "string" ||
    !hash.test(row.digest) ||
    typeof row.created_at !== "string" ||
    row.created_at.length > 64 ||
    !Number.isFinite(Date.parse(row.created_at)) ||
    !/(Z|[+-]\d{2}:\d{2})$/.test(row.created_at) ||
    ![
      "preparing",
      "dispatched",
      "not_dispatched",
      "verified",
      "unknown",
    ].includes(String(row.state))
  )
    throw problem();
  const { digest, ...unsigned } = row;
  if (digest !== (await attemptDigest(unsigned))) throw problem();
  const terminal = ["not_dispatched", "verified", "unknown"].includes(
    String(row.state),
  );
  if (
    terminal
      ? typeof row.finished_at !== "string" ||
        row.finished_at.length > 64 ||
        !Number.isFinite(Date.parse(row.finished_at)) ||
        !/(Z|[+-]\d{2}:\d{2})$/.test(row.finished_at)
      : row.finished_at !== null
  )
    throw problem();
  if (row.state === "verified") {
    const event = object(row.observation);
    if (
      row.disposition !== "verified" ||
      Object.keys(event).sort().join(",") !== "digest,event_id,request_id" ||
      typeof event.event_id !== "string" ||
      !id.test(event.event_id) ||
      typeof event.request_id !== "string" ||
      !id.test(event.request_id) ||
      typeof event.digest !== "string" ||
      !hash.test(event.digest)
    )
      throw problem();
  } else if (
    row.observation !== null ||
    row.disposition !==
      (row.state === "not_dispatched" ? "local_admission_refused" : null)
  )
    throw problem();
  return row as AttemptReceipt;
}
export async function archiveRefusedAttempt(
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  marker: AttemptMarker,
  receipt: AttemptReceipt,
): Promise<void> {
  const retained = await exactAttemptMarker(storage, marker);
  const confirmed = await readAttemptReceipt(receipt, retained);
  if (confirmed.state !== "not_dispatched") throw problem();
  const key = attemptKey(marker),
    archive = key + ":history:" + confirmed.attempt_id;
  const packet = JSON.stringify({ marker: retained, receipt: confirmed });
  if (storage.getItem(archive) !== null && storage.getItem(archive) !== packet)
    throw problem();
  storage.setItem(archive, packet);
  if (storage.getItem(archive) !== packet) throw problem();
  await exactAttemptMarker(storage, marker);
  storage.removeItem(key);
  if (storage.getItem(key) !== null) throw problem();
}
