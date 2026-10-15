/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type ContinuationContext = {
  base: string;
  actor: string;
  run: string;
  repository: string;
  project: string | null;
};
export type ContinuationCommand = {
  execution_id: string;
  task_id: "implement" | "verify";
  attempt: number;
  command_label: string;
  argument_count: number;
  source_digest: string;
  invocation_digest: string;
  wal_digest: string;
  binding_digest: string;
};
export type ContinuationMarker = ContinuationContext & {
  version: 1;
  execution: string;
  request: string;
  task: "implement" | "verify";
  attempt: number;
  sourceDigest: string;
  invocationDigest: string;
  walDigest: string;
  bindingDigest: string;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Continuation identity could not be verified.");
  return value as Record<string, unknown>;
}
function requireValue(condition: unknown): asserts condition {
  if (!condition)
    throw new Error("Continuation identity could not be verified.");
}
const labels = [
  "Python",
  "Git",
  "npm",
  "Node",
  "Make",
  "pytest",
  "uv",
  "Java",
  "Maven",
  "Project",
].map((value) => value + " command");
const states = [
  "requires_leader_confirmation",
  "source_unavailable",
  "leader_unavailable",
  "source_changed",
  "request_already_recorded",
  "admission_unavailable",
  "not_applicable",
  "no_candidate",
  "evidence_unavailable",
  "evidence_changed",
  "read_budget_exceeded",
];
export function candidateRead(
  value: unknown,
  context: ContinuationContext,
): { state: string; commands: ContinuationCommand[] } {
  const data = record(value);
  requireValue(
    data.version === 1 &&
      data.actor_id === context.actor &&
      data.run_id === context.run &&
      data.repository_id === context.repository &&
      data.project_id === context.project &&
      states.includes(String(data.state)) &&
      Array.isArray(data.commands) &&
      data.commands.length <= 16,
  );
  const commands = data.commands.map((item) => {
    const c = record(item);
    requireValue(
      Object.keys(c).sort().join() ===
        [
          "execution_id",
          "task_id",
          "attempt",
          "command_label",
          "argument_count",
          "source_digest",
          "invocation_digest",
          "wal_digest",
          "binding_digest",
        ]
          .sort()
          .join() &&
        id.test(String(c.execution_id)) &&
        ["implement", "verify"].includes(String(c.task_id)) &&
        Number.isInteger(c.attempt) &&
        Number(c.attempt) > 0 &&
        Number.isInteger(c.argument_count) &&
        Number(c.argument_count) >= 0 &&
        Number(c.argument_count) <= 63 &&
        labels.includes(String(c.command_label)) &&
        [
          c.source_digest,
          c.invocation_digest,
          c.wal_digest,
          c.binding_digest,
        ].every((v) => typeof v === "string" && hash.test(v)),
    );
    return c as ContinuationCommand;
  });
  requireValue(
    data.state === "requires_leader_confirmation" || commands.length === 0,
  );
  requireValue(
    new Set(commands.map((c) => c.execution_id)).size === commands.length,
  );
  return { state: String(data.state), commands };
}
export function continuationKey(c: ContinuationContext) {
  return (
    "forge.runner-continuation.v1:" +
    JSON.stringify([c.base, c.actor, c.run, c.repository, c.project])
  );
}
export function readContinuationMarker(
  value: string | null,
  c: ContinuationContext,
): ContinuationMarker | null {
  if (value === null) return null;
  const m = record(JSON.parse(value));
  requireValue(
    Object.keys(m).sort().join() ===
      [
        "version",
        "base",
        "actor",
        "run",
        "repository",
        "project",
        "execution",
        "request",
        "task",
        "attempt",
        "sourceDigest",
        "invocationDigest",
        "walDigest",
        "bindingDigest",
      ]
        .sort()
        .join() &&
      m.version === 1 &&
      Object.entries(c).every(([key, v]) => m[key] === v) &&
      [m.actor, m.run, m.repository, m.execution, m.request].every(
        (v) => typeof v === "string" && id.test(v),
      ) &&
      (m.project === null ||
        (typeof m.project === "string" && id.test(m.project))) &&
      ["implement", "verify"].includes(String(m.task)) &&
      Number.isInteger(m.attempt) &&
      Number(m.attempt) > 0 &&
      [m.sourceDigest, m.invocationDigest, m.walDigest, m.bindingDigest].every(
        (v) => typeof v === "string" && hash.test(v),
      ),
  );
  return m as ContinuationMarker;
}
type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;
export function retainContinuation(
  storage: Storage,
  c: ContinuationContext,
  command: ContinuationCommand,
  request: string,
): ContinuationMarker {
  const key = continuationKey(c);
  requireValue(storage.getItem(key) === null);
  const m: ContinuationMarker = {
    ...c,
    version: 1,
    execution: command.execution_id,
    request,
    task: command.task_id,
    attempt: command.attempt,
    sourceDigest: command.source_digest,
    invocationDigest: command.invocation_digest,
    walDigest: command.wal_digest,
    bindingDigest: command.binding_digest,
  };
  const value = JSON.stringify(m);
  readContinuationMarker(value, c);
  storage.setItem(key, value);
  requireValue(storage.getItem(key) === value);
  return m;
}
export function continuationReceipt(
  value: unknown,
  m: ContinuationMarker,
): { status: string; accepted: boolean } {
  const r = record(value),
    input = record(r.request),
    actor = record(input.actor),
    run = record(input.run_binding);
  requireValue(
    r.id === m.request &&
      r.run_id === m.run &&
      r.kind === "runner_continue" &&
      r.request_digest === m.bindingDigest &&
      input.run_id === m.run &&
      input.execution_id === m.execution &&
      input.task_id === m.task &&
      input.attempt === m.attempt &&
      input.wal_digest === m.walDigest &&
      input.invocation_digest === m.invocationDigest &&
      actor.user_id === m.actor &&
      run.repository_id === m.repository &&
      run.project_id === m.project &&
      [
        "queued",
        "executing",
        "completed",
        "failed",
        "needs_reconciliation",
      ].includes(String(r.status)),
  );
  return {
    status: String(r.status),
    accepted:
      r.status === "completed" && record(r.result).disposition === "accepted",
  };
}
