/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { record as retained } from "./learning-record-fixture";

export const nativeRecord = {
  ...retained,
  knowledge_provider: "koinon",
  verified_at: "2026-10-30T12:00:00Z",
  expires_at: "2026-11-02T12:00:00Z",
  envelope: {
    ...retained.envelope,
    question: "Fixture: observe the inventory regression",
    statement:
      "The local inventory check returned exit code 0. External claims remain unverified.",
  },
};
export const nativeVersion = {
  asset_id: "inventory-document",
  native_source_id: "native-inventory-source",
  source_version_id: "inventory-version-7",
  extraction_id: "inventory-extraction-3",
};
export const nativeProjection = {
  id: "1".repeat(32),
  record_id: nativeRecord.id,
  version: 2,
  provider: "koinon",
  state: "source_admitted",
  workspace_id: "inventory-workspace",
  source_id: "inventory-source",
  source_status: "ready",
  request_id: "2".repeat(32),
  target: {
    revision: "3".repeat(32),
    provider: "koinon",
    tenant_id: "fixture-tenant",
  },
  scope: {
    workspace_id: "inventory-workspace",
    project_id: "inventory-project",
    collection_id: "operational-evidence",
    dataset_ids: [],
  },
  content_sha256: "4".repeat(64),
  payload_digest: "5".repeat(64),
  upstream_authority: "unverified",
  operational_evidence: {
    version: 2,
    kind: "local_outcome",
    authority: "native_receipt_observation",
    canonical_memory: false,
    failure_cause: null,
    record_id: nativeRecord.id,
    envelope: nativeRecord.envelope,
    envelope_sha256: "5".repeat(64),
    expires_at: nativeRecord.expires_at,
  },
  asset_ids: [nativeVersion.asset_id],
  source_version_id: null,
  source_versions: [],
  version_state: "not_recorded",
  citations: [],
};
export function nativeReceipt(requestId: string, actor = "f".repeat(32)) {
  return {
    version: 2,
    event_id: "6".repeat(32),
    record_id: nativeRecord.id,
    repository_id: nativeRecord.envelope.context.repository_id,
    run_id: nativeRecord.envelope.run_id,
    request_id: requestId,
    kind: "projection_observation_v2",
    event_state: "verified",
    current_record_state: "verified",
    current_projection_state: "verified",
    actor_id: actor,
    projection_id: nativeProjection.id,
    submission_request_id: nativeProjection.request_id,
    workspace_id: nativeProjection.workspace_id,
    target_revision: nativeProjection.target.revision,
    payload_digest: nativeProjection.payload_digest,
    source_id: nativeProjection.source_id,
    content_sha256: nativeProjection.content_sha256,
    source_versions: [nativeVersion],
    citations: [
      {
        ...nativeVersion,
        source_id: nativeProjection.source_id,
        chunk_id: "inventory-chunk-1",
        workspace_id: nativeProjection.workspace_id,
        project_id: nativeProjection.scope.project_id,
        tenant_id: nativeProjection.target.tenant_id,
      },
    ],
    upstream_authority: "unverified",
    created_at: "2026-10-30T12:05:00Z",
    sequence: 4,
    digest: "7".repeat(64),
  };
}
export const nativeVerifiedProjection = {
  ...nativeProjection,
  state: "verified",
  source_versions: [nativeVersion],
  version_state: "recorded",
  citations: nativeReceipt("8".repeat(32)).citations,
  reconciliation_id: "8".repeat(32),
};
export const nativeFailureRecord = {
  ...nativeRecord,
  id: "9".repeat(32),
  state: "candidate",
  kind: "observed_failure",
  observed_at: "2026-10-30T12:00:00Z",
  verified_at: undefined,
  envelope: {
    ...nativeRecord.envelope,
    type: "observed_failure",
    question: "Fixture: inspect an observed failure",
    statement:
      "The inventory check returned exit code 1. Its cause has not been classified.",
  },
};
export const nativeFailureVersion = {
  asset_id: "inventory-failure-document",
  native_source_id: "native-failure-source",
  source_version_id: "failure-version-2",
  extraction_id: "failure-extraction-1",
};
export const nativeFailureProjection = {
  ...nativeProjection,
  id: "8".repeat(32),
  record_id: nativeFailureRecord.id,
  source_id: "inventory-failure-source",
  request_id: "7".repeat(32),
  content_sha256: "8".repeat(64),
  payload_digest: "9".repeat(64),
  operational_evidence: {
    ...nativeProjection.operational_evidence,
    kind: "observed_failure",
    failure_cause: "not_classified",
    record_id: nativeFailureRecord.id,
    envelope: nativeFailureRecord.envelope,
    envelope_sha256: "9".repeat(64),
  },
  asset_ids: [nativeFailureVersion.asset_id],
};
export function nativeFailureReceipt(requestId: string) {
  return {
    ...nativeReceipt(requestId),
    record_id: nativeFailureRecord.id,
    current_record_state: "candidate",
    projection_id: nativeFailureProjection.id,
    submission_request_id: nativeFailureProjection.request_id,
    source_id: nativeFailureProjection.source_id,
    payload_digest: nativeFailureProjection.payload_digest,
    content_sha256: nativeFailureProjection.content_sha256,
    source_versions: [nativeFailureVersion],
    citations: [
      {
        ...nativeFailureVersion,
        chunk_id: "failure-chunk-1",
        source_id: nativeFailureProjection.source_id,
        workspace_id: nativeFailureProjection.workspace_id,
        project_id: nativeFailureProjection.scope.project_id,
        tenant_id: nativeFailureProjection.target.tenant_id,
      },
    ],
  };
}
