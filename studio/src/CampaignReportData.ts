/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export const campaignStates = [
  "verified",
  "blocked",
  "unknown",
  "in_progress",
  "not_started",
] as const;
export type CampaignState = (typeof campaignStates)[number];
export type CampaignReference = { sequence: number; digest: string };
export type CampaignTask = {
  id: string;
  attempt: number;
  state: "receipted" | "in_progress" | "not_started" | "unknown" | "blocked";
  evidence_digest: string | null;
  receipt: CampaignReference | null;
};
export type CampaignCheck = {
  command_digest: string;
  argument_count: number;
  exit_code: number;
  receipt: CampaignReference;
};
export type CampaignComponent = {
  component_id: string;
  repository_id: string;
  run_id: string;
  attempt: number;
  state: CampaignState;
  recorded_status: string;
  reason: string | null;
  verification: "verified" | "not_verified";
  admission_digest: string;
  run_digest: string;
  source_base: string;
  branch: string | null;
  branch_receipt: CampaignReference | null;
  checkpoint_revision: string | null;
  source_tree: string | null;
  source_tree_receipt: CampaignReference | null;
  checkpoint_receipt: CampaignReference | null;
  plan_digest: string | null;
  journal: CampaignReference & { head: string };
  tasks: CampaignTask[];
  checks: CampaignCheck[];
  prior_attempts: {
    attempt: number;
    run_id: string;
    admission_digest: string;
  }[];
  depends_on: string[];
};
export type CampaignReport = {
  version: 1;
  project_id: string;
  campaign: {
    id: string;
    recorded_status: string | null;
    project_digest: string | null;
    request_digest: string | null;
    journal_head: string | null;
    event_count: number | null;
  };
  state: CampaignState;
  reason: string | null;
  counts: Partial<Record<CampaignState, number>> | null;
  components: CampaignComponent[];
  milestones: "not_configured";
  waves: "not_configured";
  scm_freshness: "not_observed";
  limits: Record<string, number>;
  digest: string;
};
export type CampaignSummary = {
  id: string;
  status: string;
  created_at: string | null;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/,
  revision = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/,
  slug = /^[a-z][a-z0-9-]{0,63}$/;
function requireValue(condition: unknown): asserts condition {
  if (!condition)
    throw new Error(
      "The campaign response cannot be matched to the selected product and campaign.",
    );
}
function record(value: unknown): Record<string, unknown> {
  requireValue(
    Boolean(value) && typeof value === "object" && !Array.isArray(value),
  );
  return value as Record<string, unknown>;
}
function exactKeys(value: Record<string, unknown>, keys: string[]) {
  requireValue(Object.keys(value).sort().join() === keys.sort().join());
}
function string(value: unknown, max = 2000) {
  requireValue(
    typeof value === "string" &&
      value.length <= max &&
      ![...value].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127),
  );
  return value;
}
function matching(value: unknown, pattern: RegExp) {
  const v = string(value);
  requireValue(pattern.test(v));
  return v;
}
function integer(value: unknown, min: number, max: number) {
  requireValue(
    typeof value === "number" &&
      Number.isSafeInteger(value) &&
      value >= min &&
      value <= max,
  );
  return value;
}
function nullable<T>(value: unknown, parse: (v: unknown) => T): T | null {
  return value === null ? null : parse(value);
}
function array<T>(value: unknown, max: number, parse: (v: unknown) => T): T[] {
  requireValue(Array.isArray(value) && value.length <= max);
  return value.map(parse);
}
function state(value: unknown): CampaignState {
  requireValue(campaignStates.includes(value as CampaignState));
  return value as CampaignState;
}
function reference(value: unknown): CampaignReference {
  const r = record(value);
  exactKeys(r, ["sequence", "digest"]);
  return {
    sequence: integer(r.sequence, 0, 2048),
    digest: matching(r.digest, hash),
  };
}
function task(value: unknown): CampaignTask {
  const r = record(value);
  exactKeys(r, ["id", "attempt", "state", "evidence_digest", "receipt"]);
  requireValue(
    ["receipted", "in_progress", "not_started", "unknown", "blocked"].includes(
      String(r.state),
    ),
  );
  const result = {
    id: string(r.id, 2000),
    attempt: integer(r.attempt, 1, 1_000_000),
    state: r.state as CampaignTask["state"],
    evidence_digest: nullable(r.evidence_digest, (v) => matching(v, hash)),
    receipt: nullable(r.receipt, reference),
  };
  requireValue(
    result.state !== "receipted" ||
      (result.evidence_digest !== null && result.receipt !== null),
  );
  return result;
}
function component(value: unknown): CampaignComponent {
  const r = record(value);
  exactKeys(r, [
    "component_id",
    "repository_id",
    "run_id",
    "attempt",
    "state",
    "recorded_status",
    "reason",
    "verification",
    "admission_digest",
    "run_digest",
    "source_base",
    "branch",
    "branch_receipt",
    "checkpoint_revision",
    "source_tree",
    "source_tree_receipt",
    "checkpoint_receipt",
    "plan_digest",
    "journal",
    "tasks",
    "checks",
    "prior_attempts",
    "depends_on",
  ]);
  const journal = record(r.journal);
  exactKeys(journal, ["sequence", "digest", "head"]);
  const result: CampaignComponent = {
    component_id: matching(r.component_id, slug),
    repository_id: matching(r.repository_id, id),
    run_id: matching(r.run_id, id),
    attempt: integer(r.attempt, 1, 1_000_000),
    state: state(r.state),
    recorded_status: matching(r.recorded_status, /^[a-z][a-z0-9_]{0,63}$/),
    reason: nullable(r.reason, string),
    verification: r.verification as CampaignComponent["verification"],
    admission_digest: matching(r.admission_digest, hash),
    run_digest: matching(r.run_digest, hash),
    source_base: matching(r.source_base, revision),
    branch: nullable(r.branch, (v) => string(v, 1024)),
    branch_receipt: nullable(r.branch_receipt, reference),
    checkpoint_revision: nullable(r.checkpoint_revision, (v) =>
      matching(v, revision),
    ),
    source_tree: nullable(r.source_tree, (v) => matching(v, revision)),
    source_tree_receipt: nullable(r.source_tree_receipt, reference),
    checkpoint_receipt: nullable(r.checkpoint_receipt, reference),
    plan_digest: nullable(r.plan_digest, (v) => matching(v, hash)),
    journal: {
      sequence: integer(journal.sequence, 0, 2048),
      digest: matching(journal.digest, hash),
      head: matching(journal.head, hash),
    },
    tasks: array(r.tasks, 128, task),
    checks: array(r.checks, 2048, (value) => {
      const c = record(value);
      exactKeys(c, [
        "command_digest",
        "argument_count",
        "exit_code",
        "receipt",
      ]);
      return {
        command_digest: matching(c.command_digest, hash),
        argument_count: integer(c.argument_count, 0, 63),
        exit_code: integer(c.exit_code, -2_147_483_648, 2_147_483_647),
        receipt: reference(c.receipt),
      };
    }),
    prior_attempts: array(r.prior_attempts, 200, (value) => {
      const a = record(value);
      exactKeys(a, ["attempt", "run_id", "admission_digest"]);
      return {
        attempt: integer(a.attempt, 1, 1_000_000),
        run_id: matching(a.run_id, id),
        admission_digest: matching(a.admission_digest, hash),
      };
    }),
    depends_on: array(r.depends_on, 50, (v) => matching(v, slug)),
  };
  requireValue(
    ["verified", "not_verified"].includes(result.verification) &&
      result.journal.digest === result.journal.head,
  );
  requireValue(
    result.state !== "verified" || result.verification === "verified",
  );
  requireValue(
    new Set(result.tasks.map((t) => t.id)).size === result.tasks.length &&
      new Set(result.prior_attempts.map((a) => a.attempt)).size ===
        result.prior_attempts.length,
  );
  requireValue(result.prior_attempts.every((a) => a.attempt < result.attempt));
  requireValue(
    [
      result.branch_receipt,
      result.checkpoint_receipt,
      result.source_tree_receipt,
      ...result.tasks.map((t) => t.receipt),
      ...result.checks.map((c) => c.receipt),
    ].every((ref) => ref === null || ref.sequence <= result.journal.sequence),
  );
  return result;
}
export function readCampaignList(
  value: unknown,
  project: string,
): CampaignSummary[] {
  matching(project, id);
  const items = array(value, 200, (value) => {
    const c = record(value);
    requireValue(c.project_id === project);
    return {
      id: matching(c.id, id),
      status: matching(c.status, /^[a-z][a-z0-9_]{0,63}$/),
      created_at:
        c.created_at === undefined || c.created_at === null
          ? null
          : string(c.created_at, 100),
    };
  });
  requireValue(new Set(items.map((c) => c.id)).size === items.length);
  requireValue(
    items.every(
      (c) => c.created_at === null || Number.isFinite(Date.parse(c.created_at)),
    ),
  );
  return items;
}
export function readCampaignReport(
  value: unknown,
  context: { project: string; campaign: string },
): CampaignReport {
  const r = record(value);
  exactKeys(r, [
    "version",
    "project_id",
    "campaign",
    "state",
    "reason",
    "counts",
    "components",
    "milestones",
    "waves",
    "scm_freshness",
    "limits",
    "digest",
  ]);
  requireValue(
    r.version === 1 &&
      r.project_id === context.project &&
      id.test(context.project) &&
      id.test(context.campaign),
  );
  const header = record(r.campaign);
  exactKeys(header, [
    "id",
    "recorded_status",
    "project_digest",
    "request_digest",
    "journal_head",
    "event_count",
  ]);
  requireValue(header.id === context.campaign);
  requireValue(
    r.milestones === "not_configured" &&
      r.waves === "not_configured" &&
      r.scm_freshness === "not_observed",
  );
  const limits = record(r.limits),
    bounds = {
      attempts: 200,
      campaign_events: 1024,
      tasks_per_run: 128,
      journal_entries_per_run: 2048,
      run_events_per_run: 2048,
      snapshot_seconds: 10,
      evidence_json_bytes: 16_777_216,
    };
  exactKeys(limits, Object.keys(bounds));
  const parsedLimits = Object.fromEntries(
    Object.entries(bounds).map(([key, max]) => [
      key,
      integer(limits[key], 1, max),
    ]),
  );
  const components = array(r.components, 200, component);
  requireValue(
    new Set(components.map((c) => c.component_id)).size === components.length,
  );
  let counts: CampaignReport["counts"] = null;
  if (r.counts !== null) {
    const source = record(r.counts);
    requireValue(
      Object.keys(source).every((key) =>
        campaignStates.includes(key as CampaignState),
      ),
    );
    counts = Object.fromEntries(
      Object.entries(source).map(([key, value]) => [
        key,
        integer(value, 0, 200),
      ]),
    );
    requireValue(
      components.length > 0 &&
        campaignStates.every(
          (key) =>
            (counts![key] ?? 0) ===
            components.filter((c) => c.state === key).length,
        ),
    );
  } else requireValue(r.state === "unknown" && components.length === 0);
  return {
    version: 1,
    project_id: context.project,
    campaign: {
      id: context.campaign,
      recorded_status: nullable(header.recorded_status, (v) =>
        matching(v, /^[a-z][a-z0-9_]{0,63}$/),
      ),
      project_digest: nullable(header.project_digest, (v) => matching(v, hash)),
      request_digest: nullable(header.request_digest, (v) => matching(v, hash)),
      journal_head: nullable(header.journal_head, (v) => matching(v, hash)),
      event_count: nullable(header.event_count, (v) => integer(v, 0, 1024)),
    },
    state: state(r.state),
    reason: nullable(r.reason, string),
    counts,
    components,
    milestones: "not_configured",
    waves: "not_configured",
    scm_freshness: "not_observed",
    limits: parsedLimits,
    digest: matching(r.digest, hash),
  };
}
