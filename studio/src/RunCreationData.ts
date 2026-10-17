/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { normalizeBase, request, type Connection, type ForgeRun } from "./api";
import { attemptDigest } from "./LearningObservationAttemptData";
export type RunCreationScope = {
  base: string;
  actor: string;
  repository: string;
  project: string | null;
  component: string | null;
};
export type CreationMarker = RunCreationScope & {
  version: 1;
  requestId: string;
  inputDigest: string;
  body: string;
};
export type CreationReceipt = {
  version: 1;
  request_id: string;
  original_actor: { user_id: string; authentication_method: string };
  request_digest: string;
  input: Record<string, unknown>;
  repository_id: string;
  project_id: string | null;
  component_id: string | null;
  run_id: string;
  admitted_at: string;
  admission_digest: string;
  digest: string;
};
type Disk = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export type CreationLocks = {
  request: <T>(
    name: string,
    options: { mode: "exclusive" },
    callback: () => T | PromiseLike<T>,
  ) => Promise<T>;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const failure = () =>
  new Error(
    "We could not confirm whether this run started. Keep this request and check its result before starting another run.",
  );
function check(value: unknown): asserts value {
  if (!value) throw failure();
}
function object(value: unknown): Record<string, unknown> {
  check(value !== null && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function keys(value: Record<string, unknown>, names: string[]) {
  check(Object.keys(value).sort().join(",") === names.sort().join(","));
}
function bytes(value: string) {
  return new TextEncoder().encode(value).byteLength;
}
function scopeKey(scope: RunCreationScope) {
  const base = new URL(scope.base);
  check(
    ["http:", "https:"].includes(base.protocol) &&
      !base.username &&
      !base.password &&
      !base.search &&
      !base.hash &&
      normalizeBase(scope.base) === scope.base,
  );
  check(
    id.test(scope.actor) &&
      id.test(scope.repository) &&
      (scope.project === null || id.test(scope.project)) &&
      (scope.component === null ||
        /^[a-z][a-z0-9-]{0,63}$/.test(scope.component)) &&
      Boolean(scope.project) === Boolean(scope.component),
  );
  return (
    "forge.run-creation.v1:" +
    encodeURIComponent(
      JSON.stringify([
        scope.base,
        scope.actor,
        scope.repository,
        scope.project,
        scope.component,
      ]),
    )
  );
}
export function normalizeRunBrief(value: string): string {
  return value.replace(
    // eslint-disable-next-line no-control-regex
    /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g,
    "",
  );
}
function input(
  value: unknown,
  scope: RunCreationScope,
): Record<string, unknown> {
  const row = object(value);
  check(
    row.repository_id === scope.repository &&
      (row.project_id ?? null) === scope.project &&
      (row.component_id ?? null) === scope.component &&
      typeof row.brief === "string" &&
      row.brief === normalizeRunBrief(row.brief) &&
      Array.from(row.brief).length > 0 &&
      Array.from(row.brief).length <= 40000,
  );
  const allowed = new Set([
    "repository_id",
    "project_id",
    "component_id",
    "brief",
    "backend",
    "frontend",
    "agent",
    "operation",
    "commands",
    "publish",
    "product_definition",
    "skills",
    "assurance",
  ]);
  check(Object.keys(row).every((key) => allowed.has(key)));
  return row;
}
async function marker(
  value: unknown,
  scope: RunCreationScope,
): Promise<CreationMarker> {
  const row = object(value);
  keys(row, [
    "base",
    "actor",
    "repository",
    "project",
    "component",
    "version",
    "requestId",
    "inputDigest",
    "body",
  ]);
  check(
    row.version === 1 &&
      typeof row.requestId === "string" &&
      id.test(row.requestId) &&
      typeof row.inputDigest === "string" &&
      hash.test(row.inputDigest) &&
      typeof row.body === "string" &&
      bytes(row.body) <= 256 * 1024,
  );
  check(scopeKey(row as CreationMarker) === scopeKey(scope));
  const body = object(JSON.parse(row.body));
  keys(body, ["version", "input"]);
  check(body.version === 1);
  input(body.input, scope);
  check((await attemptDigest(body)) === row.inputDigest);
  return structuredClone(row) as CreationMarker;
}
export async function makeCreationMarker(
  scope: RunCreationScope,
  value: Record<string, unknown>,
): Promise<CreationMarker> {
  scopeKey(scope);
  const submitted = structuredClone(value);
  check(typeof submitted.brief === "string");
  submitted.brief = normalizeRunBrief(submitted.brief);
  input(submitted, scope);
  const envelope = { version: 1, input: submitted },
    body = JSON.stringify(envelope);
  check(bytes(body) <= 256 * 1024);
  return marker(
    {
      ...scope,
      version: 1,
      requestId: crypto.randomUUID().replace(/-/g, ""),
      inputDigest: await attemptDigest(envelope),
      body,
    },
    scope,
  );
}
export async function readCreationMarker(
  disk: Disk,
  scope: RunCreationScope,
): Promise<CreationMarker | null> {
  const raw = disk.getItem(scopeKey(scope));
  if (raw === null) return null;
  check(bytes(raw) <= 600 * 1024);
  return marker(JSON.parse(raw), scope);
}
export async function retainCreationMarker(
  disk: Disk,
  value: CreationMarker,
  locks: CreationLocks | undefined,
  isCurrent: () => boolean,
): Promise<CreationMarker> {
  const frozen = await marker(value, value);
  check(locks && typeof locks.request === "function");
  return locks.request(scopeKey(frozen), { mode: "exclusive" }, () => {
    check(isCurrent());
    const key = scopeKey(frozen);
    check(disk.getItem(key) === null);
    const encoded = JSON.stringify(frozen);
    check(bytes(encoded) <= 600 * 1024);
    disk.setItem(key, encoded);
    check(disk.getItem(key) === encoded);
    return structuredClone(frozen);
  });
}
export async function clearCreationMarker(
  disk: Disk,
  value: CreationMarker,
  locks: CreationLocks | undefined,
  isCurrent: () => boolean,
): Promise<void> {
  const frozen = await marker(value, value);
  check(locks && typeof locks.request === "function");
  await locks.request(scopeKey(frozen), { mode: "exclusive" }, async () => {
    check(isCurrent());
    const current = await readCreationMarker(disk, frozen);
    check(
      isCurrent() &&
        current?.requestId === frozen.requestId &&
        current.inputDigest === frozen.inputDigest &&
        current.body === frozen.body,
    );
    disk.removeItem(scopeKey(frozen));
    check(disk.getItem(scopeKey(frozen)) === null);
  });
}
async function receipt(
  value: unknown,
  original: CreationMarker,
): Promise<CreationReceipt> {
  const row = object(value);
  check(bytes(JSON.stringify(row)) <= 256 * 1024);
  keys(row, [
    "version",
    "request_id",
    "original_actor",
    "request_digest",
    "input",
    "repository_id",
    "project_id",
    "component_id",
    "run_id",
    "admitted_at",
    "admission_digest",
    "digest",
  ]);
  const actor = object(row.original_actor);
  keys(actor, ["user_id", "authentication_method"]);
  check(
    actor.user_id === original.actor &&
      ["oidc_session", "oidc_bearer", "local_bootstrap"].includes(
        String(actor.authentication_method),
      ),
  );
  check(
    row.version === 1 &&
      row.request_id === original.requestId &&
      row.request_digest === original.inputDigest &&
      row.repository_id === original.repository &&
      row.project_id === original.project &&
      row.component_id === original.component &&
      typeof row.run_id === "string" &&
      id.test(row.run_id) &&
      typeof row.admitted_at === "string" &&
      row.admitted_at.length > 0 &&
      row.admitted_at.length <= 80 &&
      typeof row.admission_digest === "string" &&
      hash.test(row.admission_digest) &&
      typeof row.digest === "string" &&
      hash.test(row.digest),
  );
  input(row.input, original);
  check(
    (await attemptDigest({ version: 1, input: row.input })) ===
      original.inputDigest,
  );
  const unsigned = { ...row };
  delete unsigned.digest;
  check((await attemptDigest(unsigned)) === row.digest);
  return structuredClone(row) as CreationReceipt;
}
function connected(connection: Connection, value: CreationMarker) {
  check(
    normalizeBase(connection.base) === value.base ||
      (normalizeBase(connection.base) === "/api/v1" &&
        typeof location !== "undefined" &&
        new URL("/api/v1", location.origin).href === value.base),
  );
}
export async function sendCreation(
  connection: Connection,
  value: CreationMarker,
  isCurrent: () => boolean,
): Promise<CreationReceipt> {
  const frozen = await marker(value, value);
  connected(connection, frozen);
  check(isCurrent());
  const answer = await request<unknown>(
    connection,
    "/run-creation-requests/" + frozen.requestId,
    { method: "POST", body: frozen.body },
  );
  const result = await receipt(answer, frozen);
  check(isCurrent());
  return result;
}
export async function recoverCreation(
  connection: Connection,
  value: CreationMarker,
  isCurrent: () => boolean,
): Promise<CreationReceipt> {
  const frozen = await marker(value, value);
  connected(connection, frozen);
  check(isCurrent());
  const answer = await request<unknown>(
    connection,
    "/run-creation-requests/" + frozen.requestId,
  );
  const result = await receipt(answer, frozen);
  check(isCurrent());
  return result;
}
export async function readCreatedRun(
  connection: Connection,
  value: CreationReceipt,
  isCurrent: () => boolean,
): Promise<ForgeRun> {
  check(isCurrent());
  const run = await request<ForgeRun>(connection, "/runs/" + value.run_id);
  check(
    isCurrent() &&
      run.id === value.run_id &&
      run.repository_id === value.repository_id &&
      (run.project_id ?? null) === value.project_id &&
      (run.component_id ?? null) === value.component_id,
  );
  return run;
}
