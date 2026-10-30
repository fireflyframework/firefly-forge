/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { LearningProjection, LearningRecord } from "./LearningRecordData";
import type { InvalidationContext } from "./LearningRecordData";
import { attemptDigest } from "./LearningObservationAttemptData";

export type NativeSourceVersion = {
  asset_id: string;
  native_source_id: string;
  source_version_id: string;
  extraction_id: string;
};
export type NativeSourceCitation = NativeSourceVersion & {
  source_id: string;
  chunk_id: string;
  workspace_id: string;
  tenant_id: string;
  project_id: string;
};
export type NativeLearningEvidence = {
  version: 2;
  kind: "local_outcome" | "observed_failure";
  upstreamAuthority: "unverified";
  tenant: string;
  scope: {
    workspace_id: string;
    project_id: string;
    collection_id: string;
    dataset_ids: string[];
  };
  sourceVersions: NativeSourceVersion[];
  citations: NativeSourceCitation[];
  versionState: "recorded" | "not_recorded";
  observationRequest?: string;
};
export const nativeProblem = () =>
  new Error(
    "Native source evidence could not be confirmed. Keep the original request identity and review its saved scope.",
  );
export function nativeObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw nativeProblem();
  return value as Record<string, unknown>;
}
export function nativeText(
  value: unknown,
  maximum: number,
  pattern?: RegExp,
): string {
  if (
    typeof value !== "string" ||
    !value.length ||
    value.length > maximum ||
    (pattern && !pattern.test(value))
  )
    throw nativeProblem();
  return value;
}
export const nativeId = /^[a-f0-9]{32}$/;
export const nativeHash = /^[a-f0-9]{64}$/;
const catalogId = /^[A-Za-z0-9_-]{1,64}$/;
export function nativeChoice<T extends string>(
  value: unknown,
  options: readonly T[],
): T {
  if (typeof value !== "string" || !options.includes(value as T))
    throw nativeProblem();
  return value as T;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw nativeProblem();
  return value;
}
export function nativeVersions(value: unknown): NativeSourceVersion[] {
  const seen = new Set<string>();
  return array(value).map((value) => {
    const row = nativeObject(value);
    const result = {
      asset_id: nativeText(row.asset_id, 64),
      native_source_id: nativeText(row.native_source_id, 64),
      source_version_id: nativeText(row.source_version_id, 64),
      extraction_id: nativeText(row.extraction_id, 64),
    };
    if (seen.has(result.asset_id)) throw nativeProblem();
    seen.add(result.asset_id);
    return result;
  });
}
export function nativeCitations(
  value: unknown,
  evidence: Pick<NativeLearningEvidence, "scope" | "tenant" | "sourceVersions">,
  source: string,
): NativeSourceCitation[] {
  return array(value).map((value) => {
    const row = nativeObject(value);
    const version = evidence.sourceVersions.find(
      (item) => item.asset_id === row.asset_id,
    );
    if (
      !version ||
      row.source_id !== source ||
      row.workspace_id !== evidence.scope.workspace_id ||
      row.project_id !== evidence.scope.project_id ||
      row.tenant_id !== evidence.tenant ||
      Object.entries(version).some(([key, value]) => row[key] !== value)
    )
      throw nativeProblem();
    return {
      ...version,
      source_id: source,
      chunk_id: nativeText(row.chunk_id, 200),
      workspace_id: evidence.scope.workspace_id,
      project_id: evidence.scope.project_id,
      tenant_id: evidence.tenant,
    };
  });
}
export function readNativeProjection(
  value: unknown,
  record: string,
  expected?: LearningRecord,
): LearningProjection {
  if (nativeObject(value).state === "review_required")
    throw new Error(
      "External memory proposals require private review in Koinon. This panel observes source evidence and cannot approve memory.",
    );
  const row = nativeObject(value),
    operational = nativeObject(row.operational_evidence),
    envelope = nativeObject(operational.envelope),
    context = nativeObject(envelope.context),
    scope = nativeObject(row.scope),
    target = nativeObject(row.target);
  if (
    row.version !== 2 ||
    row.provider !== "koinon" ||
    row.record_id !== record ||
    operational.version !== 2 ||
    operational.record_id !== record ||
    operational.authority !== "native_receipt_observation" ||
    operational.canonical_memory !== false ||
    row.upstream_authority !== "unverified" ||
    operational.envelope_sha256 !== row.payload_digest ||
    target.provider !== "koinon" ||
    row.workspace_id !== scope.workspace_id
  )
    throw nativeProblem();
  const kind = nativeChoice(operational.kind, [
    "local_outcome",
    "observed_failure",
  ] as const);
  if (
    envelope.type !== kind ||
    envelope.version !== 1 ||
    (kind === "observed_failure"
      ? operational.failure_cause !== "not_classified"
      : operational.failure_cause !== null)
  )
    throw nativeProblem();
  if (
    expected &&
    (expected.id !== record ||
      expected.kind !== kind ||
      envelope.run_id !== expected.envelope.run_id ||
      Object.entries(expected.envelope.context).some(
        ([key, value]) => context[key] !== value,
      ) ||
      Object.entries(expected.envelope.source).some(
        ([key, value]) => nativeObject(envelope.source)[key] !== value,
      ))
  )
    throw nativeProblem();
  const native: NativeLearningEvidence = {
    version: 2,
    kind,
    upstreamAuthority: "unverified",
    tenant: nativeText(target.tenant_id, 64, catalogId),
    scope: {
      workspace_id: nativeText(scope.workspace_id, 64, catalogId),
      project_id: nativeText(scope.project_id, 64, catalogId),
      collection_id: nativeText(scope.collection_id, 64, catalogId),
      dataset_ids: array(scope.dataset_ids).map((item) =>
        nativeText(item, 64, catalogId),
      ),
    },
    sourceVersions:
      row.source_versions == null ? [] : nativeVersions(row.source_versions),
    citations: [],
    versionState: nativeChoice(row.version_state ?? "not_recorded", [
      "recorded",
      "not_recorded",
    ] as const),
    ...(row.reconciliation_id == null
      ? {}
      : {
          observationRequest: nativeText(row.reconciliation_id, 32, nativeId),
        }),
  };
  if (native.versionState === "recorded" && !native.sourceVersions.length)
    throw nativeProblem();
  const source =
    row.source_id == null
      ? undefined
      : nativeText(row.source_id, 64, catalogId);
  if (row.citations != null && array(row.citations).length) {
    if (!source) throw nativeProblem();
    native.citations = nativeCitations(row.citations, native, source);
  }
  const state = nativeChoice(row.state, [
    "pending",
    "executing",
    "source_admitted",
    "verified",
    "unknown",
    "abandoned",
  ] as const);
  if (
    state === "verified" &&
    (!source ||
      !native.citations.length ||
      !native.sourceVersions.length ||
      native.versionState !== "recorded")
  )
    throw nativeProblem();
  return {
    id: nativeText(row.id, 32, nativeId),
    record_id: record,
    state,
    native,
    workspace_id: native.scope.workspace_id,
    source_id: source,
    source_status:
      row.source_status == null
        ? undefined
        : nativeText(row.source_status, 200),
    submission_request_id: nativeText(row.request_id, 32, nativeId),
    payload_digest: nativeText(row.payload_digest, 64, nativeHash),
    content_sha256: nativeText(row.content_sha256, 64, nativeHash),
    target_revision: nativeText(target.revision, 32, nativeId),
    citations: native.citations,
  };
}

