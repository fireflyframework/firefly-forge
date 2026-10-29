/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type RecoveryContext = {
  base: string;
  actor: string;
  project: string | null;
  repository: string;
  run: string;
};
export type ResultReference = {
  kind: "http_response" | "bridge_observation";
  record_digest: string;
  observation_id: string | null;
  terminal_digest: string | null;
};
export type Candidate = {
  completion_id: string;
  checkpoint_digest: string;
  intent_digest: string;
  task: "implement";
  attempt: number;
  ordinal: number | null;
  deadline_ms: number | null;
  invocation_protocol?: "forge-owned-checkpoint-v3";
  work_duration?: {
    protocol: "forge-work-duration-v2";
    timeout_seconds: number | null;
  };
  created_at: string;
  state: string;
  result: ResultReference | null;
  observation: { id: string; state: string } | null;
  ticket: { id: string; status: string } | null;
};
export type RecoveryMarker = {
  version: 1;
  context: RecoveryContext;
  completion: string;
  checkpoint: string;
  intent: string;
  kind: "observation" | "restore";
  request: string;
  source: ResultReference | null;
};
const id = /^[a-f0-9]{32}$/,
  completion = /^fbr1_[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const statuses = [
  "queued",
  "executing",
  "completed",
  "failed",
  "needs_reconciliation",
];
const observed = ["pending", "paused", "terminal", "result_unavailable"];
function requireValue(value: unknown): asserts value {
  if (!value)
    throw new Error("The original result identity could not be verified.");
}
function record(value: unknown): Record<string, unknown> {
  requireValue(value && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function keys(value: Record<string, unknown>, allowed: string[]) {
  requireValue(Object.keys(value).sort().join() === [...allowed].sort().join());
}
function reference(value: unknown): ResultReference {
  const r = record(value);
  keys(r, ["kind", "record_digest", "observation_id", "terminal_digest"]);
  requireValue(
    hash.test(String(r.record_digest)) &&
      ((r.kind === "http_response" &&
        r.observation_id === null &&
        r.terminal_digest === null) ||
        (r.kind === "bridge_observation" &&
          id.test(String(r.observation_id)) &&
          hash.test(String(r.terminal_digest)))),
  );
  return r as ResultReference;
}
export function candidatePage(
  value: unknown,
  context: RecoveryContext,
): { state: string; items: Candidate[] } {
  const p = record(value);
  keys(p, [
    "version",
    "actor_id",
    "run_id",
    "repository_id",
    "project_id",
    "state",
    "items",
  ]);
  requireValue(
    p.version === 1 &&
      p.actor_id === context.actor &&
      p.run_id === context.run &&
      p.repository_id === context.repository &&
      p.project_id === context.project &&
      ["available", "no_candidate", "evidence_unavailable"].includes(
        String(p.state),
      ) &&
      Array.isArray(p.items) &&
      p.items.length <= 16,
  );
  const items = p.items.map((value) => {
    const c = record(value);
    const owned = c.invocation_protocol === "forge-owned-checkpoint-v3";
    keys(c, [
      "completion_id",
      "checkpoint_digest",
      "intent_digest",
      "task",
      "attempt",
      "ordinal",
      "deadline_ms",
      "created_at",
      "state",
      "result",
      "observation",
      "ticket",
      ...(owned ? ["invocation_protocol", "work_duration"] : []),
    ]);
    if (owned) {
      const duration = record(c.work_duration);
      keys(duration, ["protocol", "timeout_seconds"]);
      requireValue(
        duration.protocol === "forge-work-duration-v2" &&
          (duration.timeout_seconds === null ||
            (typeof duration.timeout_seconds === "number" &&
              Number.isSafeInteger(duration.timeout_seconds) &&
              duration.timeout_seconds > 0)),
      );
      requireValue(
        (duration.timeout_seconds === null) === (c.deadline_ms === null),
      );
    }
    requireValue(
      completion.test(String(c.completion_id)) &&
        hash.test(String(c.checkpoint_digest)) &&
        hash.test(String(c.intent_digest)) &&
        c.task === "implement" &&
        Number.isSafeInteger(c.attempt) &&
        Number(c.attempt) >= 1 &&
        Number(c.attempt) <= 1000000 &&
        (c.ordinal === null ||
          (Number.isSafeInteger(c.ordinal) &&
            Number(c.ordinal) >= 1 &&
            Number(c.ordinal) <= 100)) &&
        (c.deadline_ms === null ||
          (Number.isSafeInteger(c.deadline_ms) && Number(c.deadline_ms) > 0)) &&
        typeof c.created_at === "string" &&
        c.created_at.length <= 40 &&
        Number.isFinite(Date.parse(c.created_at)) &&
        [
          "ready",
          "awaiting_result",
          "held",
          "legacy",
          "expired",
          "consumed",
          "ticket_recorded",
        ].includes(String(c.state)),
    );
    if (c.result !== null) reference(c.result);
    if (c.observation !== null) {
      const o = record(c.observation);
      keys(o, ["id", "state"]);
      requireValue(id.test(String(o.id)) && observed.includes(String(o.state)));
    }
    if (c.ticket !== null) {
      const t = record(c.ticket);
      keys(t, ["id", "status"]);
      requireValue(
        id.test(String(t.id)) && statuses.includes(String(t.status)),
      );
    }
    requireValue(
      (c.state !== "ready" || c.result !== null) &&
        (c.state !== "ticket_recorded" || c.ticket !== null) &&
        (c.ticket === null || c.state === "ticket_recorded") &&
        (c.state === "legacy" ||
          (c.ordinal !== null && (c.deadline_ms !== null || owned))),
    );
    return c as Candidate;
  });
  requireValue(
    new Set(items.map((c) => c.completion_id)).size === items.length &&
      (p.state === "available" || items.length === 0),
  );
  return { state: String(p.state), items };
}
export function markerKey(c: RecoveryContext) {
  return (
    "forge.agent-checkpoint.v1:" +
    JSON.stringify([c.base, c.actor, c.project, c.repository, c.run])
  );
}
export function readMarker(
  raw: string | null,
  context: RecoveryContext,
): RecoveryMarker | null {
  if (raw === null) return null;
  const m = record(JSON.parse(raw));
  keys(m, [
    "version",
    "context",
    "completion",
    "checkpoint",
    "intent",
    "kind",
    "request",
    "source",
  ]);
  const c = record(m.context);
  keys(c, ["base", "actor", "project", "repository", "run"]);
  requireValue(
    Object.entries(context).every(([k, v]) => c[k] === v) &&
      [c.actor, c.repository, c.run].every(
        (v) => typeof v === "string" && id.test(v),
      ) &&
      (c.project === null ||
        (typeof c.project === "string" && id.test(c.project))) &&
      typeof c.base === "string" &&
      c.base.length <= 4096 &&
      m.version === 1 &&
      completion.test(String(m.completion)) &&
      hash.test(String(m.checkpoint)) &&
      hash.test(String(m.intent)) &&
      id.test(String(m.request)) &&
      ["observation", "restore"].includes(String(m.kind)) &&
      (m.kind !== "observation" || m.source === null),
  );
  if (m.kind === "restore") reference(m.source);
  return m as RecoveryMarker;
}
type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;
export async function sendOnce<T>(
  storage: Storage,
  marker: RecoveryMarker,
  send: () => Promise<T>,
  confirmedPrevious?: { marker: RecoveryMarker; state: string },
): Promise<T> {
  const key = markerKey(marker.context),
    previous = readMarker(storage.getItem(key), marker.context);
  requireValue(
    previous === null ||
      (confirmedPrevious &&
        JSON.stringify(previous) === JSON.stringify(confirmedPrevious.marker) &&
        previous.request !== marker.request &&
        ((previous.kind === "observation" &&
          observed.includes(confirmedPrevious.state) &&
          previous.completion === marker.completion &&
          previous.intent === marker.intent &&
          previous.checkpoint === marker.checkpoint) ||
          (previous.kind === "restore" &&
            confirmedPrevious.state === "completed" &&
            previous.completion !== marker.completion))),
  );
  const raw = JSON.stringify(marker);
  readMarker(raw, marker.context);
  storage.setItem(key, raw);
  requireValue(storage.getItem(key) === raw);
  return await send();
}
export function observationReceipt(value: unknown, m: RecoveryMarker): string {
  const r = record(value),
    p = record(r.payload),
    i = record(p.input),
    s = record(p.snapshot);
  requireValue(
    m.kind === "observation" &&
      r.kind === "bridge_request_observation" &&
      r.run_id === m.context.run &&
      r.task_id === "implement" &&
      r.idempotency_key === m.request &&
      hash.test(String(r.digest)) &&
      p.request_id === m.completion &&
      p.intent_digest === m.intent &&
      p.observed_by === m.context.actor &&
      i.version === 1 &&
      i.observation_id === m.request &&
      i.expected_intent_digest === m.intent &&
      observed.includes(String(s.state)),
  );
  return String(s.state);
}
export function restoreBody(m: RecoveryMarker) {
  requireValue(m.kind === "restore");
  return {
    request_id: m.request,
    expected_checkpoint_digest: m.checkpoint,
    expected_intent_digest: m.intent,
    source: m.source,
  };
}
export function restoreReceipt(value: unknown, m: RecoveryMarker): string {
  const r = record(value),
    request = record(r.request),
    actor = record(request.actor),
    input = record(request.input);
  requireValue(
    m.kind === "restore" &&
      r.id === m.request &&
      r.run_id === m.context.run &&
      r.kind === "agent_restore" &&
      statuses.includes(String(r.status)) &&
      request.run_id === m.context.run &&
      request.completion_request_id === m.completion &&
      actor.user_id === m.context.actor &&
      Object.entries(restoreBody(m)).every(
        ([key, v]) => JSON.stringify(input[key]) === JSON.stringify(v),
      ),
  );
  keys(input, [
    "request_id",
    "expected_checkpoint_digest",
    "expected_intent_digest",
    "source",
  ]);
  return String(r.status);
}
export function discoveredReceipt(
  value: unknown,
  c: Candidate,
  context: RecoveryContext,
): string {
  const r = record(value),
    q = record(r.request),
    input = record(q.input);
  requireValue(
    c.ticket &&
      r.id === c.ticket.id &&
      r.run_id === context.run &&
      r.kind === "agent_restore" &&
      statuses.includes(String(r.status)) &&
      q.run_id === context.run &&
      q.completion_request_id === c.completion_id &&
      input.request_id === c.ticket.id &&
      input.expected_checkpoint_digest === c.checkpoint_digest &&
      input.expected_intent_digest === c.intent_digest,
  );
  reference(input.source);
  return String(r.status);
}
