/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { InvalidationContext, LearningRecord } from "./LearningRecordData";
export type ObservationMarker = InvalidationContext & {
  version: 1;
  requestId: string;
  projection: string;
  submissionRequest: string;
  workspace: string;
  source: string;
  targetRevision: string;
  payloadDigest: string;
  contentHash: string;
};
export type LearningObservationReceipt = {
  event_id: string;
  record_id: string;
  repository_id: string;
  run_id: string;
  request_id: string;
  kind: "projection_observation";
  event_state: "verified";
  current_record_state: LearningRecord["state"];
  current_projection_state:
    | "pending"
    | "executing"
    | "source_admitted"
    | "verified"
    | "unknown"
    | "abandoned";
  actor_id: string;
  projection_id: string;
  submission_request_id: string;
  workspace_id: string;
  source_id: string;
  target_revision: string;
  payload_digest: string;
  content_sha256: string;
  citations: { source_id: string; chunk_id: string }[];
  created_at: string;
  sequence: number;
  digest: string;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/,
  source = /^[A-Za-z0-9._-]{1,200}$/,
  chunk = /^[A-Za-z0-9:._-]{1,200}$/;
const problem = () =>
  new Error(
    "The original observation could not be confirmed. Its identity is retained; no request was repeated.",
  );
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw problem();
  return value as Record<string, unknown>;
}
function text(value: unknown, pattern: RegExp): string {
  if (typeof value !== "string" || !pattern.test(value)) throw problem();
  return value;
}
function choice<T extends string>(value: unknown, options: readonly T[]): T {
  if (typeof value !== "string" || !options.includes(value as T))
    throw problem();
  return value as T;
}
export function observationKey(context: InvalidationContext): string {
  return `forge.learning-observation.v1:${encodeURIComponent(context.base)}:${context.actor}:${context.repository}:${context.record}`;
}
export function readObservationMarker(
  value: string | null,
  context: InvalidationContext,
): ObservationMarker | null {
  if (value === null) return null;
  try {
    if (value.length > 10000) throw problem();
    const row = object(JSON.parse(value));
    if (
      Object.keys(row).sort().join(",") !==
        "actor,base,contentHash,payloadDigest,project,projection,record,repository,requestId,run,source,submissionRequest,targetRevision,version,workspace" ||
      row.version !== 1
    )
      throw problem();
    for (const field of [
      "base",
      "actor",
      "repository",
      "project",
      "run",
      "record",
    ] as const) {
      if (row[field] !== context[field]) throw problem();
      if (field !== "base" && field !== "project") text(row[field], id);
    }
    if (context.project !== null) text(context.project, id);
    const base = new URL(context.base);
    if (
      !["http:", "https:"].includes(base.protocol) ||
      base.username ||
      base.password ||
      base.search ||
      base.hash ||
      context.base.length > 2048
    )
      throw problem();
    return {
      ...context,
      version: 1,
      requestId: text(row.requestId, id),
      projection: text(row.projection, id),
      submissionRequest: text(row.submissionRequest, id),
      workspace: text(row.workspace, source),
      source: text(row.source, source),
      targetRevision: text(row.targetRevision, id),
      payloadDigest: text(row.payloadDigest, hash),
      contentHash: text(row.contentHash, hash),
    };
  } catch {
    throw problem();
  }
}
export function retainObservation(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: ObservationMarker,
): void {
  const key = observationKey(marker);
  if (storage.getItem(key) !== null) throw problem();
  const packet = JSON.stringify(marker);
  readObservationMarker(packet, marker);
  storage.setItem(key, packet);
  if (storage.getItem(key) !== packet) throw problem();
}
export function readLearningObservation(
  value: unknown,
  pending: ObservationMarker,
): LearningObservationReceipt {
  readObservationMarker(JSON.stringify(pending), pending);
  const row = object(value);
  if (
    [
      "actor_id",
      "projection_id",
      "submission_request_id",
      "workspace_id",
      "target_revision",
      "payload_digest",
    ].some((field) => row[field] === null)
  )
    throw new Error(
      "Receipt provenance is incomplete. The original observation remains unresolved.",
    );
  if (
    Object.keys(row).sort().join(",") !==
      "actor_id,citations,content_sha256,created_at,current_projection_state,current_record_state,digest,event_id,event_state,kind,payload_digest,projection_id,record_id,repository_id,request_id,run_id,sequence,source_id,submission_request_id,target_revision,workspace_id" ||
    row.record_id !== pending.record ||
    row.repository_id !== pending.repository ||
    row.run_id !== pending.run ||
    row.request_id !== pending.requestId ||
    row.actor_id !== pending.actor ||
    row.projection_id !== pending.projection ||
    row.submission_request_id !== pending.submissionRequest ||
    row.workspace_id !== pending.workspace ||
    row.source_id !== pending.source ||
    row.target_revision !== pending.targetRevision ||
    row.payload_digest !== pending.payloadDigest ||
    row.content_sha256 !== pending.contentHash ||
    row.kind !== "projection_observation" ||
    row.event_state !== "verified" ||
    typeof row.sequence !== "number" ||
    !Number.isSafeInteger(row.sequence) ||
    row.sequence < 1 ||
    typeof row.created_at !== "string" ||
    row.created_at.length > 64 ||
    !/(Z|[+-]\d{2}:\d{2})$/.test(row.created_at) ||
    !Number.isFinite(Date.parse(row.created_at)) ||
    !Array.isArray(row.citations) ||
    !row.citations.length ||
    row.citations.length > 8
  )
    throw problem();
  const citations = row.citations.map((value) => {
    const citation = object(value);
    if (
      Object.keys(citation).sort().join(",") !== "chunk_id,source_id" ||
      citation.source_id !== pending.source
    )
      throw problem();
    return {
      source_id: pending.source,
      chunk_id: text(citation.chunk_id, chunk),
    };
  });
  return {
    event_id: text(row.event_id, id),
    record_id: pending.record,
    repository_id: pending.repository,
    run_id: pending.run,
    request_id: pending.requestId,
    kind: "projection_observation",
    event_state: "verified",
    current_record_state: choice(row.current_record_state, [
      "candidate",
      "verified",
      "invalidated",
      "superseded",
    ]),
    current_projection_state: choice(row.current_projection_state, [
      "pending",
      "executing",
      "source_admitted",
      "verified",
      "unknown",
      "abandoned",
    ]),
    actor_id: pending.actor,
    projection_id: pending.projection,
    submission_request_id: pending.submissionRequest,
    workspace_id: pending.workspace,
    source_id: pending.source,
    target_revision: pending.targetRevision,
    payload_digest: pending.payloadDigest,
    content_sha256: pending.contentHash,
    citations,
    created_at: row.created_at,
    sequence: row.sequence,
    digest: text(row.digest, hash),
  };
}