export type NativeObservationMarker = InvalidationContext & {
  evidenceKind: NativeLearningEvidence["kind"];
  version: 2;
  requestId: string;
  projection: string;
  submissionRequest: string;
  tenant: string;
  workspace: string;
  remoteProject: string;
  collection: string;
  source: string;
  targetRevision: string;
  payloadDigest: string;
  contentHash: string;
  bodyDigest: string;
};
export type NativeObservationReceipt = {
  version: 2;
  event_id: string;
  record_id: string;
  repository_id: string;
  run_id: string;
  request_id: string;
  kind: "projection_observation_v2";
  event_state: "verified";
  current_record_state: LearningRecord["state"];
  current_projection_state: LearningProjection["state"];
  actor_id: string | null;
  projection_id: string;
  submission_request_id: string;
  workspace_id: string;
  target_revision: string;
  payload_digest: string;
  source_id: string;
  content_sha256: string;
  source_versions: NativeSourceVersion[];
  citations: NativeSourceCitation[];
  upstream_authority: "unverified";
  created_at: string;
  sequence: number;
  digest: string;
};
export function nativeObservationKey(context: InvalidationContext): string {
  return `forge.learning-native-observation.v2:${encodeURIComponent(context.base)}:${context.actor}:${context.repository}:${context.record}`;
}
export function nativeObservationBody(marker: NativeObservationMarker) {
  return { request_id: marker.requestId, source_id: marker.source };
}
export function readNativeObservationMarker(
  value: string | null,
  context: InvalidationContext,
): NativeObservationMarker | null {
  if (value === null) return null;
  try {
    if (value.length > 10000) throw nativeProblem();
    const row = nativeObject(JSON.parse(value));
    const keys = [
      "version",
      "evidenceKind",
      "base",
      "actor",
      "repository",
      "project",
      "run",
      "record",
      "requestId",
      "projection",
      "submissionRequest",
      "tenant",
      "workspace",
      "remoteProject",
      "collection",
      "source",
      "targetRevision",
      "payloadDigest",
      "contentHash",
      "bodyDigest",
    ];
    if (
      Object.keys(row).sort().join(",") !== keys.sort().join(",") ||
      row.version !== 2
    )
      throw nativeProblem();
    const base = new URL(context.base);
    if (
      !["http:", "https:"].includes(base.protocol) ||
      base.username ||
      base.password ||
      base.search ||
      base.hash
    )
      throw nativeProblem();
    for (const field of [
      "base",
      "actor",
      "repository",
      "project",
      "run",
      "record",
    ] as const)
      if (row[field] !== context[field]) throw nativeProblem();
    for (const field of ["actor", "repository", "run", "record"] as const)
      nativeText(row[field], 32, nativeId);
    if (row.project !== null) nativeText(row.project, 32, nativeId);
    return {
      ...context,
      version: 2,
      evidenceKind: nativeChoice(row.evidenceKind, [
        "local_outcome",
        "observed_failure",
      ] as const),
      requestId: nativeText(row.requestId, 32, nativeId),
      projection: nativeText(row.projection, 32, nativeId),
      submissionRequest: nativeText(row.submissionRequest, 32, nativeId),
      tenant: nativeText(row.tenant, 64, catalogId),
      workspace: nativeText(row.workspace, 64, catalogId),
      remoteProject: nativeText(row.remoteProject, 64, catalogId),
      collection: nativeText(row.collection, 64, catalogId),
      source: nativeText(row.source, 64, catalogId),
      targetRevision: nativeText(row.targetRevision, 32, nativeId),
      payloadDigest: nativeText(row.payloadDigest, 64, nativeHash),
      contentHash: nativeText(row.contentHash, 64, nativeHash),
      bodyDigest: nativeText(row.bodyDigest, 64, nativeHash),
    };
  } catch {
    throw nativeProblem();
  }
}
export async function makeNativeObservationMarker(
  context: InvalidationContext,
  projection: LearningProjection,
  source: string,
  requestId: string,
): Promise<NativeObservationMarker> {
  const evidence = projection.native;
  if (!evidence) throw nativeProblem();
  const marker = {
    ...context,
    version: 2 as const,
    evidenceKind: evidence.kind,
    requestId,
    projection: projection.id,
    submissionRequest: projection.submission_request_id!,
    tenant: evidence.tenant,
    workspace: evidence.scope.workspace_id,
    remoteProject: evidence.scope.project_id,
    collection: evidence.scope.collection_id,
    source,
    targetRevision: projection.target_revision!,
    payloadDigest: projection.payload_digest!,
    contentHash: projection.content_sha256!,
    bodyDigest: await attemptDigest({
      request_id: requestId,
      source_id: source,
    }),
  };
  return readNativeObservationMarker(JSON.stringify(marker), context)!;
}
export async function retainNativeObservation(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: NativeObservationMarker,
) {
  const key = nativeObservationKey(marker);
  if (storage.getItem(key) !== null) throw nativeProblem();
  const packet = JSON.stringify(marker);
  readNativeObservationMarker(packet, marker);
  if (
    (await attemptDigest(nativeObservationBody(marker))) !== marker.bodyDigest
  )
    throw nativeProblem();
  storage.setItem(key, packet);
  if (storage.getItem(key) !== packet) throw nativeProblem();
}
export async function exactNativeObservation(
  storage: Pick<Storage, "getItem">,
  marker: NativeObservationMarker,
) {
  const saved = readNativeObservationMarker(
    storage.getItem(nativeObservationKey(marker)),
    marker,
  );
  if (
    !saved ||
    JSON.stringify(saved) !== JSON.stringify(marker) ||
    (await attemptDigest(nativeObservationBody(saved))) !== saved.bodyDigest
  )
    throw nativeProblem();
  return saved;
}
export function readNativeLearningReceipt(
  value: unknown,
  pending: NativeObservationMarker,
  historical = false,
): NativeObservationReceipt {
  const row = nativeObject(value);
  const keys = [
    "version",
    "event_id",
    "record_id",
    "repository_id",
    "run_id",
    "request_id",
    "kind",
    "event_state",
    "current_record_state",
    "current_projection_state",
    "actor_id",
    "projection_id",
    "submission_request_id",
    "workspace_id",
    "target_revision",
    "payload_digest",
    "source_id",
    "content_sha256",
    "source_versions",
    "citations",
    "upstream_authority",
    "created_at",
    "sequence",
    "digest",
  ];
  if (
    Object.keys(row).sort().join(",") !== keys.sort().join(",") ||
    (pending.evidenceKind === "observed_failure" &&
      row.current_record_state === "verified")
  )
    throw nativeProblem();
  const fields = {
    record_id: pending.record,
    repository_id: pending.repository,
    run_id: pending.run,
    request_id: pending.requestId,
    projection_id: pending.projection,
    submission_request_id: pending.submissionRequest,
    workspace_id: pending.workspace,
    source_id: pending.source,
    target_revision: pending.targetRevision,
    payload_digest: pending.payloadDigest,
    content_sha256: pending.contentHash,
  };
  if (
    Object.entries(fields).some(([key, value]) => row[key] !== value) ||
    row.version !== 2 ||
    row.kind !== "projection_observation_v2" ||
    row.event_state !== "verified" ||
    row.upstream_authority !== "unverified" ||
    (!historical && row.actor_id !== pending.actor)
  )
    throw nativeProblem();
  const sourceVersions = nativeVersions(row.source_versions);
  const evidence = {
    sourceVersions,
    tenant: pending.tenant,
    scope: {
      workspace_id: pending.workspace,
      project_id: pending.remoteProject,
      collection_id: pending.collection,
      dataset_ids: [],
    },
  };
  const citations = nativeCitations(row.citations, evidence, pending.source);
  const created = nativeText(row.created_at, 64);
  if (
    !sourceVersions.length ||
    !citations.length ||
    typeof row.sequence !== "number" ||
    !Number.isSafeInteger(row.sequence) ||
    row.sequence < 1 ||
    !/(Z|[+-]\d{2}:\d{2})$/.test(created) ||
    !Number.isFinite(Date.parse(created))
  )
    throw nativeProblem();
  return {
    ...fields,
    version: 2,
    event_id: nativeText(row.event_id, 32, nativeId),
    kind: "projection_observation_v2",
    event_state: "verified",
    current_record_state: nativeChoice(row.current_record_state, [
      "candidate",
      "verified",
      "invalidated",
      "superseded",
    ] as const),
    current_projection_state: nativeChoice(row.current_projection_state, [
      "pending",
      "executing",
      "source_admitted",
      "verified",
      "unknown",
      "abandoned",
    ] as const),
    actor_id:
      row.actor_id === null ? null : nativeText(row.actor_id, 32, nativeId),
    source_versions: sourceVersions,
    citations,
    upstream_authority: "unverified",
    created_at: created,
    sequence: row.sequence,
    digest: nativeText(row.digest, 64, nativeHash),
  };
}
