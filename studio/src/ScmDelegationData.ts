/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";

export type DelegationContext = {
  base: string;
  actor: string;
  project: string;
  repository: string;
};
export type DelegationMarker = DelegationContext & {
  version: 1;
  requestId: string;
  inputDigest: string;
};
type MarkerStorage = Pick<Storage, "length" | "key" | "getItem" | "setItem">;
const id = /^[a-f0-9]{32}$/;
const hash = /^[a-f0-9]{64}$/;
const trackingError = () =>
  new Error(
    "The original delegation request could not be retained or read. Keep it for review; do not send another request.",
  );
function context(value: DelegationContext): string {
  const url = new URL(value.base);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.href.replace(/\/$/, "") !== value.base ||
    ![value.actor, value.project, value.repository].every(
      (item) => typeof item === "string" && id.test(item),
    )
  )
    throw trackingError();
  return `forge.scm-delegation.v1:${encodeURIComponent(value.base)}:${value.actor}:${value.project}:${value.repository}:`;
}
function marker(value: unknown): DelegationMarker {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw trackingError();
  const item = value as DelegationMarker;
  if (
    Object.keys(item).sort().join(",") !==
      "actor,base,inputDigest,project,repository,requestId,version" ||
    item.version !== 1 ||
    typeof item.requestId !== "string" ||
    !id.test(item.requestId) ||
    typeof item.inputDigest !== "string" ||
    !hash.test(item.inputDigest)
  )
    throw trackingError();
  context(item);
  return item;
}
function markerKey(value: DelegationMarker): string {
  return context(marker(value)) + value.requestId;
}
export function delegationMarkerBinding(value: DelegationMarker): string {
  return markerKey(value) + ":" + value.inputDigest;
}
export function readDelegationMarkers(
  storage: MarkerStorage,
  scope: DelegationContext,
): DelegationMarker[] {
  try {
    const prefix = context(scope);
    if (storage.length > 4096) throw trackingError();
    const result: DelegationMarker[] = [];
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      if (!key?.startsWith(prefix)) continue;
      const item = marker(JSON.parse(storage.getItem(key) ?? "null"));
      if (markerKey(item) !== key || context(item) !== prefix)
        throw trackingError();
      result.push(item);
      if (result.length > 100) throw trackingError();
    }
    return result;
  } catch {
    throw trackingError();
  }
}
export function retainDelegationMarker(
  storage: MarkerStorage,
  value: DelegationMarker,
  confirmed: ReadonlySet<string>,
): DelegationMarker {
  try {
    const item = marker(value),
      existing = readDelegationMarkers(storage, item),
      key = markerKey(item);
    if (
      existing.length >= 100 ||
      existing.some(
        (entry) => !confirmed.has(delegationMarkerBinding(entry)),
      ) ||
      storage.getItem(key) !== null
    )
      throw trackingError();
    const raw = JSON.stringify(item);
    storage.setItem(key, raw);
    if (storage.getItem(key) !== raw) throw trackingError();
    return marker(JSON.parse(raw));
  } catch {
    throw trackingError();
  }
}

