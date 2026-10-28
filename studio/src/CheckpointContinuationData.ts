/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { RecoveryContext } from "./InvalidReadRecoveryData";
export type CheckpointCandidate = {
  version: 1;
  kind: "checkpoint_continuation_candidate";
  run_id: string;
  request_id: string;
  digest: string;
  admission: "READY" | "HOLD";
  holds: string[];
  execution_authorized: false;
  original_admission_seal: string;
  attempt: number;
  maximum_window_seconds: number;
  frozen_timeout_seconds: number;
  original_deadline_ms: number;
  current_profile: Record<string, string> | null;
  usage: Record<string, unknown> & { requests: number; tool_calls: number };
  limits: Record<string, unknown> & {
    request_limit: number | null;
    tool_calls_limit: number | null;
  };
  ticket_digest: string;
  events_digest: string;
  journal_digest: string;
  run_digest: string;
  task_digest: string;
  checkpoint_record_digest: string;
  response_record_digest: string;
  source_digest: string;
  current_runtime_contract: string;
  policy_digest: string;
};
export type CheckpointInput = {
  version: 1;
  candidate_digest: string;
  valid_for_seconds: number;
  acknowledge_retained_response_consumed_once: true;
  acknowledge_no_completed_effect_replay: true;
  acknowledge_same_request_attempt_and_budgets: true;
  acknowledge_finite_ownership_lease: true;
};
export type CheckpointStatus = {
  version: 1;
  kind: "checkpoint_continuation_status";
  run_id: string;
  request_id: string;
  status:
    | "not_recovered"
    | "queued"
    | "executing"
    | "completed"
    | "failed"
    | "needs_reconciliation";
  action_id: string | null;
  witness_digest: string | null;
  original_admission_seal: string;
  lease: Record<string, unknown> | null;
};
export type CheckpointMarker = {
  version: 1;
  context: RecoveryContext;
  action: string;
  seal: string;
  body: CheckpointInput;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const holds = [
  "lease_policy_disabled",
  "checkpoint_continuation_disabled",
  "current_profile_unqualified",
  "active_execution_budget_expired",
];
function requireValue(value: unknown): asserts value {
  if (!value)
    throw Error("The saved continuation record could not be verified.");
}
function record(value: unknown): Record<string, unknown> {
  requireValue(value && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function keys(value: Record<string, unknown>, names: string[]) {
  requireValue(Object.keys(value).sort().join() === [...names].sort().join());
}
const integer = (
  value: unknown,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): value is number =>
  typeof value === "number" &&
  Number.isSafeInteger(value) &&
  value >= min &&
  value <= max;
const digest = (value: unknown): value is string =>
  typeof value === "string" && hash.test(value);
const identifier = (value: unknown): value is string =>
  typeof value === "string" && id.test(value);
function bound(value: Record<string, unknown>, context: RecoveryContext) {
  requireValue(
    value.version === 1 &&
      identifier(context.run) &&
      identifier(context.request) &&
      value.run_id === context.run &&
      value.request_id === context.request,
  );
}
function cost(value: unknown) {
  requireValue(
    value === null ||
      (typeof value === "string" &&
        /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim()) &&
        Number.isFinite(Number(value)) &&
        Number(value) >= 0),
  );
}
export function checkpointCandidate(
  value: unknown,
  context: RecoveryContext,
  seal?: string,
): CheckpointCandidate {
  const c = record(value);
  keys(c, [
    "version",
    "kind",
    "run_id",
    "request_id",
    "digest",
    "admission",
    "holds",
    "execution_authorized",
    "original_admission_seal",
    "attempt",
    "ticket_digest",
    "events_digest",
    "journal_digest",
    "run_digest",
    "task_digest",
    "checkpoint_record_digest",
    "response_record_digest",
    "source_digest",
    "current_runtime_contract",
    "current_profile",
    "policy_digest",
    "frozen_timeout_seconds",
    "maximum_window_seconds",
    "original_deadline_ms",
    "usage",
    "limits",
  ]);
  bound(c, context);
  requireValue(
    c.kind === "checkpoint_continuation_candidate" &&
      c.execution_authorized === false &&
      (c.admission === "READY" || c.admission === "HOLD") &&
      Array.isArray(c.holds) &&
      c.holds.every((h) => typeof h === "string" && holds.includes(h)) &&
      new Set(c.holds).size === c.holds.length &&
      integer(c.attempt, 1) &&
      integer(c.frozen_timeout_seconds, 1, 7200) &&
      integer(c.maximum_window_seconds, 1, Number(c.frozen_timeout_seconds)) &&
      integer(c.original_deadline_ms, 1),
  );
  for (const name of [
    "digest",
    "original_admission_seal",
    "ticket_digest",
    "events_digest",
    "journal_digest",
    "run_digest",
    "task_digest",
    "checkpoint_record_digest",
    "response_record_digest",
    "source_digest",
    "current_runtime_contract",
    "policy_digest",
  ])
    requireValue(digest(c[name]));
  requireValue(!seal || c.original_admission_seal === seal);
  if (c.current_profile !== null)
    requireValue(
      Object.keys(record(c.current_profile)).length > 0 &&
        Object.values(record(c.current_profile)).every(
          (v) => typeof v === "string",
        ),
    );
  requireValue(
    c.admission !== "READY" ||
      (c.holds.length === 0 && c.current_profile !== null),
  );
  const usage = record(c.usage),
    limits = record(c.limits);
  const counters = [
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "input_audio_tokens",
    "cache_audio_read_tokens",
    "output_audio_tokens",
    "requests",
    "tool_calls",
  ];
  const required = [...counters, "audio_seconds", "details", "cost"];
  requireValue(
    required.every((k) => k in usage) &&
      Object.keys(usage).every((k) =>
        [
          ...required,
          "output_reasoning_tokens",
          "cache_write_1h_tokens",
        ].includes(k),
      ),
  );
  for (const [key, v] of Object.entries(usage)) {
    if (key === "cost") cost(v);
    else if (key === "details")
      requireValue(Object.values(record(v)).every((n) => integer(n)));
    else if (key === "audio_seconds")
      requireValue(typeof v === "number" && Number.isFinite(v) && v >= 0);
    else requireValue(integer(v));
  }
  keys(limits, [
    "cost_limit",
    "request_limit",
    "tool_calls_limit",
    "input_tokens_limit",
    "output_tokens_limit",
    "total_tokens_limit",
    "per_request_input_tokens_limit",
    "count_tokens_before_request",
  ]);
  for (const [key, v] of Object.entries(limits)) {
    if (key === "cost_limit") cost(v);
    else if (key === "count_tokens_before_request")
      requireValue(typeof v === "boolean");
    else requireValue(v === null || integer(v));
  }
  return c as CheckpointCandidate;
}
export function checkpointInput(
  c: CheckpointCandidate,
  seconds: number,
): CheckpointInput {
  requireValue(
    c.admission === "READY" &&
      c.holds.length === 0 &&
      integer(seconds, 1, c.maximum_window_seconds),
  );
  return {
    version: 1,
    candidate_digest: c.digest,
    valid_for_seconds: seconds,
    acknowledge_retained_response_consumed_once: true,
    acknowledge_no_completed_effect_replay: true,
    acknowledge_same_request_attempt_and_budgets: true,
    acknowledge_finite_ownership_lease: true,
  };
}
function input(value: unknown): CheckpointInput {
  const b = record(value);
  keys(b, [
    "version",
    "candidate_digest",
    "valid_for_seconds",
    "acknowledge_retained_response_consumed_once",
    "acknowledge_no_completed_effect_replay",
    "acknowledge_same_request_attempt_and_budgets",
    "acknowledge_finite_ownership_lease",
  ]);
  requireValue(
    b.version === 1 &&
      digest(b.candidate_digest) &&
      integer(b.valid_for_seconds, 1, 7200),
  );
  for (const key of [
    "acknowledge_retained_response_consumed_once",
    "acknowledge_no_completed_effect_replay",
    "acknowledge_same_request_attempt_and_budgets",
    "acknowledge_finite_ownership_lease",
  ])
    requireValue(b[key] === true);
  return b as CheckpointInput;
}
export function checkpointStatus(
  value: unknown,
  context: RecoveryContext,
  action?: string,
  seal?: string,
): CheckpointStatus {
  const s = record(value);
  keys(s, [
    "version",
    "kind",
    "run_id",
    "request_id",
    "status",
    "action_id",
    "witness_digest",
    "original_admission_seal",
    "lease",
  ]);
  bound(s, context);
  requireValue(
    s.kind === "checkpoint_continuation_status" &&
      typeof s.status === "string" &&
      [
        "not_recovered",
        "queued",
        "executing",
        "completed",
        "failed",
        "needs_reconciliation",
      ].includes(s.status) &&
      digest(s.original_admission_seal) &&
      (!seal || s.original_admission_seal === seal),
  );
  if (s.status === "not_recovered")
    requireValue(
      s.action_id === null &&
        s.witness_digest === null &&
        s.lease === null &&
        !action,
    );
  else {
    requireValue(
      identifier(s.action_id) &&
        digest(s.witness_digest) &&
        (!action || s.action_id === action),
    );
    record(s.lease);
  }
  return s as CheckpointStatus;
}
export function checkpointMarkerKey(c: RecoveryContext) {
  return (
    "forge.checkpoint-continuation.v1:" +
    JSON.stringify([c.base, c.run, c.request])
  );
}
export function readCheckpointMarker(
  raw: string | null,
  context: RecoveryContext,
): CheckpointMarker | null {
  if (raw === null) return null;
  const m = record(JSON.parse(raw));
  keys(m, ["version", "context", "action", "seal", "body"]);
  const c = record(m.context);
  keys(c, Object.keys(context));
  requireValue(
    m.version === 1 &&
      identifier(m.action) &&
      digest(m.seal) &&
      Object.entries(context).every(([k, v]) => c[k] === v) &&
      identifier(context.actor) &&
      identifier(context.repository) &&
      (context.project === null || identifier(context.project)),
  );
  input(m.body);
  return m as CheckpointMarker;
}
export async function sendCheckpointOnce<T>(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: CheckpointMarker,
  send: () => Promise<T>,
): Promise<T> {
  const key = checkpointMarkerKey(marker.context);
  requireValue(storage.getItem(key) === null);
  const raw = JSON.stringify(marker);
  readCheckpointMarker(raw, marker.context);
  storage.setItem(key, raw);
  requireValue(storage.getItem(key) === raw);
  return await send();
}
