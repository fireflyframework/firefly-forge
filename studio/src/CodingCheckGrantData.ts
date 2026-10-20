/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  allowanceCandidate,
  allowanceInput,
  allowanceMarkerKey,
  allowanceReceipt,
  type AllowanceContext,
  type AllowanceInput,
  type AllowanceLocks,
  type AllowanceReview,
} from "./ProviderRecoveryAllowanceData";

export const codingProtocol = "native-coding-configured-check-v1";
export const codingStateEvent = "forge-provider-recovery-state";
type Data = Record<string, unknown>;
export type CodingCandidate = {
  version: 1;
  protocol: typeof codingProtocol;
  run_id: string;
  agent: "codex" | "claude";
  recovery_request_id: string;
  task_key: string;
  task_attempt: 5;
  revision: number;
  actor_digest: string;
  cut: Record<string, string>;
  source_digest: string;
  command_indices: number[];
  command_count: number;
  commands: { command_index: number; argv: string[]; timeout: number }[];
  runner: {
    mode: "host" | "docker";
    configuration: Data;
    digest: string;
    effective_network: string;
    enforced_controls: string[];
    reason: string;
  };
  channel: {
    transport: "streamable_http";
    host: "127.0.0.1";
    bind_port: number;
  };
  independent_final_check_count: number;
  shared_command_limit: number;
  shared_commands_used: number;
  shared_commands_remaining: number;
  interactive_commands_remaining: number;
  journal_digest: string;
  digest: string;
};
export type CodingInput = {
  request_id: string;
  recovery_request_id: string;
  candidate_digest: string;
  expected_revision: number;
  command_indices: number[];
  call_limit: number;
  valid_seconds: number;
  bind_port: number;
  reason: string;
};
export type LinkedAllowance = AllowanceInput & {
  coding_check_grant_id: string;
};
export type CodingMarker = {
  version: 1;
  kind: "coding-check-recovery";
  context: AllowanceContext;
  candidate: CodingCandidate;
  grant: CodingInput;
  inputDigest: string;
  allowance: null | {
    review: AllowanceReview;
    body: LinkedAllowance;
    inputDigest: string;
  };
};
export type CodingReceipt = {
  grantId: string;
  revision: number;
  receiptDigest: string;
  expires_at_ms: number;
  callLimit: number;
};
type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;
const hash = /^[a-f0-9]{64}$/,
  id = /^[a-f0-9]{32}$/;