export type DelegatedAction = "respond" | "status" | "cancel";
export type ScmProvider = "github" | "gitlab" | "bitbucket";
export type VerifiedReference = {
  version: 1;
  provider: ScmProvider;
  api_origin: string;
  installation_id: string | null;
  remote_repository_id: string;
  external_actor_id: string;
  web_origin: string;
  connection_revision: string;
  connection_scope_digest: string;
  webhook_key_revision: string;
  verification_method: "raw_body_hmac_sha256" | "gitlab_signing_hmac_sha256";
  payload_digest: string;
};
export type DelegationInput = {
  version: 1;
  request_id: string;
  expected_previous_digest: string | null;
  expected_project_binding_digest: string;
  verified_receipt_id: string;
  expected_verified_reference_digest: string;
  native_user_id: string;
  component_ids: string[];
  allowed_actions: DelegatedAction[];
  active: boolean;
  acknowledge_webhook_asserted_identity: true;
};
export type DelegationReceipt = {
  version: 1;
  event_id: string;
  project_id: string;
  repository_id: string;
  project_binding_digest: string;
  binding_key: string;
  revision: number;
  previous_digest: string | null;
  prepared_at: string;
  administering_user_id: string;
  request: DelegationInput;
  request_digest: string;
  verified_reference: VerifiedReference;
  digest: string;
};
export type DelegationCandidate = {
  verified_receipt_id: string;
  verified_reference: VerifiedReference;
  verified_reference_digest: string;
};
export type CandidatePage = {
  version: 1;
  project_id: string;
  repository_id: string;
  project_binding_digest: string;
  items: DelegationCandidate[];
  next_cursor: null;
};
export type DelegationHead = {
  version: 1;
  event_id: string;
  project_id: string;
  repository_id: string;
  project_binding_digest: string;
  request_id: string;
  binding_key: string;
  revision: number;
  digest: string;
  native_user_id: string;
  component_ids: string[];
  allowed_actions: DelegatedAction[];
  active: boolean;
  provider: ScmProvider;
  external_actor_id: string;
  prepared_at: string;
  summary_digest: string;
  current_head: true;
  head_status: "active" | "revoked" | "stale_project" | "stale_connection";
};
export type HeadPage = {
  version: 1;
  project_id: string;
  repository_id: string;
  project_binding_digest: string;
  heads_only: true;
  items: DelegationHead[];
  next_cursor: string | null;
};
type ReceiptScope = Pick<DelegationContext, "project" | "repository"> & {
  requestId?: string;
  actor?: string;
  inputDigest?: string;
  eventId?: string;
  receiptDigest?: string;
};
const contractError = () =>
  new Error(
    "The delegation response could not be verified for this project and repository. No new request was sent.",
  );
