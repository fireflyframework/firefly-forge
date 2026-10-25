/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type ExecutionPolicy = {
  enabled: boolean;
  dispatch_window: number;
  max_parallel_runs: number;
};
export type ExecutionPolicyView = {
  project_id: string;
  policy: ExecutionPolicy;
  digest: string | null;
  effective_digest: string;
  request_id: string | null;
  permissions: { configure: boolean };
};
export type ExecutionCampaign = {
  id: string;
  status: string;
  control: string | null;
  publish: boolean;
  head: string;
  held: number;
  budget: {
    granted_dispatches: number;
    consumed_dispatches: number;
    remaining_dispatches: number;
    max_parallel_runs: number;
  } | null;
};
const invalid = () =>
  new Error("The campaign execution response could not be verified.");
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw invalid();
  return value as Record<string, unknown>;
};
const integer = (value: unknown, zero = false): number => {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < (zero ? 0 : 1)
  )
    throw invalid();
  return value;
};
const hex = (value: unknown, size: number): string => {
  if (
    typeof value !== "string" ||
    !new RegExp(`^[a-f0-9]{${size}}$`).test(value)
  )
    throw invalid();
  return value;
};
export function positiveInteger(value: string): number {
  if (!/^[1-9][0-9]*$/.test(value))
    throw new Error("Use a positive whole number.");
  return integer(Number(value));
}
export function executionPolicy(
  value: unknown,
  projectId: string,
): ExecutionPolicyView {
  const data = object(value),
    policy = object(data.policy),
    permissions = object(data.permissions);
  if (
    data.version !== 1 ||
    data.project_id !== projectId ||
    typeof policy.enabled !== "boolean" ||
    typeof permissions.configure !== "boolean"
  )
    throw invalid();
  const digest = data.digest === null ? null : hex(data.digest, 64),
    effective = hex(data.effective_digest, 64);
  if (digest !== null && digest !== effective) throw invalid();
  const requestId = data.request_id === null ? null : hex(data.request_id, 32);
  if ((digest === null) !== (requestId === null)) throw invalid();
  return {
    project_id: projectId,
    policy: {
      enabled: policy.enabled,
      dispatch_window: integer(policy.dispatch_window),
      max_parallel_runs: integer(policy.max_parallel_runs),
    },
    digest,
    effective_digest: effective,
    request_id: requestId,
    permissions: { configure: permissions.configure },
  };
}
export function executionCampaign(
  value: unknown,
  projectId: string,
  campaignId?: string,
): ExecutionCampaign {
  const data = object(value),
    journal = object(data.journal),
    launch = object(data.launch);
  const id = hex(data.id, 32);
  if (
    data.project_id !== projectId ||
    (campaignId && id !== campaignId) ||
    journal.verified !== true ||
    typeof data.status !== "string" ||
    typeof launch.publish !== "boolean" ||
    !Array.isArray(data.children)
  )
    throw invalid();
  const children = data.children.map(object);
  if (
    children.some(
      (child) =>
        typeof child.current !== "boolean" || typeof child.status !== "string",
    )
  )
    throw invalid();
  const held = children.filter(
    (child) =>
      child.current &&
      !["campaign_blocked", "succeeded", "failed", "cancelled"].includes(
        child.status as string,
      ),
  ).length;
  let budget = null;
  if (data.execution_budget_state !== undefined) {
    const raw = object(data.execution_budget_state);
    if (raw.version !== 1) throw invalid();
    budget = {
      granted_dispatches: integer(raw.granted_dispatches),
      consumed_dispatches: integer(raw.consumed_dispatches, true),
      remaining_dispatches: integer(raw.remaining_dispatches, true),
      max_parallel_runs: integer(raw.max_parallel_runs),
    };
    if (
      budget.granted_dispatches - budget.consumed_dispatches !==
      budget.remaining_dispatches
    )
      throw invalid();
  }
  if (
    data.control !== null &&
    data.control !== undefined &&
    typeof data.control !== "string"
  )
    throw invalid();
  return {
    id,
    status: data.status,
    control: (data.control as string | null) ?? null,
    publish: launch.publish,
    head: hex(journal.head, 64),
    held,
    budget,
  };
}
export function executionCampaigns(
  value: unknown,
  projectId: string,
): ExecutionCampaign[] {
  if (!Array.isArray(value)) throw invalid();
  const items = value.map((item) => executionCampaign(item, projectId));
  if (new Set(items.map((item) => item.id)).size !== items.length)
    throw invalid();
  return items;
}
export function renewalBody(
  campaign: ExecutionCampaign,
  view: ExecutionPolicyView,
  requestId: string,
  amount: string,
  parallel: string,
) {
  const additional = positiveInteger(amount),
    slots = positiveInteger(parallel);
  if (
    !campaign.budget ||
    campaign.control === "cancelled" ||
    campaign.status === "needs_reconciliation" ||
    !view.policy.enabled ||
    additional > view.policy.dispatch_window ||
    slots > view.policy.max_parallel_runs
  )
    throw new Error(
      "Review the current campaign and enabled project execution window before renewing.",
    );
  return {
    request_id: hex(requestId, 32),
    expected_journal_head: campaign.head,
    expected_policy_digest: view.effective_digest,
    additional_dispatches: additional,
    max_parallel_runs: slots,
  };
}
