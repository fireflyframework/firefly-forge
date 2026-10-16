/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  readDelegationReceipt,
  type DelegationContext,
  type DelegationReceipt,
} from "./ScmDelegationData";
export type ProductChangeInput = {
  version: 1;
  request_id: string;
  expected_previous_digest: string | null;
  expected_project_binding_digest: string;
  expected_binding_request_id: string;
  expected_binding_digest: string;
  component_ids: string[];
  allowed_actions: ["product_change"];
  active: boolean;
  acknowledge_product_definition_edit: true;
};
export type ProductChangeReceipt = {
  version: 1;
  event_id: string;
  project_id: string;
  repository_id: string;
  binding_key: string;
  native_user_id: string;
  revision: number;
  previous_digest: string | null;
  prepared_at: string;
  administering_user_id: string;
  request: ProductChangeInput;
  request_digest: string;
  digest: string;
};
export type ProductChangeMarker = DelegationContext & {
  version: 1;
  requestId: string;
  inputDigest: string;
  bindingRequestId: string;
  bindingDigest: string;
  projectDigest: string;
  expectedHead: string | null;
};
type StoragePort = Pick<Storage, "length" | "key" | "getItem" | "setItem">;
export type ProductChangeLocks = {
  request<T>(
    name: string,
    options: { mode: "exclusive" },
    callback: () => T,
  ): Promise<T>;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
function check(value: unknown): asserts value {
  if (!value)
    throw new Error(
      "The exact consent or original request could not be verified. Keep the original request; do not send a replacement.",
    );
}
function record(
  value: unknown,
  keys: string[],
  limit = 65536,
): Record<string, unknown> {
  check(value && typeof value === "object" && !Array.isArray(value));
  const row = value as Record<string, unknown>;
  check(Object.keys(row).sort().join() === [...keys].sort().join());
  check(new TextEncoder().encode(JSON.stringify(row)).length <= limit);
  return row;
}
function matches(value: unknown, pattern: RegExp): string {
  check(typeof value === "string" && pattern.test(value));
  return value;
}
function nullableHash(value: unknown): string | null {
  return value === null ? null : matches(value, hash);
}
export function productChangeInput(value: unknown): ProductChangeInput {
  const row = record(value, [
    "version",
    "request_id",
    "expected_previous_digest",
    "expected_project_binding_digest",
    "expected_binding_request_id",
    "expected_binding_digest",
    "component_ids",
    "allowed_actions",
    "active",
    "acknowledge_product_definition_edit",
  ]);
  check(
    row.version === 1 &&
      row.acknowledge_product_definition_edit === true &&
      typeof row.active === "boolean",
  );
  matches(row.request_id, id);
  matches(row.expected_binding_request_id, id);
  nullableHash(row.expected_previous_digest);
  matches(row.expected_project_binding_digest, hash);
  matches(row.expected_binding_digest, hash);
  check(
    Array.isArray(row.component_ids) &&
      row.component_ids.length >= 1 &&
      row.component_ids.length <= 50 &&
      new Set(row.component_ids).size === row.component_ids.length,
  );
  row.component_ids.forEach((item) => matches(item, /^[a-z][a-z0-9-]{0,63}$/));
  check(
    Array.isArray(row.allowed_actions) &&
      row.allowed_actions.length === 1 &&
      row.allowed_actions[0] === "product_change",
  );
  return row as ProductChangeInput;
}
function prefix(scope: DelegationContext): string {
  const url = new URL(scope.base);
  check(
    ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      url.href.replace(/\/$/, "") === scope.base,
  );
  [scope.actor, scope.project, scope.repository].forEach((value) =>
    matches(value, id),
  );
  return `forge.scm-product-change-consent.v1:${encodeURIComponent(scope.base)}:${scope.actor}:${scope.project}:${scope.repository}:`;
}
function marker(value: unknown): ProductChangeMarker {
  const row = record(value, [
    "version",
    "base",
    "actor",
    "project",
    "repository",
    "requestId",
    "inputDigest",
    "bindingRequestId",
    "bindingDigest",
    "projectDigest",
    "expectedHead",
  ]);
  check(row.version === 1);
  matches(row.requestId, id);
  matches(row.bindingRequestId, id);
  [row.inputDigest, row.bindingDigest, row.projectDigest].forEach((value) =>
    matches(value, hash),
  );
  nullableHash(row.expectedHead);
  check(typeof row.base === "string");
  const result = row as ProductChangeMarker;
  prefix(result);
  return result;
}
export function productChangeMarkerBinding(value: ProductChangeMarker): string {
  const item = marker(value);
  return (
    prefix(item) +
    [
      item.requestId,
      item.inputDigest,
      item.bindingRequestId,
      item.bindingDigest,
      item.projectDigest,
      item.expectedHead ?? "initial",
    ].join(":")
  );
}
export function readProductChangeMarkers(
  storage: StoragePort,
  scope: DelegationContext,
): ProductChangeMarker[] {
  const start = prefix(scope),
    result: ProductChangeMarker[] = [];
  check(storage.length <= 4096);
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index);
    if (!key?.startsWith(start)) continue;
    const item = marker(JSON.parse(storage.getItem(key) ?? "null"));
    check(key === prefix(item) + item.requestId && prefix(item) === start);
    result.push(item);
    check(result.length <= 100);
  }
  return result;
}
export async function retainProductChangeMarker(
  storage: StoragePort,
  value: ProductChangeMarker,
  confirmed: ReadonlySet<string>,
  locks: ProductChangeLocks | undefined,
  current: () => boolean,
): Promise<ProductChangeMarker> {
  const item = marker(value);
  check(locks && typeof locks.request === "function");
  return locks.request(prefix(item), { mode: "exclusive" }, () => {
    check(current());
    const existing = readProductChangeMarkers(storage, item);
    check(
      existing.length < 100 &&
        existing.every((entry) =>
          confirmed.has(productChangeMarkerBinding(entry)),
        ),
    );
    const key = prefix(item) + item.requestId;
    check(storage.getItem(key) === null);
    const encoded = JSON.stringify(item);
    storage.setItem(key, encoded);
    check(storage.getItem(key) === encoded);
    return marker(JSON.parse(encoded));
  });
}
const receiptKeys = [
  "version",
  "event_id",
  "project_id",
  "repository_id",
  "binding_key",
  "native_user_id",
  "revision",
  "previous_digest",
  "prepared_at",
  "administering_user_id",
  "request",
  "request_digest",
  "digest",
];
export function productChangeGrantReference(value: unknown): {
  requestId: string;
  digest: string;
} {
  const row = record(value, receiptKeys),
    input = productChangeInput(row.request);
  return {
    requestId: input.expected_binding_request_id,
    digest: input.expected_binding_digest,
  };
}
export async function readProductChangeReceipt(
  value: unknown,
  bindingValue: DelegationReceipt,
  scope: DelegationContext,
  retained?: ProductChangeMarker,
  expectedComponents?: readonly string[],
): Promise<ProductChangeReceipt> {
  prefix(scope);
  const row = record(value, receiptKeys),
    input = productChangeInput(row.request);
  const binding = await readDelegationReceipt(bindingValue, {
    project: scope.project,
    repository: scope.repository,
  });
  check(
    row.version === 1 &&
      row.project_id === scope.project &&
      row.repository_id === scope.repository &&
      row.binding_key === binding.binding_key &&
      row.native_user_id === binding.request.native_user_id,
  );
  matches(row.event_id, id);
  matches(row.administering_user_id, id);
  matches(row.request_digest, hash);
  matches(row.digest, hash);
  check(
    Number.isSafeInteger(row.revision) &&
      (row.revision as number) >= 1 &&
      (row.revision as number) <= 50,
  );
  check(
    typeof row.prepared_at === "string" &&
      row.prepared_at.length <= 50 &&
      Number.isFinite(Date.parse(row.prepared_at)),
  );
  check(
    nullableHash(row.previous_digest) === input.expected_previous_digest &&
      (row.revision === 1) === (row.previous_digest === null),
  );
  check(
    input.expected_binding_request_id === binding.request.request_id &&
      input.expected_binding_digest === binding.digest &&
      input.expected_project_binding_digest === binding.project_binding_digest,
  );
  if (expectedComponents)
    check(
      [...input.component_ids].sort().join() ===
        [...expectedComponents].sort().join(),
    );
  check((await attemptDigest(input)) === row.request_digest);
  const { digest, ...unsigned } = row;
  check((await attemptDigest(unsigned)) === digest);
  if (retained) {
    const saved = marker(retained);
    check(
      prefix(saved) === prefix(scope) &&
        saved.actor === row.administering_user_id &&
        saved.requestId === input.request_id &&
        saved.inputDigest === row.request_digest &&
        saved.bindingRequestId === input.expected_binding_request_id &&
        saved.bindingDigest === input.expected_binding_digest &&
        saved.projectDigest === input.expected_project_binding_digest &&
        saved.expectedHead === input.expected_previous_digest,
    );
  }
  return row as ProductChangeReceipt;
}

