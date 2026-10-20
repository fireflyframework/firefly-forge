/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";
import type { DelegationContext } from "./ScmDelegationData";
import {
  readPollingBindingReceipt,
  type PollingBindingReceipt,
} from "./ScmPollingData";
export type PollResumeInput = {
  version: 3;
  transport: "github_rest_poll_v1";
  request_id: string;
  expected_previous_digest: string | null;
  expected_project_binding_digest: string;
  expected_binding_request_id: string;
  expected_binding_digest: string;
  component_ids: string[];
  allowed_actions: ["resume"];
  active: boolean;
  acknowledge_same_run_resume: true;
};
export type PollResumeReceipt = {
  version: 3;
  event_id: string;
  project_id: string;
  repository_id: string;
  binding_key: string;
  native_user_id: string;
  revision: number;
  previous_digest: string | null;
  prepared_at: string;
  administering_user_id: string;
  request: PollResumeInput;
  request_digest: string;
  digest: string;
};
export type PollResumeMarker = DelegationContext & {
  version: 3;
  requestId: string;
  inputDigest: string;
  bindingRequestId: string;
  bindingDigest: string;
  projectDigest: string;
  expectedHead: string | null;
};
type StoragePort = Pick<Storage, "length" | "key" | "getItem" | "setItem">;
export type PollResumeLocks = {
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
function record(value: unknown, keys: string[]): Record<string, unknown> {
  check(value && typeof value === "object" && !Array.isArray(value));
  const row = value as Record<string, unknown>;
  check(Object.keys(row).sort().join() === [...keys].sort().join());
  check(new TextEncoder().encode(JSON.stringify(row)).length <= 65536);
  return row;
}
function matches(value: unknown, pattern: RegExp): string {
  check(typeof value === "string" && pattern.test(value));
  return value;
}
function nullableHash(value: unknown): string | null {
  return value === null ? null : matches(value, hash);
}
export function pollResumeInput(value: unknown): PollResumeInput {
  const row = record(value, [
    "version",
    "transport",
    "request_id",
    "expected_previous_digest",
    "expected_project_binding_digest",
    "expected_binding_request_id",
    "expected_binding_digest",
    "component_ids",
    "allowed_actions",
    "active",
    "acknowledge_same_run_resume",
  ]);
  check(
    row.version === 3 &&
      row.transport === "github_rest_poll_v1" &&
      row.acknowledge_same_run_resume === true &&
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
      row.allowed_actions[0] === "resume",
  );
  return row as PollResumeInput;
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
  return `forge.scm-poll-resume-consent.v3:${encodeURIComponent(scope.base)}:${scope.actor}:${scope.project}:${scope.repository}:`;
}
function marker(value: unknown): PollResumeMarker {
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
  check(row.version === 3);
  matches(row.requestId, id);
  matches(row.bindingRequestId, id);
  [row.inputDigest, row.bindingDigest, row.projectDigest].forEach((value) =>
    matches(value, hash),
  );
  nullableHash(row.expectedHead);
  check(typeof row.base === "string");
  const result = row as PollResumeMarker;
  prefix(result);
  return result;
}
export function pollResumeMarkerBinding(value: PollResumeMarker): string {
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
export function readPollResumeMarkers(
  storage: StoragePort,
  scope: DelegationContext,
): PollResumeMarker[] {
  const start = prefix(scope),
    result: PollResumeMarker[] = [];
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
export async function retainPollResumeMarker(
  storage: StoragePort,
  value: PollResumeMarker,
  confirmed: ReadonlySet<string>,
  locks: PollResumeLocks | undefined,
  current: () => boolean,
): Promise<PollResumeMarker> {
  const item = marker(value);
  check(locks && typeof locks.request === "function");
  return locks.request(prefix(item), { mode: "exclusive" }, () => {
    check(current());
    const existing = readPollResumeMarkers(storage, item);
    check(
      existing.length < 100 &&
        existing.every((entry) =>
          confirmed.has(pollResumeMarkerBinding(entry)),
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
export function pollResumeGrantReference(value: unknown): {
  requestId: string;
  digest: string;
} {
  const row = record(value, receiptKeys),
    input = pollResumeInput(row.request);
  return {
    requestId: input.expected_binding_request_id,
    digest: input.expected_binding_digest,
  };
}
export async function readPollResumeReceipt(
  value: unknown,
  bindingValue: PollingBindingReceipt,
  scope: DelegationContext,
  retained?: PollResumeMarker,
): Promise<PollResumeReceipt> {
  prefix(scope);
  const row = record(value, receiptKeys),
    input = pollResumeInput(row.request);
  const binding = await readPollingBindingReceipt(bindingValue, {
    project: scope.project,
    repository: scope.repository,
    requestId: bindingValue.request.request_id,
  });
  check(
    row.version === 3 &&
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
      input.expected_project_binding_digest ===
        binding.project_binding_digest &&
      input.component_ids.every((item) =>
        binding.request.component_ids.includes(item),
      ),
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
  return row as PollResumeReceipt;
}

export async function readPollResumeHeads(
  value: unknown,
  scope: DelegationContext,
  after: string | null = null,
): Promise<{ items: PollingBindingReceipt[]; next_after: string | null }> {
  prefix(scope);
  check(value && typeof value === "object" && !Array.isArray(value));
  const page = value as Record<string, unknown>;
  check(
    Object.keys(page).sort().join() ===
      ["project_id", "repository_id", "heads_only", "items", "next_after"]
        .sort()
        .join(),
  );
  check(new TextEncoder().encode(JSON.stringify(page)).length <= 524288);
  check(
    page.project_id === scope.project &&
      page.repository_id === scope.repository &&
      page.heads_only === true &&
      Array.isArray(page.items) &&
      page.items.length <= 50,
  );
  if (after !== null) matches(after, hash);
  const next = nullableHash(page.next_after);
  const items: PollingBindingReceipt[] = [];
  let previous = after;
  for (const value of page.items) {
    check(value && typeof value === "object" && !Array.isArray(value));
    const request = (value as Record<string, unknown>).request;
    check(request && typeof request === "object" && !Array.isArray(request));
    const requestId = matches(
      (request as Record<string, unknown>).request_id,
      id,
    );
    const receipt = await readPollingBindingReceipt(value, {
      project: scope.project,
      repository: scope.repository,
      requestId,
    });
    check(previous === null || receipt.binding_key > previous);
    previous = receipt.binding_key;
    items.push(receipt);
  }
  check(
    next === null ||
      (items.length === 50 && next === items.at(-1)!.binding_key),
  );
  return { items, next_after: next };
}