function check(value: unknown): asserts value {
  if (!value) throw contractError();
}
function object(
  value: unknown,
  keys: string[],
  maximum = 16 * 1024 * 1024,
): Record<string, unknown> {
  check(value && typeof value === "object" && !Array.isArray(value));
  const row = value as Record<string, unknown>;
  check(Object.keys(row).sort().join(",") === [...keys].sort().join(","));
  check(new TextEncoder().encode(JSON.stringify(value)).length <= maximum);
  return row;
}
function text(value: unknown, maximum: number): string {
  check(
    typeof value === "string" &&
      Array.from(value).length >= 1 &&
      Array.from(value).length <= maximum,
  );
  check(
    !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(
      value,
    ),
  );
  return value;
}
function matches(value: unknown, pattern: RegExp): string {
  const result = text(value, 500);
  check(pattern.test(result));
  return result;
}
function count(value: unknown): number {
  check(
    Number.isSafeInteger(value) &&
      (value as number) >= 1 &&
      (value as number) <= 50,
  );
  return value as number;
}
function nullableDigest(value: unknown): string | null {
  return value === null ? null : matches(value, hash);
}
function timestamp(value: unknown): string {
  const result = text(value, 50);
  check(Number.isFinite(Date.parse(result)));
  return result;
}
function provider(value: unknown): ScmProvider {
  check(["github", "gitlab", "bitbucket"].includes(value as string));
  return value as ScmProvider;
}
function providerId(value: unknown, kind: ScmProvider): string {
  return matches(
    value,
    kind === "bitbucket"
      ? /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/
      : /^[1-9][0-9]{0,39}$/,
  );
}
function origin(value: unknown): string {
  const result = text(value, 500),
    url = new URL(result),
    authority = result.slice("https://".length);
  check(
    url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      !url.hostname.endsWith(".") &&
      /^https:\/\/[^/?#\\\s]+$/.test(result) &&
      authority === authority.toLowerCase(),
  );
  return result;
}
function componentIds(value: unknown): string[] {
  check(Array.isArray(value) && value.length >= 1 && value.length <= 50);
  value.forEach((item) => matches(item, /^[a-z][a-z0-9-]{0,63}$/));
  check(new Set(value).size === value.length);
  return value as string[];
}
function actions(value: unknown): DelegatedAction[] {
  check(
    Array.isArray(value) &&
      value.length >= 1 &&
      value.length <= 3 &&
      value.every((item) => ["respond", "status", "cancel"].includes(item)) &&
      new Set(value).size === value.length,
  );
  return value as DelegatedAction[];
}
function scoped(
  row: Record<string, unknown>,
  scope: Pick<DelegationContext, "project" | "repository">,
) {
  check(
    matches(row.project_id, id) === scope.project &&
      matches(row.repository_id, id) === scope.repository,
  );
}
function reference(value: unknown): VerifiedReference {
  const row = object(
    value,
    [
      "version",
      "provider",
      "api_origin",
      "installation_id",
      "remote_repository_id",
      "external_actor_id",
      "web_origin",
      "connection_revision",
      "connection_scope_digest",
      "webhook_key_revision",
      "verification_method",
      "payload_digest",
    ],
    8192,
  );
  check(row.version === 1);
  const kind = provider(row.provider);
  origin(row.api_origin);
  origin(row.web_origin);
  providerId(row.remote_repository_id, kind);
  providerId(row.external_actor_id, kind);
  if (kind === "github") matches(row.installation_id, /^[1-9][0-9]{0,39}$/);
  else check(row.installation_id === null);
  matches(row.connection_revision, id);
  matches(row.connection_scope_digest, hash);
  matches(row.webhook_key_revision, id);
  matches(row.payload_digest, hash);
  check(
    row.verification_method ===
      (kind === "gitlab"
        ? "gitlab_signing_hmac_sha256"
        : "raw_body_hmac_sha256"),
  );
  return row as VerifiedReference;
}
export function delegationInput(value: unknown): DelegationInput {
  const row = object(
    value,
    [
      "version",
      "request_id",
      "expected_previous_digest",
      "expected_project_binding_digest",
      "verified_receipt_id",
      "expected_verified_reference_digest",
      "native_user_id",
      "component_ids",
      "allowed_actions",
      "active",
      "acknowledge_webhook_asserted_identity",
    ],
    65536,
  );
  check(
    row.version === 1 &&
      row.acknowledge_webhook_asserted_identity === true &&
      typeof row.active === "boolean",
  );
  matches(row.request_id, id);
  nullableDigest(row.expected_previous_digest);
  matches(row.expected_project_binding_digest, hash);
  matches(row.verified_receipt_id, id);
  matches(row.expected_verified_reference_digest, hash);
  matches(row.native_user_id, id);
  componentIds(row.component_ids);
  actions(row.allowed_actions);
  return row as DelegationInput;
}
export async function delegationBindingKey(
  project: string,
  value: VerifiedReference,
): Promise<string> {
  return attemptDigest({
    project_id: project,
    provider: value.provider,
    api_origin: value.api_origin,
    installation_id: value.installation_id,
    remote_repository_id: value.remote_repository_id,
    external_actor_id: value.external_actor_id,
  });
}
export async function readDelegationReceipt(
  value: unknown,
  scope: ReceiptScope,
): Promise<DelegationReceipt> {
  const row = object(
    value,
    [
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
    ],
    65536,
  );
  check(row.version === 1);
  scoped(row, scope);
  matches(row.event_id, id);
  matches(row.project_binding_digest, hash);
  matches(row.binding_key, hash);
  matches(row.administering_user_id, id);
  matches(row.request_digest, hash);
  matches(row.digest, hash);
  timestamp(row.prepared_at);
  const revision = count(row.revision),
    previous = nullableDigest(row.previous_digest),
    input = delegationInput(row.request),
    verified = reference(row.verified_reference);
  check((revision === 1) === (previous === null));
  check(
    input.expected_previous_digest === previous &&
      input.expected_project_binding_digest === row.project_binding_digest,
  );
  check(
    (await attemptDigest(input)) === row.request_digest &&
      (await attemptDigest(verified)) ===
        input.expected_verified_reference_digest &&
      (await delegationBindingKey(scope.project, verified)) === row.binding_key,
  );
  const { digest, ...body } = row;
  check((await attemptDigest(body)) === digest);
  if (scope.requestId !== undefined)
    check(input.request_id === scope.requestId);
  if (scope.actor !== undefined)
    check(row.administering_user_id === scope.actor);
  if (scope.inputDigest !== undefined)
    check(row.request_digest === scope.inputDigest);
  if (scope.eventId !== undefined) check(row.event_id === scope.eventId);
  if (scope.receiptDigest !== undefined)
    check(row.digest === scope.receiptDigest);
  return row as DelegationReceipt;
}
export async function readDelegationCandidates(
  value: unknown,
  scope: Pick<DelegationContext, "project" | "repository">,
): Promise<CandidatePage> {
  const row = object(value, [
    "version",
    "project_id",
    "repository_id",
    "project_binding_digest",
    "items",
    "next_cursor",
  ]);
  check(row.version === 1 && row.next_cursor === null);
  scoped(row, scope);
  matches(row.project_binding_digest, hash);
  check(Array.isArray(row.items) && row.items.length <= 50);
  const seen = new Set();
  for (const value of row.items) {
    const item = object(
        value,
        [
          "verified_receipt_id",
          "verified_reference",
          "verified_reference_digest",
        ],
        16384,
      ),
      ref = reference(item.verified_reference),
      key = matches(item.verified_receipt_id, id);
    check(!seen.has(key));
    seen.add(key);
    check(
      matches(item.verified_reference_digest, hash) ===
        (await attemptDigest(ref)),
    );
  }
  return row as CandidatePage;
}
export async function readDelegationHeads(
  value: unknown,
  scope: Pick<DelegationContext, "project" | "repository">,
  after: string | null = null,
): Promise<HeadPage> {
  const row = object(value, [
    "version",
    "project_id",
    "repository_id",
    "project_binding_digest",
    "heads_only",
    "items",
    "next_cursor",
  ]);
  check(row.version === 1 && row.heads_only === true);
  scoped(row, scope);
  matches(row.project_binding_digest, hash);
  check(Array.isArray(row.items) && row.items.length <= 50);
  let previous = after ?? "";
  const keys = new Set();
  if (after !== null) matches(after, id);
  for (const value of row.items) {
    const item = object(
      value,
      [
        "version",
        "event_id",
        "project_id",
        "repository_id",
        "project_binding_digest",
        "request_id",
        "binding_key",
        "revision",
        "digest",
        "native_user_id",
        "component_ids",
        "allowed_actions",
        "active",
        "provider",
        "external_actor_id",
        "prepared_at",
        "summary_digest",
        "current_head",
        "head_status",
      ],
      16384,
    );
    check(
      item.version === 1 &&
        item.current_head === true &&
        typeof item.active === "boolean",
    );
    scoped(item, scope);
    const event = matches(item.event_id, id);
    check(event > previous);
    previous = event;
    matches(item.request_id, id);
    matches(item.native_user_id, id);
    matches(item.project_binding_digest, hash);
    matches(item.digest, hash);
    matches(item.summary_digest, hash);
    count(item.revision);
    timestamp(item.prepared_at);
    componentIds(item.component_ids);
    actions(item.allowed_actions);
    providerId(item.external_actor_id, provider(item.provider));
    const key = matches(item.binding_key, hash);
    check(!keys.has(key));
    keys.add(key);
    check(
      ["active", "revoked", "stale_project", "stale_connection"].includes(
        item.head_status as string,
      ),
    );
    if (item.head_status === "active" || item.head_status === "revoked")
      check(
        item.active === (item.head_status === "active") &&
          item.project_binding_digest === row.project_binding_digest,
      );
    const { summary_digest, current_head, head_status, ...body } = item;
    void current_head;
    void head_status;
    check((await attemptDigest(body)) === summary_digest);
  }
  check(
    row.next_cursor === null ||
      (row.items.length === 50 && matches(row.next_cursor, id) === previous),
  );
  return row as HeadPage;
}