export type ProductChangeOperation = {
  version: 1;
  event_id: string;
  project_id: string;
  repository_id: string;
  native_user_id: string;
  authentication_method: "scm_mapping";
  binding_request_id: string;
  binding_digest: string;
  grant_request_id: string;
  grant_digest: string;
  verified_receipt_id: string;
  verified_reference: DelegationReceipt["verified_reference"];
  request: Record<string, unknown>;
  request_digest: string;
  product_revision: number;
  product_digest: string;
  prepared_at: string;
  digest: string;
};
export async function readProductChangeOperation(
  value: unknown,
  grantValue: unknown,
  binding: DelegationReceipt,
  scope: DelegationContext,
  requestId: string,
): Promise<ProductChangeOperation> {
  matches(requestId, id);
  const row = record(
    value,
    [
      "version",
      "event_id",
      "project_id",
      "repository_id",
      "native_user_id",
      "authentication_method",
      "binding_request_id",
      "binding_digest",
      "grant_request_id",
      "grant_digest",
      "verified_receipt_id",
      "verified_reference",
      "request",
      "request_digest",
      "product_revision",
      "product_digest",
      "prepared_at",
      "digest",
    ],
    131072,
  );
  const grant = await readProductChangeReceipt(grantValue, binding, scope);
  const command = record(
    row.request,
    [
      "version",
      "action",
      "project_id",
      "expected_project_binding_digest",
      "expected_grant_request_id",
      "expected_grant_digest",
      "change",
    ],
    100000,
  );
  const change = record(
    command.change,
    [
      "request_id",
      "base_revision",
      "base_digest",
      "reason",
      "feature_upserts",
      "remove_feature_ids",
      "integration_upserts",
      "remove_integration_ids",
      "append_journeys",
      "append_acceptance",
      "append_user_stories",
    ],
    80000,
  );
  check(
    row.version === 1 &&
      command.version === 1 &&
      command.action === "product_change" &&
      row.authentication_method === "scm_mapping" &&
      grant.request.active,
  );
  check(
    row.project_id === scope.project &&
      command.project_id === scope.project &&
      row.repository_id === scope.repository,
  );
  check(
    row.native_user_id === grant.native_user_id &&
      row.binding_request_id === binding.request.request_id &&
      row.binding_digest === binding.digest,
  );
  check(
    row.grant_request_id === grant.request.request_id &&
      row.grant_digest === grant.digest &&
      command.expected_grant_request_id === row.grant_request_id &&
      command.expected_grant_digest === row.grant_digest &&
      command.expected_project_binding_digest ===
        grant.request.expected_project_binding_digest,
  );
  check(
    change.request_id === requestId &&
      Number.isSafeInteger(change.base_revision) &&
      (change.base_revision as number) >= 1 &&
      row.product_revision === (change.base_revision as number) + 1,
  );
  matches(change.base_digest, hash);
  check(
    typeof change.reason === "string" &&
      change.reason.length >= 1 &&
      change.reason.length <= 2000,
  );
  for (const key of ["feature_upserts", "integration_upserts"]) {
    const entries = change[key];
    check(
      Array.isArray(entries) &&
        entries.length <= (key === "feature_upserts" ? 100 : 50),
    );
    entries.forEach((item) =>
      check(item && typeof item === "object" && !Array.isArray(item)),
    );
  }
  for (const key of [
    "remove_feature_ids",
    "remove_integration_ids",
    "append_journeys",
    "append_acceptance",
    "append_user_stories",
  ]) {
    const entries = change[key];
    check(
      Array.isArray(entries) &&
        entries.length <=
          (key === "append_journeys" || key === "remove_integration_ids"
            ? 50
            : 100),
    );
    entries.forEach((item) =>
      check(
        typeof item === "string" && item.length >= 1 && item.length <= 4000,
      ),
    );
    check(new Set(entries).size === entries.length);
  }
  const reference = record(
    row.verified_reference,
    Object.keys(binding.verified_reference),
  );
  for (const [key, expected] of Object.entries(binding.verified_reference))
    if (key !== "payload_digest") check(reference[key] === expected);
  matches(reference.payload_digest, hash);
  [row.event_id, row.verified_receipt_id].forEach((item) => matches(item, id));
  [row.product_digest, row.request_digest, row.digest].forEach((item) =>
    matches(item, hash),
  );
  check(
    typeof row.prepared_at === "string" &&
      row.prepared_at.length <= 50 &&
      Number.isFinite(Date.parse(row.prepared_at)),
  );
  check((await attemptDigest(command)) === row.request_digest);
  const { digest, ...unsigned } = row;
  check((await attemptDigest(unsigned)) === digest);
  return row as ProductChangeOperation;
}
