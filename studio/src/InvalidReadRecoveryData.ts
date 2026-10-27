/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type RecoveryContext = {
  base: string;
  actor: string;
  authentication: string;
  project: string | null;
  repository: string;
  run: string;
  request: string;
};
export type Candidate = {
  version: 1;
  kind: string;
  run_id: string;
  request_id: string;
  digest: string;
  admission: "READY" | "HOLD";
  holds: string[];
  execution_authorized: false;
  original_admission_seal: string;
  failed_event_digest: string;
  original_settlement_digest: string;
  original_claim_digest: string;
  checkpoint_digest: string;
  source_digest: string;
  attempt: number;
  current_profile: Record<string, string> | null;
  current_runtime_contract: string;
  policy_digest: string;
  maximum_window_seconds: number;
  frozen_timeout_seconds: number;
  original_lease: Record<string, unknown>;
  usage: {
    requests: number;
    request_limit: number | null;
    tool_calls: number;
    tool_calls_limit: number | null;
    commands: number;
    command_limit: number | null;
  };
};
export type RecoveryInput = {
  version: 1;
  candidate_digest: string;
  expected_admission_seal: string;
  expected_failed_event_digest: string;
  valid_for_seconds: number;
  acknowledge_never_claimed: true;
  acknowledge_same_request_and_attempt: true;
  acknowledge_preserved_consumption_and_budgets: true;
  acknowledge_renewed_same_attempt_lease: true;
};
export type RecoveryMarker = {
  version: 1;
  context: RecoveryContext;
  recovery: string;
  body: RecoveryInput;
};
export type RecoveryStatus = {
  version: 1;
  kind: string;
  run_id: string;
  request_id: string;
  recovery_id: string | null;
  recovery_record_digest: string | null;
  status: string;
  original_admission_seal: string;
  lease: Record<string, unknown> | null;
  current_profile: Record<string, string> | null;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const holds = [
  "current_profile_unqualified",
  "lease_policy_disabled",
  "unavailable_observation_policy_disabled",
  "active_execution_budget_expired",
];
function requireValue(value: unknown): asserts value {
  if (!value)
    throw Error("The saved continuation identity could not be verified.");
}
function record(value: unknown): Record<string, unknown> {
  requireValue(value && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function keys(value: Record<string, unknown>, allowed: string[]) {
  requireValue(Object.keys(value).sort().join() === [...allowed].sort().join());
}
function integer(value: unknown, min: number, max = Number.MAX_SAFE_INTEGER) {
  return (
    Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max
  );
}
function profile(value: unknown) {
  if (value === null) return;
  requireValue(
    Object.values(record(value)).every((v) => typeof v === "string"),
  );
}
function bound(value: Record<string, unknown>, context: RecoveryContext) {
  requireValue(
    value.version === 1 &&
      id.test(context.run) &&
      id.test(context.request) &&
      value.run_id === context.run &&
      value.request_id === context.request,
  );
}
export function candidate(value: unknown, context: RecoveryContext): Candidate {
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
    "failed_event_digest",
    "original_settlement_digest",
    "original_claim_digest",
    "checkpoint_digest",
    "source_digest",
    "attempt",
    "current_profile",
    "current_runtime_contract",
    "policy_digest",
    "maximum_window_seconds",
    "frozen_timeout_seconds",
    "original_lease",
    "usage",
  ]);
  bound(c, context);
  requireValue(
    c.kind === "unclaimed_invalid_read_recovery_candidate" &&
      c.execution_authorized === false &&
      ["READY", "HOLD"].includes(String(c.admission)) &&
      Array.isArray(c.holds) &&
      c.holds.every((h) => holds.includes(String(h))) &&
      new Set(c.holds).size === c.holds.length &&
      integer(c.attempt, 1) &&
      integer(c.frozen_timeout_seconds, 1, 7200) &&
      integer(c.maximum_window_seconds, 0, Number(c.frozen_timeout_seconds)),
  );
  for (const key of [
    "digest",
    "original_admission_seal",
    "failed_event_digest",
    "original_settlement_digest",
    "original_claim_digest",
    "checkpoint_digest",
    "source_digest",
    "current_runtime_contract",
    "policy_digest",
  ])
    requireValue(hash.test(String(c[key])));
  profile(c.current_profile);
  record(c.original_lease);
  const u = record(c.usage);
  keys(u, [
    "requests",
    "request_limit",
    "tool_calls",
    "tool_calls_limit",
    "commands",
    "command_limit",
  ]);
  for (const key of ["requests", "tool_calls", "commands"])
    requireValue(integer(u[key], 0));
  for (const key of ["request_limit", "tool_calls_limit", "command_limit"])
    requireValue(
      u[key] === null || integer(u[key], key === "command_limit" ? 1 : 0),
    );
  requireValue(
    c.admission !== "READY" ||
      (c.holds.length === 0 &&
        Number(c.maximum_window_seconds) > 0 &&
        c.current_profile !== null),
  );
  return c as Candidate;
}
function input(value: unknown): RecoveryInput {
  const b = record(value);
  keys(b, [
    "version",
    "candidate_digest",
    "expected_admission_seal",
    "expected_failed_event_digest",
    "valid_for_seconds",
    "acknowledge_never_claimed",
    "acknowledge_same_request_and_attempt",
    "acknowledge_preserved_consumption_and_budgets",
    "acknowledge_renewed_same_attempt_lease",
  ]);
  requireValue(b.version === 1 && integer(b.valid_for_seconds, 1, 7200));
  for (const key of [
    "candidate_digest",
    "expected_admission_seal",
    "expected_failed_event_digest",
  ])
    requireValue(hash.test(String(b[key])));
  for (const key of [
    "acknowledge_never_claimed",
    "acknowledge_same_request_and_attempt",
    "acknowledge_preserved_consumption_and_budgets",
    "acknowledge_renewed_same_attempt_lease",
  ])
    requireValue(b[key] === true);
  return b as RecoveryInput;
}
export function recoveryInput(c: Candidate): RecoveryInput {
  requireValue(
    c.admission === "READY" &&
      c.holds.length === 0 &&
      c.maximum_window_seconds > 0,
  );
  return input({
    version: 1,
    candidate_digest: c.digest,
    expected_admission_seal: c.original_admission_seal,
    expected_failed_event_digest: c.failed_event_digest,
    valid_for_seconds: c.maximum_window_seconds,
    acknowledge_never_claimed: true,
    acknowledge_same_request_and_attempt: true,
    acknowledge_preserved_consumption_and_budgets: true,
    acknowledge_renewed_same_attempt_lease: true,
  });
}
export function recoveryStatus(
  value: unknown,
  context: RecoveryContext,
  recovery?: string,
  seal?: string,
): RecoveryStatus {
  const s = record(value);
  keys(s, [
    "version",
    "kind",
    "run_id",
    "request_id",
    "recovery_id",
    "recovery_record_digest",
    "status",
    "original_admission_seal",
    "lease",
    "current_profile",
  ]);
  bound(s, context);
  requireValue(
    s.kind === "unclaimed_invalid_read_recovery_status" &&
      [
        "not_recovered",
        "queued",
        "executing",
        "completed",
        "failed",
        "needs_reconciliation",
      ].includes(String(s.status)) &&
      hash.test(String(s.original_admission_seal)) &&
      (!seal || s.original_admission_seal === seal),
  );
  profile(s.current_profile);
  if (s.status === "not_recovered") {
    requireValue(
      s.recovery_id === null &&
        s.recovery_record_digest === null &&
        s.lease === null &&
        !recovery,
    );
  } else {
    requireValue(
      id.test(String(s.recovery_id)) &&
        hash.test(String(s.recovery_record_digest)) &&
        (!recovery || s.recovery_id === recovery),
    );
    record(s.lease);
  }
  return s as RecoveryStatus;
}
// The shared key also prevents a different actor from reusing this browser's uncertain action.
export function markerKey(c: RecoveryContext) {
  return (
    "forge.invalid-read-recovery.v1:" +
    JSON.stringify([c.base, c.run, c.request])
  );
}
export function readMarker(
  raw: string | null,
  context: RecoveryContext,
): RecoveryMarker | null {
  if (raw === null) return null;
  const m = record(JSON.parse(raw));
  keys(m, ["version", "context", "recovery", "body"]);
  const c = record(m.context);
  keys(c, [
    "base",
    "actor",
    "authentication",
    "project",
    "repository",
    "run",
    "request",
  ]);
  requireValue(
    m.version === 1 &&
      id.test(String(m.recovery)) &&
      Object.entries(context).every(([key, value]) => c[key] === value) &&
      id.test(context.actor) &&
      id.test(context.repository) &&
      (context.project === null || id.test(context.project)),
  );
  input(m.body);
  return m as RecoveryMarker;
}
export async function sendOnce<T>(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: RecoveryMarker,
  send: () => Promise<T>,
) {
  const key = markerKey(marker.context);
  requireValue(storage.getItem(key) === null);
  const raw = JSON.stringify(marker);
  readMarker(raw, marker.context);
  storage.setItem(key, raw);
  requireValue(storage.getItem(key) === raw);
  return await send();
}
