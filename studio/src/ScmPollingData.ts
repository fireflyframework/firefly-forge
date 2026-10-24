/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";

type Data = Record<string, unknown>;
export type PollingBindingReceipt = {
  version: 2;
  event_id: string;
  project_id: string;
  repository_id: string;
  project_binding_digest: string;
  binding_key: string;
  revision: number;
  previous_digest: string | null;
  prepared_at: string;
  administering_user_id: string;
  request: {
    version: 2;
    request_id: string;
    expected_previous_digest: string | null;
    expected_project_binding_digest: string;
    observed_receipt_id: string;
    expected_reference_digest: string;
    native_user_id: string;
    component_ids: string[];
    allowed_actions: ("respond" | "status" | "checkpoint")[];
    active: boolean;
    acknowledge_api_observed_identity: true;
  };
  request_digest: string;
  verified_reference: {
    version: 1;
    transport: "github_rest_poll_v1";
    provider: "github";
    api_origin: string;
    web_origin: string;
    installation_id: string;
    remote_repository_id: string;
    external_actor_id: string;
    connection_revision: string;
    connection_scope_digest: string;
    resource_id: string;
    issue_number: number;
    resource_updated_at: string;
    payload_digest: string;
    response_sha256: string;
    observed_at: string;
  };
  digest: string;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/,
  numeric = /^[1-9][0-9]{0,39}$/;
function check(value: unknown): asserts value {
  if (!value)
    throw Error(
      "The historical API-observed delegation receipt could not be verified.",
    );
}
function object(value: unknown, keys: string[]): Data {
  check(value && typeof value === "object" && !Array.isArray(value));
  const row = value as Data;
  check(Object.keys(row).sort().join(",") === [...keys].sort().join(","));
  return row;
}
function match(value: unknown, pattern: RegExp): string {
  check(typeof value === "string" && pattern.test(value));
  return value;
}
function timestamp(value: unknown) {
  check(
    typeof value === "string" &&
      value.length <= 50 &&
      Number.isFinite(Date.parse(value)),
  );
}
function origin(value: unknown) {
  check(typeof value === "string" && value.length <= 500);
  const url = new URL(value),
    authority = value.slice("https://".length);
  check(
    url.protocol === "https:" &&
      url.hostname &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      !url.hostname.endsWith(".") &&
      /^https:\/\/[^/?#\\\s]+$/.test(value) &&
      authority === authority.toLowerCase(),
  );
}
function strings(value: unknown, pattern: RegExp, maximum: number) {
  check(
    Array.isArray(value) &&
      value.length >= 1 &&
      value.length <= maximum &&
      new Set(value).size === value.length,
  );
  value.forEach((item) => match(item, pattern));
}
export async function readPollingBindingReceipt(
  value: unknown,
  scope: {
    project: string;
    repository: string;
    requestId: string;
    receiptDigest?: string;
  },
): Promise<PollingBindingReceipt> {
  const row = object(value, [
    "version",
    "event_id",
    "project_id",
    "repository_id",
    "project_binding_digest",
    "binding_key",
    "revision",
    "previous_digest",
    "prepared_at",
    "administering_user_id",
    "request",
    "request_digest",
    "verified_reference",
    "digest",
  ]);
  check(new TextEncoder().encode(JSON.stringify(row)).length <= 65536);
  check(
    row.version === 2 &&
      row.project_id === match(scope.project, id) &&
      row.repository_id === match(scope.repository, id),
  );
  for (const key of ["event_id", "administering_user_id"]) match(row[key], id);
  for (const key of [
    "project_binding_digest",
    "binding_key",
    "request_digest",
    "digest",
  ])
    match(row[key], hash);
  check(
    Number.isSafeInteger(row.revision) &&
      (row.revision as number) >= 1 &&
      (row.revision as number) <= 50,
  );
  if (row.previous_digest !== null) match(row.previous_digest, hash);
  check((row.revision === 1) === (row.previous_digest === null));
  timestamp(row.prepared_at);
  const request = object(row.request, [
    "version",
    "request_id",
    "expected_previous_digest",
    "expected_project_binding_digest",
    "observed_receipt_id",
    "expected_reference_digest",
    "native_user_id",
    "component_ids",
    "allowed_actions",
    "active",
    "acknowledge_api_observed_identity",
  ]);
  check(
    request.version === 2 &&
      typeof request.active === "boolean" &&
      request.acknowledge_api_observed_identity === true,
  );
  for (const key of ["request_id", "native_user_id", "observed_receipt_id"])
    match(request[key], id);
  for (const key of [
    "expected_project_binding_digest",
    "expected_reference_digest",
  ])
    match(request[key], hash);
  check(
    request.expected_previous_digest === row.previous_digest &&
      request.expected_project_binding_digest === row.project_binding_digest,
  );
  strings(request.component_ids, /^[a-z][a-z0-9-]{0,63}$/, 50);
  strings(request.allowed_actions, /^(respond|status|checkpoint)$/, 3);
  const reference = object(row.verified_reference, [
    "version",
    "transport",
    "provider",
    "api_origin",
    "web_origin",
    "installation_id",
    "remote_repository_id",
    "external_actor_id",
    "connection_revision",
    "connection_scope_digest",
    "resource_id",
    "issue_number",
    "resource_updated_at",
    "payload_digest",
    "response_sha256",
    "observed_at",
  ]);
  check(
    reference.version === 1 &&
      reference.transport === "github_rest_poll_v1" &&
      reference.provider === "github",
  );
  origin(reference.api_origin);
  origin(reference.web_origin);
  for (const key of [
    "installation_id",
    "remote_repository_id",
    "external_actor_id",
    "resource_id",
  ])
    match(reference[key], numeric);
  match(reference.connection_revision, id);
  for (const key of [
    "connection_scope_digest",
    "payload_digest",
    "response_sha256",
  ])
    match(reference[key], hash);
  check(
    Number.isSafeInteger(reference.issue_number) &&
      (reference.issue_number as number) >= 1,
  );
  timestamp(reference.resource_updated_at);
  timestamp(reference.observed_at);
  const identity = Object.fromEntries(
    [
      "provider",
      "api_origin",
      "installation_id",
      "remote_repository_id",
      "external_actor_id",
    ].map((key) => [key, reference[key]]),
  );
  check(
    (await attemptDigest({
      project_id: scope.project,
      transport: "github_rest_poll_v1",
      ...identity,
    })) === row.binding_key,
  );
  check(
    (await attemptDigest(request)) === row.request_digest &&
      (await attemptDigest(reference)) === request.expected_reference_digest,
  );
  const { digest, ...body } = row;
  check(
    (await attemptDigest(body)) === digest &&
      request.request_id === match(scope.requestId, id),
  );
  if (scope.receiptDigest !== undefined)
    check(digest === match(scope.receiptDigest, hash));
  return row as PollingBindingReceipt;
}
