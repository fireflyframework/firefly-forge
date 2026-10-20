/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";

export const allowanceProtocol = "native-provider-recovery-allowance-v1";
export type AllowanceContext = {
  base: string;
  actor: string;
  authentication: string;
  run: string;
  repository: string;
  project: string | null;
  agent: "codex" | "claude";
};
export type AllowanceReview = {
  digest: string;
  agent: "codex" | "claude";
  nativeProcessDigest: string;
  sourceDigest: string;
  gitDigest: string;
  configRevision: string;
  failedTaskKey: string;
  executablePath: string;
  executableSha256: string;
};
export type AllowanceInput = {
  request_id: string;
  candidate_digest: string;
  native_process_digest: string;
  agent: "codex" | "claude";
  reason: string;
  mitigation_evidence_digest: string;
  adopt_current_executable: true;
};
export type AllowanceMarker = {
  version: 1;
  context: AllowanceContext;
  review: AllowanceReview;
  body: AllowanceInput;
  inputDigest: string;
};
type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;
export type AllowanceLocks = {
  request<T>(
    key: string,
    options: { mode: "exclusive" },
    call: () => Promise<T>,
  ): Promise<T>;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const problem = () =>
  new Error(
    "The final allowance identity could not be verified. No replacement request was sent.",
  );
function requireValue(value: unknown): asserts value {
  if (!value) throw problem();
}
function record(value: unknown): Record<string, unknown> {
  requireValue(value && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function keys(value: Record<string, unknown>, expected: string[]) {
  requireValue(
    Object.keys(value).sort().join() === [...expected].sort().join(),
  );
}
function text(value: unknown, maximum: number): string {
  requireValue(
    typeof value === "string" &&
      Array.from(value).length > 0 &&
      Array.from(value).length <= maximum &&
      !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(
        value,
      ),
  );
  return value;
}
function match(value: unknown, pattern: RegExp): string {
  const v = text(value, 256);
  requireValue(pattern.test(v));
  return v;
}
function review(value: unknown, agent: string): AllowanceReview {
  const r = record(value);
  keys(r, [
    "digest",
    "agent",
    "nativeProcessDigest",
    "sourceDigest",
    "gitDigest",
    "configRevision",
    "failedTaskKey",
    "executablePath",
    "executableSha256",
  ]);
  requireValue(r.agent === agent && ["codex", "claude"].includes(agent));
  for (const k of [
    "digest",
    "nativeProcessDigest",
    "sourceDigest",
    "gitDigest",
    "executableSha256",
  ])
    match(r[k], hash);
  match(r.configRevision, id);
  match(r.failedTaskKey, id);
  const path = text(r.executablePath, 4096);
  requireValue(
    path.startsWith("/") &&
      !Array.from(path).some((character) => {
        const code = character.codePointAt(0)!;
        return code < 32 || code === 127;
      }) &&
      !path.split("/").includes(".."),
  );
  return r as AllowanceReview;
}
export function allowanceCandidate(
  value: unknown,
  context: AllowanceContext,
): AllowanceReview {
  requireValue(
    new TextEncoder().encode(JSON.stringify(value)).length <= 256 * 1024,
  );
  const r = record(value);
  keys(r, [
    "version",
    "protocol",
    "operator_digest",
    "run_id",
    "agent",
    "config_revision",
    "failed_task_key",
    "failed_intent_digest",
    "failure_digest",
    "journal_digest",
    "dispatches_used",
    "next_attempt",
    "process_evidence",
    "native_process_digest",
    "cut",
    "executable_adoption",
    "digest",
  ]);
  requireValue(
    r.version === 1 &&
      r.protocol === allowanceProtocol &&
      r.run_id === context.run &&
      r.agent === context.agent &&
      r.dispatches_used === 4 &&
      r.next_attempt === 5 &&
      r.process_evidence === "native_cli_process_receipt",
  );
  for (const k of [
    "operator_digest",
    "failed_intent_digest",
    "failure_digest",
    "journal_digest",
    "native_process_digest",
    "digest",
  ])
    match(r[k], hash);
  const cut = record(r.cut),
    executable = record(r.executable_adoption);
  keys(cut, [
    "source_digest",
    "git_digest",
    "index_digest",
    "metadata_digest",
    "scope_digest",
    "invocation_digest",
    "commands_digest",
    "policy_digest",
    "provider_digest",
    "retained_state_digest",
  ]);
  for (const [key, v] of Object.entries(cut))
    if (key !== "index_digest" || v !== null) match(v, hash);
  keys(executable, [
    "path",
    "sha256",
    "dev",
    "ino",
    "size",
    "mode",
    "uid",
    "gid",
    "mtime_ns",
    "ctime_ns",
  ]);
  for (const key of [
    "dev",
    "ino",
    "size",
    "mode",
    "uid",
    "gid",
    "mtime_ns",
    "ctime_ns",
  ])
    requireValue(
      typeof executable[key] === "number" &&
        Number.isInteger(executable[key]) &&
        Number(executable[key]) >= 0 &&
        Number(executable[key]) <= 2 ** 64,
    );
  requireValue(
    Number(executable.size) > 0 && Number(executable.size) <= 512 * 1024 * 1024,
  );
  // Native nanosecond metadata can exceed JS's safe integer range. The server
  // digest stays opaque; this view does not rehash that rounded JSON packet.
  return review(
    {
      digest: r.digest,
      agent: r.agent,
      nativeProcessDigest: r.native_process_digest,
      sourceDigest: cut.source_digest,
      gitDigest: cut.git_digest,
      configRevision: r.config_revision,
      failedTaskKey: r.failed_task_key,
      executablePath: executable.path,
      executableSha256: executable.sha256,
    },
    context.agent,
  );
}
export function allowanceInput(
  view: AllowanceReview,
  requestId: string,
  reason: string,
  evidence: string,
): AllowanceInput {
  match(requestId, id);
  match(evidence, hash);
  text(reason, 2000);
  requireValue(reason.trim().length > 0);
  review(view, view.agent);
  return {
    request_id: requestId,
    candidate_digest: view.digest,
    native_process_digest: view.nativeProcessDigest,
    agent: view.agent,
    reason,
    mitigation_evidence_digest: evidence,
    adopt_current_executable: true,
  };
}
function input(value: unknown, view: AllowanceReview): AllowanceInput {
  const b = record(value);
  keys(b, [
    "request_id",
    "candidate_digest",
    "native_process_digest",
    "agent",
    "reason",
    "mitigation_evidence_digest",
    "adopt_current_executable",
  ]);
  const expected = allowanceInput(
    view,
    match(b.request_id, id),
    text(b.reason, 2000),
    match(b.mitigation_evidence_digest, hash),
  );
  requireValue(Object.entries(expected).every(([k, v]) => b[k] === v));
  return expected;
}
export function allowanceMarkerKey(context: AllowanceContext): string {
  return "forge.provider-recovery-allowance.v1:" + JSON.stringify(context);
}
function validateContext(
  value: unknown,
  expected: AllowanceContext,
): AllowanceContext {
  const c = record(value);
  keys(c, [
    "base",
    "actor",
    "authentication",
    "run",
    "repository",
    "project",
    "agent",
  ]);
  requireValue(Object.entries(expected).every(([k, v]) => c[k] === v));
  for (const k of ["actor", "run", "repository"]) match(c[k], id);
  if (c.project !== null) match(c.project, id);
  const url = new URL(text(c.base, 4096));
  requireValue(
    ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      c.authentication !== "scm_mapping" &&
      ["codex", "claude"].includes(String(c.agent)),
  );
  text(c.authentication, 80);
  return c as AllowanceContext;
}
export async function readAllowanceMarker(
  raw: string | null,
  context: AllowanceContext,
): Promise<AllowanceMarker | null> {
  if (raw === null) return null;
  requireValue(raw.length <= 24 * 1024);
  const m = record(JSON.parse(raw));
  keys(m, ["version", "context", "review", "body", "inputDigest"]);
  requireValue(m.version === 1);
  const c = validateContext(m.context, context),
    r = review(m.review, c.agent),
    b = input(m.body, r);
  requireValue(
    match(m.inputDigest, hash) ===
      (await attemptDigest({ context: c, review: r, body: b })),
  );
  return {
    version: 1,
    context: c,
    review: r,
    body: b,
    inputDigest: m.inputDigest as string,
  };
}
export async function retainAllowance(
  storage: Storage,
  locks: AllowanceLocks | undefined,
  context: AllowanceContext,
  selected: AllowanceReview,
  body: AllowanceInput,
): Promise<AllowanceMarker> {
  requireValue(locks && typeof locks.request === "function");
  return await locks.request(
    allowanceMarkerKey(context),
    { mode: "exclusive" },
    async () => {
      const key = allowanceMarkerKey(context);
      requireValue(storage.getItem(key) === null);
      const c = validateContext(context, context),
        r = review(selected, c.agent),
        b = input(body, r);
      const marker: AllowanceMarker = {
        version: 1,
        context: c,
        review: r,
        body: b,
        inputDigest: await attemptDigest({ context: c, review: r, body: b }),
      };
      const raw = JSON.stringify(marker);
      storage.setItem(key, raw);
      requireValue(storage.getItem(key) === raw);
      await readAllowanceMarker(raw, context);
      return marker;
    },
  );
}
export async function allowanceReceipt(
  value: unknown,
  marker: Pick<AllowanceMarker, "context" | "review" | "body">,
): Promise<{ requestId: string; attempt: 5 }> {
  requireValue(
    new TextEncoder().encode(JSON.stringify(value)).length <= 256 * 1024,
  );
  const r = record(value);
  keys(r, [
    "version",
    "protocol",
    "request",
    "candidate",
    "actor",
    "task_key",
    "previous_task",
    "process_evidence",
    "mitigation_evidence",
  ]);
  requireValue(r.version === 2 && r.protocol === allowanceProtocol);
  const body = input(r.request, marker.review);
  requireValue(
    (await attemptDigest(body)) === (await attemptDigest(marker.body)),
  );
  const actor = record(r.actor),
    previous = record(r.previous_task),
    process = record(r.process_evidence),
    mitigation = record(r.mitigation_evidence);
  keys(actor, [
    "user_id",
    "authentication_method",
    "global_admin",
    "api_audience",
    "identity_digest",
  ]);
  requireValue(
    actor.user_id === marker.context.actor &&
      actor.authentication_method === marker.context.authentication &&
      actor.global_admin === true,
  );
  match(actor.identity_digest, hash);
  requireValue(
    actor.api_audience === null || typeof actor.api_audience === "string",
  );
  const view = allowanceCandidate(r.candidate, marker.context);
  requireValue(
    (await attemptDigest(view)) === (await attemptDigest(marker.review)),
  );
  requireValue(
    previous.id === "implement" &&
      previous.status === "failed" &&
      previous.attempt === 4 &&
      previous.idempotency_key === view.failedTaskKey,
  );
  keys(process, ["provenance", "digest"]);
  keys(mitigation, ["provenance", "digest"]);
  requireValue(
    process.provenance === "native_cli_process_receipt" &&
      process.digest === body.native_process_digest &&
      mitigation.provenance === "administrator_attestation" &&
      mitigation.digest === body.mitigation_evidence_digest,
  );
  const taskKey = (
    await attemptDigest({
      protocol: allowanceProtocol,
      run: marker.context.run,
      request: body.request_id,
    })
  ).slice(0, 32);
  requireValue(r.task_key === taskKey);
  return { requestId: body.request_id, attempt: 5 };
}

export function existingAllowanceId(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const r = value as Record<string, unknown>;
  return typeof r.allowance_id === "string" &&
    id.test(r.allowance_id) &&
    r.request_id === r.allowance_id &&
    r.attempt === 5 &&
    typeof r.allowance_digest === "string" &&
    hash.test(r.allowance_digest)
    ? r.allowance_id
    : null;
}

export async function allowanceReadback(
  value: unknown,
  context: AllowanceContext,
  requestId: string,
): Promise<{ requestId: string; attempt: 5 }> {
  match(requestId, id);
  validateContext(context, context);
  requireValue(
    new TextEncoder().encode(JSON.stringify(value)).length <= 256 * 1024,
  );
  const packet = record(value),
    selected = allowanceCandidate(packet.candidate, context),
    body = input(packet.request, selected);
  requireValue(body.request_id === requestId);
  // This validates the server's existing receipt, not a previously saved local body.
  return allowanceReceipt(value, { context, review: selected, body });
}
