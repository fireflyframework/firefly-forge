/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { request, type Connection, type ForgeRun } from "./api";
import type { RecoveryContext } from "./InvalidReadRecoveryData";

export type ReconciliationContext = RecoveryContext & {
  recovery: string;
  claim: string;
  attempt: number;
};
const digestNames = [
  "owner_digest",
  "actor_digest",
  "policy_digest",
  "runtime_contract",
  "profile_digest",
  "run_digest",
  "task_digest",
  "journal_digest",
  "recovery_digest",
  "claim_digest",
  "source_digest",
  "index_digest",
  "digest",
] as const;
export type ReconciliationCandidate = Record<
  (typeof digestNames)[number],
  string
> & {
  version: 1;
  run_id: string;
  request_id: string;
  same_task_attempt: number;
};
export type ReconciliationInput = {
  version: 1;
  candidate_digest: string;
  acknowledge_same_claimed_action: true;
  acknowledge_no_execution_grant: true;
  acknowledge_local_postprocessing: true;
};
const proofDigestNames = [
  "terminal_report_sha256",
  "final_response_sha256",
  "recorded_runtime_contract",
  "current_runtime_contract",
  "current_profile_digest",
  "source_digest",
  "parent_final_digest",
  "handoff_custody_digest",
  "usage_before_digest",
  "usage_after_digest",
  "digest",
] as const;
type ConsumedTerminalCandidate = {
  version: 2;
  run_id: string;
  actor_digest: string;
  policy_digest: string;
  run_digest: string;
  journal_digest: string;
  digest: string;
  proof: Record<(typeof proofDigestNames)[number], string> & {
    version: 2;
    run_id: string;
    task_id: "implement";
    task_attempt: number;
    task_key: string;
    completion_count: number;
    completions: Array<{
      request_id: string;
      intent_digest: string;
      checkpoint_record_digest: string;
      response_record_digest: string;
      consume_record_digest: string;
    }>;
    verification: "consumed_terminal_output_known_only";
  };
};
type ConsumedTerminalInput = {
  version: 2;
  candidate_digest: string;
  policy_digest: string;
  expected_run_digest: string;
  journal_digest: string;
  proof_digest: string;
  acknowledge_known_worker_output: true;
  acknowledge_no_execution_grant: true;
  acknowledge_local_postprocessing: true;
};
export type ReconciliationReceipt = {
  version: 1;
  candidate: ReconciliationCandidate;
  request: ReconciliationInput;
  record_digest: string;
  settlement: {
    version: 2;
    kind: "consumed_terminal_recovery";
    run_id: string;
    request_id: string;
    record_digest: string;
    candidate: ConsumedTerminalCandidate;
    request: ConsumedTerminalInput;
    state: "queued" | "claimed" | "accepted";
    run_status: string;
    same_task_attempt: number;
    no_execution_grant: true;
  };
};
export type ReconciliationMarker = {
  version: 1;
  context: ReconciliationContext;
  candidate_digest: string;
  actor_digest: string;
};
const identifier = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{32}$/.test(v);
const hash = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
function requireValue(v: unknown): asserts v {
  if (!v) throw Error("The original claimed action could not be verified.");
}
function record(v: unknown): Record<string, unknown> {
  requireValue(v && typeof v === "object" && !Array.isArray(v));
  return v as Record<string, unknown>;
}
function exact(v: Record<string, unknown>, names: readonly string[]) {
  requireValue(Object.keys(v).sort().join() === [...names].sort().join());
}
export function hasClaimedConsumedTerminal(
  run: Pick<ForgeRun, "status" | "consumed_terminal_recovery">,
): boolean {
  const value = run.consumed_terminal_recovery;
  return (
    ["paused", "needs_reconciliation"].includes(run.status) &&
    !!value &&
    typeof value === "object" &&
    (value as Record<string, unknown>).state === "claimed"
  );
}
export function consumedTerminalRequest(
  run: Pick<ForgeRun, "status" | "consumed_terminal_recovery">,
): string | null {
  const value = run.consumed_terminal_recovery as
    Record<string, unknown> | undefined;
  return hasClaimedConsumedTerminal(run) && identifier(value?.request_id)
    ? value.request_id
    : null;
}
export function parseReconciliationCandidate(
  value: unknown,
  context: ReconciliationContext,
): ReconciliationCandidate {
  const c = record(value);
  exact(c, [
    "version",
    "run_id",
    "request_id",
    "same_task_attempt",
    ...digestNames,
  ]);
  requireValue(
    c.version === 1 &&
      identifier(context.run) &&
      identifier(context.request) &&
      c.run_id === context.run &&
      c.request_id === context.request,
  );
  requireValue(
    digestNames.every((key) => hash(c[key])) &&
      Number.isSafeInteger(c.same_task_attempt) &&
      Number(c.same_task_attempt) >= 1 &&
      c.same_task_attempt === context.attempt &&
      c.recovery_digest === context.recovery &&
      c.claim_digest === context.claim,
  );
  return c as ReconciliationCandidate;
}
export function reconciliationInput(
  candidate: ReconciliationCandidate,
): ReconciliationInput {
  return {
    version: 1,
    candidate_digest: candidate.digest,
    acknowledge_same_claimed_action: true,
    acknowledge_no_execution_grant: true,
    acknowledge_local_postprocessing: true,
  };
}
export function parseReconciliationReceipt(
  value: unknown,
  context: ReconciliationContext,
  marker?: ReconciliationMarker | null,
): ReconciliationReceipt {
  const r = record(value);
  exact(r, ["version", "candidate", "request", "record_digest", "settlement"]);
  const c = parseReconciliationCandidate(r.candidate, context),
    body = record(r.request),
    settlement = record(r.settlement);
  exact(body, [
    "version",
    "candidate_digest",
    "acknowledge_same_claimed_action",
    "acknowledge_no_execution_grant",
    "acknowledge_local_postprocessing",
  ]);
  requireValue(
    r.version === 1 &&
      hash(r.record_digest) &&
      body.version === 1 &&
      body.candidate_digest === c.digest &&
      body.acknowledge_same_claimed_action === true &&
      body.acknowledge_no_execution_grant === true &&
      body.acknowledge_local_postprocessing === true,
  );
  requireValue(
    !marker ||
      (marker.candidate_digest === c.digest &&
        marker.actor_digest === c.actor_digest),
  );
  exact(settlement, [
    "version",
    "kind",
    "run_id",
    "request_id",
    "record_digest",
    "candidate",
    "request",
    "state",
    "run_status",
    "same_task_attempt",
    "no_execution_grant",
  ]);
  requireValue(
    settlement.version === 2 &&
      settlement.kind === "consumed_terminal_recovery" &&
      settlement.run_id === context.run &&
      settlement.request_id === context.request &&
      settlement.record_digest === c.recovery_digest &&
      settlement.same_task_attempt === c.same_task_attempt &&
      settlement.no_execution_grant === true &&
      ["queued", "claimed", "accepted"].includes(String(settlement.state)) &&
      typeof settlement.run_status === "string",
  );
  const original = record(settlement.candidate),
    proof = record(original.proof),
    originalInput = record(settlement.request);
  exact(original, [
    "version",
    "run_id",
    "actor_digest",
    "policy_digest",
    "run_digest",
    "journal_digest",
    "proof",
    "digest",
  ]);
  exact(proof, [
    "version",
    "run_id",
    "task_id",
    "task_attempt",
    "task_key",
    "completion_count",
    "completions",
    "verification",
    ...proofDigestNames,
  ]);
  requireValue(
    original.version === 2 &&
      original.run_id === context.run &&
      original.actor_digest === c.actor_digest &&
      [
        "actor_digest",
        "policy_digest",
        "run_digest",
        "journal_digest",
        "digest",
      ].every((key) => hash(original[key])),
  );
  requireValue(
    proof.version === 2 &&
      proof.run_id === context.run &&
      proof.task_id === "implement" &&
      proof.task_attempt === c.same_task_attempt &&
      identifier(proof.task_key) &&
      proof.verification === "consumed_terminal_output_known_only" &&
      proofDigestNames.every((key) => hash(proof[key])) &&
      proof.source_digest === c.source_digest,
  );
  requireValue(
    Array.isArray(proof.completions) &&
      Number.isSafeInteger(proof.completion_count) &&
      Number(proof.completion_count) >= 1 &&
      proof.completions.length === proof.completion_count,
  );
  const completions = proof.completions.map((value) => {
    const item = record(value);
    exact(item, [
      "request_id",
      "intent_digest",
      "checkpoint_record_digest",
      "response_record_digest",
      "consume_record_digest",
    ]);
    requireValue(
      typeof item.request_id === "string" &&
        /^fbr1_[a-f0-9]{32}$/.test(item.request_id) &&
        [
          "intent_digest",
          "checkpoint_record_digest",
          "response_record_digest",
          "consume_record_digest",
        ].every((key) => hash(item[key])),
    );
    return item.request_id;
  });
  requireValue(new Set(completions).size === completions.length);
  exact(originalInput, [
    "version",
    "candidate_digest",
    "policy_digest",
    "expected_run_digest",
    "journal_digest",
    "proof_digest",
    "acknowledge_known_worker_output",
    "acknowledge_no_execution_grant",
    "acknowledge_local_postprocessing",
  ]);
  requireValue(
    originalInput.version === 2 &&
      originalInput.candidate_digest === original.digest &&
      originalInput.policy_digest === original.policy_digest &&
      originalInput.expected_run_digest === original.run_digest &&
      originalInput.journal_digest === original.journal_digest &&
      originalInput.proof_digest === proof.digest &&
      originalInput.acknowledge_known_worker_output === true &&
      originalInput.acknowledge_no_execution_grant === true &&
      originalInput.acknowledge_local_postprocessing === true,
  );
  return r as ReconciliationReceipt;
}
export function reconciliationKey(context: ReconciliationContext): string {
  return "forge.consumed-terminal-reconciliation.v1:" + JSON.stringify(context);
}
export function readReconciliationMarker(
  raw: string | null,
  context: ReconciliationContext,
): ReconciliationMarker | null {
  if (raw === null) return null;
  const m = record(JSON.parse(raw));
  exact(m, ["version", "context", "candidate_digest", "actor_digest"]);
  requireValue(
    m.version === 1 &&
      hash(m.candidate_digest) &&
      hash(m.actor_digest) &&
      JSON.stringify(m.context) === JSON.stringify(context),
  );
  return m as ReconciliationMarker;
}
export async function sendReconciliationOnce<T>(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: ReconciliationMarker,
  send: () => Promise<T>,
): Promise<T> {
  const key = reconciliationKey(marker.context);
  requireValue(storage.getItem(key) === null);
  const raw = JSON.stringify(marker);
  storage.setItem(key, raw);
  requireValue(storage.getItem(key) === raw);
  return send();
}
const root = (context: ReconciliationContext) => {
  requireValue(identifier(context.run) && identifier(context.request));
  return `/runs/${context.run}/worker-completion-settlements/${context.request}`;
};
export async function getConsumedTerminalReconciliationCandidate(
  connection: Connection,
  context: ReconciliationContext,
  signal?: AbortSignal,
): Promise<ReconciliationCandidate> {
  return parseReconciliationCandidate(
    await request(connection, root(context) + "/reconciliation-candidate", {
      signal,
    }),
    context,
  );
}
export async function getConsumedTerminalReconciliationStatus(
  connection: Connection,
  context: ReconciliationContext,
  marker?: ReconciliationMarker | null,
  signal?: AbortSignal,
): Promise<ReconciliationReceipt> {
  return parseReconciliationReceipt(
    await request(connection, root(context) + "/reconciliation", { signal }),
    context,
    marker,
  );
}
export async function reconcileConsumedTerminal(
  connection: Connection,
  context: ReconciliationContext,
  candidate: ReconciliationCandidate,
  signal?: AbortSignal,
): Promise<ReconciliationReceipt> {
  return parseReconciliationReceipt(
    await request(connection, root(context) + "/reconcile", {
      method: "POST",
      body: JSON.stringify(reconciliationInput(candidate)),
      signal,
    }),
    context,
    {
      version: 1,
      context,
      candidate_digest: candidate.digest,
      actor_digest: candidate.actor_digest,
    },
  );
}
