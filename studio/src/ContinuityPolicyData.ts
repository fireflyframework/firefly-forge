/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type ContinuityPolicy = {
  version: 1;
  enabled: boolean;
  allow_unavailable_source_observations: boolean;
  allow_pre_admission_recovery: boolean;
  allow_readonly_observation_recovery: boolean;
  allow_checkpoint_continuation: boolean;
  max_window_seconds: number;
  max_tickets_per_run: number;
  max_pending_tickets: number;
};
export type ContinuityPolicyView = {
  version: 1;
  revision: string | null;
  settings: ContinuityPolicy;
  digest: string;
};
export type PolicyContext = {
  base: string;
  actor: string;
  authentication: string;
};
export type PendingPolicy = {
  version: 1;
  context: PolicyContext;
  settings: ContinuityPolicy;
};
function requireValue(v: unknown): asserts v {
  if (!v) throw Error("Saved recovery settings could not be verified.");
}
function record(v: unknown): Record<string, unknown> {
  requireValue(v && typeof v === "object" && !Array.isArray(v));
  return v as Record<string, unknown>;
}
function keys(v: Record<string, unknown>, names: string[]) {
  requireValue(Object.keys(v).sort().join() === [...names].sort().join());
}
const integer = (v: unknown, max = Number.MAX_SAFE_INTEGER) =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 1 && v <= max;
export function continuityPolicy(value: unknown): ContinuityPolicy {
  const p = record(value);
  const flags = [
    "enabled",
    "allow_unavailable_source_observations",
    "allow_pre_admission_recovery",
    "allow_readonly_observation_recovery",
    "allow_checkpoint_continuation",
  ];
  keys(p, [
    "version",
    ...flags,
    "max_window_seconds",
    "max_tickets_per_run",
    "max_pending_tickets",
  ]);
  requireValue(
    p.version === 1 &&
      flags.every((k) => typeof p[k] === "boolean") &&
      integer(p.max_window_seconds, 7200) &&
      integer(p.max_tickets_per_run) &&
      integer(p.max_pending_tickets),
  );
  return p as ContinuityPolicy;
}
export function continuityPolicyView(value: unknown): ContinuityPolicyView {
  const p = record(value);
  keys(p, ["version", "revision", "settings", "digest"]);
  requireValue(
    p.version === 1 &&
      (p.revision === null ||
        (typeof p.revision === "string" &&
          /^[a-f0-9]{32}$/.test(p.revision))) &&
      typeof p.digest === "string" &&
      /^[a-f0-9]{64}$/.test(p.digest),
  );
  continuityPolicy(p.settings);
  return p as ContinuityPolicyView;
}
export function sameContinuityPolicy(a: ContinuityPolicy, b: ContinuityPolicy) {
  return Object.entries(a).every(
    ([k, v]) => b[k as keyof ContinuityPolicy] === v,
  );
}
export function pendingPolicyKey(c: PolicyContext) {
  return "forge.continuity-policy.v1:" + c.base;
}
export function readPendingPolicy(
  raw: string | null,
  context: PolicyContext,
): PendingPolicy | null {
  if (raw === null) return null;
  const p = record(JSON.parse(raw));
  keys(p, ["version", "context", "settings"]);
  const c = record(p.context);
  keys(c, ["base", "actor", "authentication"]);
  requireValue(
    p.version === 1 &&
      /^[a-f0-9]{32}$/.test(context.actor) &&
      Object.entries(context).every(([k, v]) => c[k] === v),
  );
  continuityPolicy(p.settings);
  return p as PendingPolicy;
}
