/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type GitDeliveryPolicy = {
  version: 1;
  enabled: boolean;
  every_units: number;
  on_pause: boolean;
  remote: "local" | "draft_pr";
  max_checkpoints: number;
};
export type GitDeliveryPolicyView = {
  version: 1;
  project_id: string;
  policy: GitDeliveryPolicy;
  digest: string | null;
  request_id: string | null;
  permissions: { configure: boolean; publish: boolean };
  remote_capabilities: {
    repository_id: string;
    provider: string;
    draft_updates: boolean;
    reason: string | null;
  }[];
};
export type GitDelivery = {
  version: 1;
  run_id: string;
  request_id: string;
  sequence: number;
  original_base_revision: string;
  parent_revision: string;
  revision: string;
  source_tree: string;
  task_id: string;
  attempt: number;
  changed_file_count: number;
  patch_sha256: string;
  branch: string;
  local_status: "checkpointed";
  remote_status: "not_requested" | "publishing" | "draft" | "unknown" | "ready";
  pr_url: string | null;
  created_at: string;
  checkpoint_digest: string;
};
export type GitDeliveries = {
  version: 1;
  run_id: string;
  policy: GitDeliveryPolicy;
  original_base_revision: string | null;
  latest_checkpoint: string | null;
  items: GitDelivery[];
};
const id = /^[a-f0-9]{32}$/;
const digest = /^[a-f0-9]{64}$/;
const revision = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;
function fail(): never {
  throw new Error(
    "The saved development workflow record could not be verified. Refresh it before continuing.",
  );
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return fail();
  return value as Record<string, unknown>;
}
function fields(value: Record<string, unknown>, names: string) {
  const expected = names.split(" ");
  if (
    Object.keys(value).length !== expected.length ||
    expected.some((name) => !(name in value))
  )
    fail();
}
function text(value: unknown, max: number, pattern?: RegExp): string {
  if (
    typeof value !== "string" ||
    !value.length ||
    value.length > max ||
    (pattern && !pattern.test(value))
  )
    return fail();
  return value;
}
function bool(value: unknown): boolean {
  return typeof value === "boolean" ? value : fail();
}
function integer(value: unknown, min: number, max: number): number {
  return typeof value === "number" &&
    Number.isInteger(value) &&
    value >= min &&
    value <= max
    ? value
    : fail();
}
function nullable(
  value: unknown,
  max: number,
  pattern?: RegExp,
): string | null {
  return value === null ? null : text(value, max, pattern);
}
function optionalDisplayText(value: unknown, max: number): string | null {
  return value === "" ? "" : nullable(value, max);
}
export function readPolicy(value: unknown): GitDeliveryPolicy {
  const v = object(value);
  fields(v, "version enabled every_units on_pause remote max_checkpoints");
  if (v.version !== 1 || (v.remote !== "local" && v.remote !== "draft_pr"))
    return fail();
  return {
    version: 1,
    enabled: bool(v.enabled),
    every_units: integer(v.every_units, 1, 20),
    on_pause: bool(v.on_pause),
    remote: v.remote,
    max_checkpoints: integer(v.max_checkpoints, 1, 50),
  };
}
export function policyView(
  value: unknown,
  projectId: string,
): GitDeliveryPolicyView {
  const v = object(value);
  fields(
    v,
    "version project_id policy digest request_id permissions remote_capabilities",
  );
  if (v.version !== 1 || v.project_id !== projectId) return fail();
  const permissions = object(v.permissions);
  fields(permissions, "configure publish");
  if (
    !Array.isArray(v.remote_capabilities) ||
    v.remote_capabilities.length > 50
  )
    return fail();
  const capabilities = v.remote_capabilities.map((raw) => {
    const c = object(raw);
    fields(c, "repository_id provider draft_updates reason");
    return {
      repository_id: text(c.repository_id, 32, id),
      provider: text(c.provider, 32),
      draft_updates: bool(c.draft_updates),
      reason: optionalDisplayText(c.reason, 300),
    };
  });
  if (
    new Set(capabilities.map((c) => c.repository_id)).size !==
    capabilities.length
  )
    return fail();
  return {
    version: 1,
    project_id: text(v.project_id, 32, id),
    policy: readPolicy(v.policy),
    digest: nullable(v.digest, 64, digest),
    request_id: nullable(v.request_id, 32, id),
    permissions: {
      configure: bool(permissions.configure),
      publish: bool(permissions.publish),
    },
    remote_capabilities: capabilities,
  };
}
export function policyRequest(
  current: GitDeliveryPolicyView,
  desired: unknown,
  requestId: string,
) {
  const policy = readPolicy(desired);
  if (
    !current.permissions.configure ||
    (policy.enabled &&
      policy.remote === "draft_pr" &&
      (!current.permissions.publish ||
        !current.remote_capabilities.length ||
        current.remote_capabilities.some((c) => !c.draft_updates)))
  )
    return fail();
  return {
    version: 1,
    request_id: text(requestId, 32, id),
    expected_previous_digest: current.digest,
    policy,
  };
}
export function policyAcknowledgment(
  value: unknown,
  current: GitDeliveryPolicyView,
  desired: unknown,
  requestId: string,
) {
  const saved = policyView(value, current.project_id);
  if (
    !saved.digest ||
    saved.request_id !== requestId ||
    JSON.stringify(saved.policy) !== JSON.stringify(readPolicy(desired))
  )
    return fail();
  return saved;
}
export function deliveryList(value: unknown, runId: string): GitDeliveries {
  const v = object(value);
  fields(
    v,
    "version run_id policy original_base_revision latest_checkpoint items",
  );
  if (
    v.version !== 1 ||
    v.run_id !== runId ||
    !Array.isArray(v.items) ||
    v.items.length > 50
  )
    return fail();
  const base = nullable(v.original_base_revision, 64, revision);
  const latest = nullable(v.latest_checkpoint, 64, revision);
  const items: GitDelivery[] = v.items.map((raw) => {
    const r = object(raw);
    fields(
      r,
      "version run_id request_id sequence original_base_revision parent_revision revision source_tree task_id attempt changed_file_count patch_sha256 branch local_status remote_status pr_url created_at checkpoint_digest",
    );
    if (
      r.version !== 1 ||
      r.run_id !== runId ||
      r.original_base_revision !== base ||
      r.local_status !== "checkpointed" ||
      !["not_requested", "publishing", "draft", "unknown", "ready"].includes(
        String(r.remote_status),
      )
    )
      return fail();
    const created = text(r.created_at, 50);
    if (!Number.isFinite(Date.parse(created))) return fail();
    return {
      version: 1,
      run_id: text(r.run_id, 32, id),
      request_id: text(r.request_id, 32, id),
      sequence: integer(r.sequence, 1, 50),
      original_base_revision: text(r.original_base_revision, 64, revision),
      parent_revision: text(r.parent_revision, 64, revision),
      revision: text(r.revision, 64, revision),
      source_tree: text(r.source_tree, 64, revision),
      task_id: text(r.task_id, 64),
      attempt: integer(r.attempt, 1, 100),
      changed_file_count: integer(r.changed_file_count, 1, 200),
      patch_sha256: text(r.patch_sha256, 64, digest),
      branch: text(r.branch, 200),
      local_status: "checkpointed",
      remote_status: r.remote_status as GitDelivery["remote_status"],
      pr_url: optionalDisplayText(r.pr_url, 2048),
      created_at: created,
      checkpoint_digest: text(r.checkpoint_digest, 64, digest),
    };
  });
  if (
    new Set(items.map((r) => r.sequence)).size !== items.length ||
    new Set(items.map((r) => r.request_id)).size !== items.length
  )
    return fail();
  items.sort((a, b) => a.sequence - b.sequence);
  if (
    (items.length && latest !== items.at(-1)?.revision) ||
    (!items.length && latest !== null)
  )
    return fail();
  return {
    version: 1,
    run_id: text(v.run_id, 32, id),
    policy: readPolicy(v.policy),
    original_base_revision: base,
    latest_checkpoint: latest,
    items,
  };
}
