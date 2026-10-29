/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { RecoveryContext } from "./InvalidReadRecoveryData";
export type FinalCandidate = Record<string, unknown> & {
  version: 1;
  digest: string;
  original_admission_seal: string;
  admission: "READY" | "HOLD";
  holds: string[];
  attempt: number;
  maximum_window_seconds: number;
  outcome: "complete" | "blocked" | "clarification" | "failed";
  followup_kind: string | null;
  usage: { requests: number; tool_calls: number };
  limits: { request_limit: number | null; tool_calls_limit: number | null };
};
export type FinalInput = {
  version: 1;
  candidate_digest: string;
  valid_for_seconds: number;
  acknowledge_already_consumed_final_output: true;
  acknowledge_no_completed_effect_replay: true;
  acknowledge_same_request_attempt_and_budgets: true;
  acknowledge_finite_ownership_lease: true;
};
export type FinalStatus = {
  version: 1;
  kind: "final_outcome_continuation_status";
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
  run_status: string;
};
export type FinalMarker = {
  version: 1;
  context: RecoveryContext;
  action: string;
  seal: string;
  body: FinalInput;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
function requireValue(value: unknown): asserts value {
  if (!value) throw Error("The saved final result could not be verified.");
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
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
): value is number =>
  typeof value === "number" &&
  Number.isSafeInteger(value) &&
  value >= minimum &&
  value <= maximum;
const identifier = (value: unknown): value is string =>
  typeof value === "string" && id.test(value);
const digest = (value: unknown): value is string =>
  typeof value === "string" && hash.test(value);
function bound(value: Record<string, unknown>, context: RecoveryContext) {
  requireValue(
    value.version === 1 &&
      identifier(context.run) &&
      identifier(context.request) &&
      value.run_id === context.run &&
      value.request_id === context.request,
  );
}
const digestKeys = [
  "ticket_digest",
  "events_digest",
  "journal_digest",
  "run_digest",
  "task_digest",
  "checkpoint_record_digest",
  "response_record_digest",
  "consume_record_digest",
  "report_digest",
  "final_outcome_digest",
  "source_digest",
  "current_runtime_contract",
  "policy_digest",
  "digest",
];
export function finalCandidate(
  value: unknown,
  context: RecoveryContext,
): FinalCandidate {
  const c = record(value);
  keys(c, [
    "version",
    "kind",
    "run_id",
    "request_id",
    "admission",
    "holds",
    "execution_authorized",
    "original_admission_seal",
    "attempt",
    ...digestKeys,
    "current_profile",
    "maximum_window_seconds",
    "admission_window_purpose",
    "outcome",
    "followup_kind",
    "usage",
    "limits",
  ]);
  bound(c, context);
  requireValue(
    c.kind === "final_outcome_continuation_candidate" &&
      c.execution_authorized === false &&
      ["READY", "HOLD"].includes(String(c.admission)) &&
      digest(c.original_admission_seal),
  );
  requireValue(
    digestKeys.every((key) => digest(c[key])) &&
      integer(c.attempt, 1) &&
      integer(c.maximum_window_seconds, 1, 7200) &&
      c.admission_window_purpose === "claim_only",
  );
  requireValue(
    Array.isArray(c.holds) &&
      c.holds.every(
        (hold) =>
          typeof hold === "string" && /^[a-z][a-z0-9_]{0,100}$/.test(hold),
      ) &&
      new Set(c.holds).size === c.holds.length &&
      (c.admission !== "READY" || c.holds.length === 0),
  );
  requireValue(
    ["complete", "blocked", "clarification", "failed"].includes(
      String(c.outcome),
    ) &&
      (c.followup_kind === null || typeof c.followup_kind === "string"),
  );
  if (c.current_profile !== null) record(c.current_profile);
  requireValue(c.admission !== "READY" || c.current_profile !== null);
  const usage = record(c.usage),
    limits = record(c.limits);
  requireValue(integer(usage.requests) && integer(usage.tool_calls));
  for (const key of ["request_limit", "tool_calls_limit"])
    requireValue(limits[key] === null || integer(limits[key]));
  return c as FinalCandidate;
}
function input(value: unknown): FinalInput {
  const b = record(value);
  keys(b, [
    "version",
    "candidate_digest",
    "valid_for_seconds",
    "acknowledge_already_consumed_final_output",
    "acknowledge_no_completed_effect_replay",
    "acknowledge_same_request_attempt_and_budgets",
    "acknowledge_finite_ownership_lease",
  ]);
  requireValue(
    b.version === 1 &&
      digest(b.candidate_digest) &&
      integer(b.valid_for_seconds, 1, 7200),
  );
  for (const key of Object.keys(b).filter((key) =>
    key.startsWith("acknowledge_"),
  ))
    requireValue(b[key] === true);
  return b as FinalInput;
}
export function finalInput(c: FinalCandidate): FinalInput {
  requireValue(
    c.admission === "READY" &&
      c.holds.length === 0 &&
      integer(c.maximum_window_seconds, 1, 7200),
  );
  return input({
    version: 1,
    candidate_digest: c.digest,
    valid_for_seconds: c.maximum_window_seconds,
    acknowledge_already_consumed_final_output: true,
    acknowledge_no_completed_effect_replay: true,
    acknowledge_same_request_attempt_and_budgets: true,
    acknowledge_finite_ownership_lease: true,
  });
}
export function finalStatus(
  value: unknown,
  context: RecoveryContext,
  action?: string,
  seal?: string,
): FinalStatus {
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
    "run_status",
  ]);
  bound(s, context);
  requireValue(
    s.kind === "final_outcome_continuation_status" &&
      [
        "not_recovered",
        "queued",
        "executing",
        "completed",
        "failed",
        "needs_reconciliation",
      ].includes(String(s.status)) &&
      digest(s.original_admission_seal) &&
      (!seal || s.original_admission_seal === seal) &&
      typeof s.run_status === "string",
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
  return s as FinalStatus;
}
export function finalMarkerKey(context: RecoveryContext) {
  return (
    "forge.final-outcome.v1:" +
    JSON.stringify([context.base, context.run, context.request])
  );
}
export function readFinalMarker(
  raw: string | null,
  context: RecoveryContext,
): FinalMarker | null {
  if (raw === null) return null;
  const m = record(JSON.parse(raw)),
    c = record(m.context);
  keys(m, ["version", "context", "action", "seal", "body"]);
  keys(c, Object.keys(context));
  requireValue(
    m.version === 1 &&
      identifier(m.action) &&
      digest(m.seal) &&
      identifier(context.actor) &&
      identifier(context.repository) &&
      (context.project === null || identifier(context.project)) &&
      Object.entries(context).every(([key, value]) => c[key] === value),
  );
  input(m.body);
  return m as FinalMarker;
}
export async function sendFinalOnce<T>(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: FinalMarker,
  send: () => Promise<T>,
): Promise<T> {
  const key = finalMarkerKey(marker.context),
    raw = JSON.stringify(marker);
  requireValue(storage.getItem(key) === null);
  readFinalMarker(raw, marker.context);
  storage.setItem(key, raw);
  requireValue(storage.getItem(key) === raw);
  return await send();
}