function valid(value: unknown): asserts value {
  if (!value)
    throw new Error(
      "The configured-check recovery identity could not be verified. Keep the saved request; no replacement will be sent.",
    );
}
function object(value: unknown): Data {
  valid(value && typeof value === "object" && !Array.isArray(value));
  return value as Data;
}
function keys(value: Data, names: string[]) {
  valid(Object.keys(value).sort().join() === names.sort().join());
}
function text(value: unknown, max = 4096): string {
  valid(
    typeof value === "string" &&
      value.length > 0 &&
      Array.from(value).length <= max &&
      !value.includes("\0") &&
      !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(
        value,
      ),
  );
  return value;
}
function match(value: unknown, pattern: RegExp) {
  const s = text(value);
  valid(pattern.test(s));
  return s;
}
function integer(value: unknown, min: number, max: number): number {
  valid(
    typeof value === "number" &&
      Number.isSafeInteger(value) &&
      value >= min &&
      value <= max,
  );
  return value;
}
function bounded(value: unknown, max = 256 * 1024) {
  valid(new TextEncoder().encode(JSON.stringify(value)).length <= max);
}
export function codingPort(value: unknown) {
  const n = integer(value, 0, 65535);
  valid(n === 0 || n >= 1024);
  return n;
}
function indices(value: unknown) {
  valid(Array.isArray(value) && value.length >= 1 && value.length <= 20);
  const list = value.map((v) => integer(v, 0, 19));
  valid(new Set(list).size === list.length);
  return list;
}
function same(a: unknown, b: unknown) {
  return attemptDigest(a).then((d) => attemptDigest(b).then((e) => d === e));
}
function validateContext(context: AllowanceContext) {
  for (const value of [context.actor, context.run, context.repository])
    match(value, id);
  if (context.project !== null) match(context.project, id);
  valid(
    ["codex", "claude"].includes(context.agent) &&
      context.authentication !== "scm_mapping",
  );
  text(context.authentication, 80);
  const url = new URL(context.base);
  valid(
    ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash,
  );
}
export function codingCandidate(
  value: unknown,
  context: AllowanceContext,
  recoveryId: string,
  port: number,
): CodingCandidate {
  bounded(value);
  validateContext(context);
  match(recoveryId, id);
  codingPort(port);
  const c = object(value);
  keys(c, [
    "version",
    "protocol",
    "run_id",
    "agent",
    "recovery_request_id",
    "task_key",
    "task_attempt",
    "revision",
    "actor_digest",
    "cut",
    "source_digest",
    "command_indices",
    "command_count",
    "commands",
    "runner",
    "channel",
    "independent_final_check_count",
    "shared_command_limit",
    "shared_commands_used",
    "shared_commands_remaining",
    "interactive_commands_remaining",
    "journal_digest",
    "digest",
  ]);
  valid(
    c.version === 1 &&
      c.protocol === codingProtocol &&
      c.run_id === context.run &&
      c.agent === context.agent &&
      c.recovery_request_id === recoveryId &&
      c.task_attempt === 5,
  );
  match(c.task_key, id);
  integer(c.revision, 0, Number.MAX_SAFE_INTEGER);
  for (const k of ["actor_digest", "source_digest", "journal_digest", "digest"])
    match(c[k], hash);
  const cut = object(c.cut);
  valid(Object.keys(cut).length >= 2 && Object.keys(cut).length <= 16);
  for (const [k, v] of Object.entries(cut)) {
    valid(/^[a-z_]+_digest$/.test(k));
    match(v, hash);
  }
  match(cut.sandbox_digest, hash);
  match(cut.scope_digest, hash);
  const selected = indices(c.command_indices),
    count = integer(c.command_count, 1, 20);
  valid(selected.length === count && selected.every((v, i) => v === i));
  valid(Array.isArray(c.commands) && c.commands.length === count);
  for (const [i, value] of c.commands.entries()) {
    const command = object(value);
    keys(command, ["command_index", "argv", "timeout"]);
    valid(
      integer(command.command_index, 0, 19) === i &&
        Array.isArray(command.argv) &&
        command.argv.length >= 1 &&
        command.argv.length <= 64,
    );
    for (const arg of command.argv) text(arg, 8192);
    integer(command.timeout, 1, 3600);
  }
  const runner = object(c.runner);
  keys(runner, [
    "mode",
    "configuration",
    "digest",
    "effective_network",
    "enforced_controls",
    "reason",
  ]);
  valid(["host", "docker"].includes(String(runner.mode)));
  match(runner.digest, hash);
  text(runner.reason, 4096);
  const config = object(runner.configuration);
  valid(config.mode === runner.mode);
  const configKeys = [
    "mode",
    "image",
    "context",
    "workspace_transport",
    "workspace_max_bytes",
    "cpus",
    "memory_mb",
    "pids_limit",
    "timeout",
    "max_output_bytes",
    "network",
    "command_limit",
    "browser",
    "internet_search",
  ];
  if ("pool_binding" in config) configKeys.push("pool_binding");
  keys(config, configKeys);
  integer(config.timeout, 1, 3600);
  integer(config.max_output_bytes, 1024, 2000000);
  integer(config.command_limit, 1, 100);
  integer(config.memory_mb, 128, 8192);
  integer(config.pids_limit, 32, 512);
  integer(config.workspace_max_bytes, 1048576, 268435456);
  valid(
    typeof config.cpus === "number" &&
      Number.isFinite(config.cpus) &&
      config.cpus >= 0.1 &&
      config.cpus <= 4,
  );
  valid(
    typeof config.internet_search === "boolean" &&
      ["bind", "archive"].includes(String(config.workspace_transport)) &&
      ["none", "egress"].includes(String(config.network)),
  );
  valid(
    Array.isArray(runner.enforced_controls) &&
      runner.enforced_controls.every(
        (v) => typeof v === "string" && v.length <= 64,
      ),
  );
  if (runner.mode === "host")
    valid(
      runner.effective_network === "host" &&
        config.image === null &&
        config.context === null &&
        config.browser === null &&
        config.workspace_transport === "bind" &&
        config.network === "none" &&
        !("pool_binding" in config) &&
        runner.enforced_controls.join() === "timeout,output",
    );
  else {
    text(config.context, 100);
    valid(
      /^[a-z0-9][a-z0-9._:/-]{0,255}@sha256:[a-f0-9]{64}$/.test(
        text(config.image, 512),
      ),
    );
    valid(
      ["none", "egress", "unavailable"].includes(
        String(runner.effective_network),
      ),
    );
  }
  const channel = object(c.channel);
  keys(channel, ["transport", "host", "bind_port"]);
  valid(
    channel.transport === "streamable_http" &&
      channel.host === "127.0.0.1" &&
      codingPort(channel.bind_port) === port,
  );
  const final = integer(c.independent_final_check_count, 1, 20),
    limit = integer(c.shared_command_limit, 1, 100),
    used = integer(c.shared_commands_used, 0, 100),
    remaining = integer(c.shared_commands_remaining, 0, 100);
  valid(
    final >= count &&
      used + remaining === limit &&
      config.command_limit === limit &&
      remaining >= final &&
      integer(c.interactive_commands_remaining, 0, 100) === remaining - final,
  );
  // The server's candidate digest is opaque, including native numeric metadata.
  return structuredClone(c) as CodingCandidate;
}
export function codingInput(
  candidate: CodingCandidate,
  requestId: string,
  selected: number[],
  calls: number,
  seconds: number,
  reason: string,
): CodingInput {
  match(requestId, id);
  const list = indices(selected);
  valid(list.every((i) => candidate.command_indices.includes(i)));
  integer(calls, 1, Math.min(20, candidate.interactive_commands_remaining));
  integer(seconds, 30, 3600);
  text(reason, 2000);
  valid(reason.trim().length > 0);
  return {
    request_id: requestId,
    recovery_request_id: candidate.recovery_request_id,
    candidate_digest: candidate.digest,
    expected_revision: candidate.revision,
    command_indices: list,
    call_limit: calls,
    valid_seconds: seconds,
    bind_port: candidate.channel.bind_port,
    reason,
  };
}
function parseInput(value: unknown, candidate: CodingCandidate) {
  const b = object(value);
  keys(b, [
    "request_id",
    "recovery_request_id",
    "candidate_digest",
    "expected_revision",
    "command_indices",
    "call_limit",
    "valid_seconds",
    "bind_port",
    "reason",
  ]);
  const parsed = codingInput(
    candidate,
    match(b.request_id, id),
    indices(b.command_indices),
    integer(b.call_limit, 1, 20),
    integer(b.valid_seconds, 30, 3600),
    text(b.reason, 2000),
  );
  valid(
    b.recovery_request_id === parsed.recovery_request_id &&
      b.candidate_digest === parsed.candidate_digest &&
      b.expected_revision === parsed.expected_revision &&
      b.bind_port === parsed.bind_port,
  );
  return parsed;
}
export async function readCodingMarker(
  raw: string | null,
  context: AllowanceContext,
): Promise<CodingMarker | null> {
  if (raw === null) return null;
  valid(raw.length <= 512 * 1024);
  const m = object(JSON.parse(raw));
  keys(m, [
    "version",
    "kind",
    "context",
    "candidate",
    "grant",
    "inputDigest",
    "allowance",
  ]);
  valid(
    m.version === 1 &&
      m.kind === "coding-check-recovery" &&
      (await same(m.context, context)),
  );
  validateContext(context);
  const b = object(m.grant),
    c = codingCandidate(
      m.candidate,
      context,
      match(b.recovery_request_id, id),
      codingPort(b.bind_port),
    ),
    grant = parseInput(b, c);
  valid(
    match(m.inputDigest, hash) ===
      (await attemptDigest({ context, candidate: c, grant })),
  );
  let allowance: CodingMarker["allowance"] = null;
  if (m.allowance !== null) {
    const a = object(m.allowance);
    keys(a, ["review", "body", "inputDigest"]);
    const body = object(a.body),
      original = object(a.review);
    const expected = {
      ...allowanceInput(
        original as AllowanceReview,
        grant.recovery_request_id,
        text(body.reason, 2000),
        match(body.mitigation_evidence_digest, hash),
      ),
      coding_check_grant_id: grant.request_id,
    };
    valid(
      (await same(body, expected)) &&
        match(a.inputDigest, hash) ===
          (await attemptDigest({ review: original, body: expected })),
    );
    allowance = {
      review: original as AllowanceReview,
      body: expected,
      inputDigest: a.inputDigest as string,
    };
  }
  return {
    version: 1,
    kind: "coding-check-recovery",
    context,
    candidate: c,
    grant,
    inputDigest: m.inputDigest as string,
    allowance,
  };
}
function announce() {
  if (typeof window !== "undefined")
    window.dispatchEvent(new Event(codingStateEvent));
}
export async function retainCodingGrant(
  storage: Storage,
  locks: AllowanceLocks | undefined,
  context: AllowanceContext,
  candidate: CodingCandidate,
  grant: CodingInput,
): Promise<CodingMarker> {
  valid(locks && typeof locks.request === "function");
  const key = allowanceMarkerKey(context);
  return locks.request(key, { mode: "exclusive" }, async () => {
    valid(storage.getItem(key) === null);
    const c = codingCandidate(
        candidate,
        context,
        grant.recovery_request_id,
        grant.bind_port,
      ),
      b = parseInput(grant, c);
    const marker: CodingMarker = {
      version: 1,
      kind: "coding-check-recovery",
      context,
      candidate: c,
      grant: b,
      inputDigest: await attemptDigest({ context, candidate: c, grant: b }),
      allowance: null,
    };
    const raw = JSON.stringify(marker);
    storage.setItem(key, raw);
    valid(storage.getItem(key) === raw);
    await readCodingMarker(raw, context);
    announce();
    return marker;
  });
}
export async function retainCodingAllowance(
  storage: Storage,
  locks: AllowanceLocks | undefined,
  marker: CodingMarker,
  review: AllowanceReview,
  body: LinkedAllowance,
): Promise<CodingMarker> {
  valid(locks && typeof locks.request === "function");
  const key = allowanceMarkerKey(marker.context);
  return locks.request(key, { mode: "exclusive" }, async () => {
    const saved = await readCodingMarker(storage.getItem(key), marker.context);
    valid(saved && !saved.allowance && (await same(saved, marker)));
    valid(
      body.request_id === marker.grant.recovery_request_id &&
        body.coding_check_grant_id === marker.grant.request_id,
    );
    const updated = {
      ...saved,
      allowance: {
        review,
        body,
        inputDigest: await attemptDigest({ review, body }),
      },
    };
    const raw = JSON.stringify(updated);
    await readCodingMarker(raw, marker.context);
    storage.setItem(key, raw);
    valid(storage.getItem(key) === raw);
    announce();
    return updated;
  });
}
export async function codingReceipt(
  value: unknown,
  marker: CodingMarker,
): Promise<CodingReceipt> {
  bounded(value);
  const r = object(value);
  keys(r, [
    "version",
    "protocol",
    "request",
    "actor",
    "candidate",
    "revision",
    "call_limit",
    "expires_at_ms",
    "receipt_digest",
  ]);
  valid(
    r.version === 1 &&
      r.protocol === codingProtocol &&
      (await same(r.request, marker.grant)),
  );
  codingCandidate(
    r.candidate,
    marker.context,
    marker.grant.recovery_request_id,
    marker.grant.bind_port,
  );
  valid(await same(r.candidate, marker.candidate));
  const actor = object(r.actor);
  keys(actor, [
    "user_id",
    "authentication_method",
    "global_admin",
    "api_audience",
    "identity_digest",
  ]);
  valid(
    actor.user_id === marker.context.actor &&
      actor.authentication_method === marker.context.authentication &&
      actor.global_admin === true &&
      (actor.api_audience === null || typeof actor.api_audience === "string"),
  );
  match(actor.identity_digest, hash);
  valid(
    integer(r.revision, 1, Number.MAX_SAFE_INTEGER) ===
      marker.grant.expected_revision + 1 &&
      integer(r.call_limit, 1, 20) === marker.grant.call_limit,
  );
  return {
    grantId: marker.grant.request_id,
    revision: r.revision as number,
    receiptDigest: match(r.receipt_digest, hash),
    expires_at_ms: integer(r.expires_at_ms, 1, Number.MAX_SAFE_INTEGER),
    callLimit: r.call_limit as number,
  };
}
export function codingCurrent(
  value: unknown,
  context: AllowanceContext,
  receipt: CodingReceipt,
  now = Date.now(),
) {
  bounded(value);
  const c = object(value);
  keys(c, [
    "protocol",
    "run_id",
    "grant_id",
    "revision",
    "status",
    "receipt_digest",
    "expires_at_ms",
  ]);
  valid(
    c.protocol === codingProtocol &&
      c.run_id === context.run &&
      c.grant_id === receipt.grantId &&
      c.status === "active" &&
      c.revision === receipt.revision &&
      c.receipt_digest === receipt.receiptDigest &&
      c.expires_at_ms === receipt.expires_at_ms &&
      integer(c.expires_at_ms, 1, Number.MAX_SAFE_INTEGER) > now,
  );
  return receipt;
}
export function codingUnclaimed(value: unknown, context: AllowanceContext) {
  bounded(value);
  const current = object(value);
  keys(current, [
    "protocol",
    "run_id",
    "grant_id",
    "revision",
    "status",
    "receipt_digest",
    "expires_at_ms",
  ]);
  valid(
    current.protocol === codingProtocol &&
      current.run_id === context.run &&
      current.status === "none" &&
      current.revision === 0 &&
      current.grant_id === null &&
      current.receipt_digest === null &&
      current.expires_at_ms === null,
  );
}
export async function codingAllowanceReceipt(
  value: unknown,
  marker: CodingMarker,
) {
  valid(marker.allowance);
  bounded(value);
  const packet = object(value),
    request = object(packet.request);
  valid(
    (await same(request, marker.allowance.body)) &&
      request.coding_check_grant_id === marker.grant.request_id,
  );
  const legacy = { ...request };
  delete legacy.coding_check_grant_id;
  // Reuse every legacy native proof check after separately validating the linked grant.
  return allowanceReceipt(
    { ...packet, request: legacy },
    {
      context: marker.context,
      review: marker.allowance.review,
      body: legacy as AllowanceInput,
    },
  );
}
export function codingAllowanceView(value: unknown, context: AllowanceContext) {
  return allowanceCandidate(value, context);
}
