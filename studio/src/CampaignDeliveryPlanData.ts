/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";
import type { CampaignArchiveReceipt } from "./CampaignReportArchiveData";
export type DeliveryScope = { project: string; campaign: string };
export type DeliveryPhase =
  "prepare" | "implement" | "verify" | "checkpoint" | "review";
export type DeliveryCondition = {
  kind: "component_verified" | "task_receipted";
  component_id: string;
  task_id: DeliveryPhase | null;
};
export type DeliveryWave = {
  id: string;
  name: string;
  component_ids: string[];
};
export type DeliveryMilestone = {
  id: string;
  name: string;
  wave_id: string;
  conditions: DeliveryCondition[];
  feature_ids: string[];
  journey_refs: string[];
  acceptance_refs: string[];
};
export type DeliveryInput = {
  request_id: string;
  expected_previous_digest: string | null;
  expected_campaign_request_digest: string;
  expected_project_digest: string;
  title: string;
  waves: DeliveryWave[];
  milestones: DeliveryMilestone[];
};
export type DeliverySource = {
  campaign_journal_head: string;
  campaign_event_count: number;
  project_digest: string;
  request_digest: string;
  report_digest: string;
  product_definitions: {
    repository_id: string;
    revision: number;
    digest: string;
  }[];
};
type Identity = {
  version: 1;
  criteria_binding: "reporting_only_post_admission";
  enforced_at_admission: false;
  project_id: string;
  campaign_id: string;
  request_id: string;
  actor_id: string;
  revision: number;
  previous_digest: string | null;
  prepared_at: string;
  repositories: string[];
  source: DeliverySource;
  request_digest: string;
  digest: string;
};
export type DeliveryReceipt = Identity & { request: DeliveryInput };
export type DeliverySummary = Identity & {
  title: string;
  receipt_digest: string;
};
export const deliveryStates = [
  "satisfied",
  "unknown",
  "blocked",
  "in_progress",
  "not_started",
  "dependency_blocked",
] as const;
export type DeliveryState = (typeof deliveryStates)[number];
export type DeliveryConditionProgress = DeliveryCondition & {
  state: DeliveryState;
  reason: string | null;
  run_id: string | null;
  component_attempt: number | null;
  task_attempt: number | null;
  evidence_digest: string | null;
  receipt: { sequence: number; digest: string } | null;
};
export type DeliveryProgress = {
  version: 1;
  criteria_binding: "reporting_only_post_admission";
  enforced_at_admission: false;
  project_id: string;
  campaign_id: string;
  plan_request_id: string;
  plan_revision: number;
  plan_digest: string;
  report_request_id: string;
  report_archive_digest: string;
  report_digest: string;
  plan_prepared_at: string;
  report_prepared_at: string;
  source: Omit<DeliverySource, "report_digest" | "product_definitions">;
  scm_freshness: "not_observed";
  state: DeliveryState;
  counts: {
    conditions: Record<string, number>;
    milestones: Record<string, number>;
    waves: Record<string, number>;
  };
  milestones: (Omit<DeliveryMilestone, "conditions"> & {
    state: DeliveryState;
    counts: Record<string, number>;
    conditions: DeliveryConditionProgress[];
  })[];
  waves: (DeliveryWave & {
    ordinal: number;
    component_states: Record<string, string>;
    milestone_ids: string[];
    observed_state: DeliveryState;
    state: DeliveryState;
    blocking_wave_ids: string[];
  })[];
  digest: string;
};
type Row = Record<string, unknown>;
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/,
  slug = /^[a-z][a-z0-9-]{0,63}$/;
const phases = ["prepare", "implement", "verify", "checkpoint", "review"];
function requireValue(v: unknown): asserts v {
  if (!v)
    throw new Error(
      "The reporting plan cannot be bound to this campaign and selected evidence.",
    );
}
function row(v: unknown): Row {
  requireValue(v && typeof v === "object" && !Array.isArray(v));
  return v as Row;
}
function keys(v: Row, expected: string[]) {
  requireValue(Object.keys(v).sort().join() === [...expected].sort().join());
}
function text(v: unknown, max = 200): string {
  requireValue(
    typeof v === "string" && [...v].length > 0 && [...v].length <= max,
  );
  return v;
}
function matching(v: unknown, regex: RegExp) {
  const s = text(v);
  requireValue(regex.test(s));
  return s;
}
function number(v: unknown, min = 1, max = 1_000_000): number {
  requireValue(
    typeof v === "number" && Number.isSafeInteger(v) && v >= min && v <= max,
  );
  return v;
}
function list(v: unknown, max: number): unknown[] {
  requireValue(Array.isArray(v) && v.length <= max);
  return v;
}
function strings(v: unknown, max: number, regex?: RegExp): string[] {
  const a = list(v, max).map((x) =>
    regex ? matching(x, regex) : text(x, 4000),
  );
  requireValue(new Set(a).size === a.length);
  return a;
}
function hashOrNull(v: unknown) {
  return v === null ? null : matching(v, hash);
}
function identity(v: Row, scope: DeliveryScope) {
  requireValue(
    v.version === 1 &&
      v.criteria_binding === "reporting_only_post_admission" &&
      v.enforced_at_admission === false &&
      v.project_id === scope.project &&
      v.campaign_id === scope.campaign,
  );
  for (const k of ["project_id", "campaign_id", "request_id", "actor_id"])
    matching(v[k], id);
  number(v.revision, 1, 50);
  hashOrNull(v.previous_digest);
  text(v.prepared_at, 64);
  requireValue(Number.isFinite(Date.parse(v.prepared_at as string)));
  const repos = strings(v.repositories, 200, id);
  requireValue(repos.length && repos.join() === [...repos].sort().join());
  matching(v.digest, hash);
  matching(v.request_digest, hash);
  const s = row(v.source);
  keys(s, [
    "campaign_journal_head",
    "campaign_event_count",
    "project_digest",
    "request_digest",
    "report_digest",
    "product_definitions",
  ]);
  for (const k of [
    "campaign_journal_head",
    "project_digest",
    "request_digest",
    "report_digest",
  ])
    matching(s[k], hash);
  number(s.campaign_event_count, 1, 1024);
  const definitions = list(s.product_definitions, 50);
  const seen = new Set<string>();
  for (const item of definitions) {
    const p = row(item);
    keys(p, ["repository_id", "revision", "digest"]);
    matching(p.repository_id, id);
    number(p.revision);
    matching(p.digest, hash);
    requireValue(
      repos.includes(p.repository_id as string) &&
        !seen.has(`${p.repository_id}:${p.revision}:${p.digest}`),
    );
    seen.add(`${p.repository_id}:${p.revision}:${p.digest}`);
  }
}
const identityKeys = [
  "version",
  "criteria_binding",
  "enforced_at_admission",
  "project_id",
  "campaign_id",
  "request_id",
  "actor_id",
  "revision",
  "previous_digest",
  "prepared_at",
  "repositories",
  "source",
  "request_digest",
  "digest",
];
async function seal(v: Row, max = 1_048_576) {
  requireValue(new TextEncoder().encode(JSON.stringify(v)).length <= max);
  matching(v.digest, hash);
  const body = Object.fromEntries(
    Object.entries(v).filter(([key]) => key !== "digest"),
  );
  requireValue((await attemptDigest(body)) === v.digest);
}
export function readDeliveryInput(value: unknown): DeliveryInput {
  const v = row(value);
  keys(v, [
    "request_id",
    "expected_previous_digest",
    "expected_campaign_request_digest",
    "expected_project_digest",
    "title",
    "waves",
    "milestones",
  ]);
  matching(v.request_id, id);
  hashOrNull(v.expected_previous_digest);
  matching(v.expected_campaign_request_digest, hash);
  matching(v.expected_project_digest, hash);
  text(v.title);
  const waves = list(v.waves, 20),
    milestones = list(v.milestones, 20);
  requireValue(waves.length && milestones.length);
  const waveIds = new Set<string>(),
    components = new Set<string>();
  for (const item of waves) {
    const w = row(item);
    keys(w, ["id", "name", "component_ids"]);
    const name = matching(w.id, slug);
    requireValue(!waveIds.has(name));
    waveIds.add(name);
    text(w.name);
    const c = strings(w.component_ids, 50, slug);
    requireValue(c.length);
    for (const name of c) {
      requireValue(!components.has(name));
      components.add(name);
    }
  }
  requireValue(components.size <= 50);
  const milestoneIds = new Set<string>(),
    covered = new Set<string>();
  let conditions = 0;
  for (const item of milestones) {
    const m = row(item);
    keys(m, [
      "id",
      "name",
      "wave_id",
      "conditions",
      "feature_ids",
      "journey_refs",
      "acceptance_refs",
    ]);
    const name = matching(m.id, slug);
    requireValue(!milestoneIds.has(name));
    milestoneIds.add(name);
    text(m.name);
    const waveId = matching(m.wave_id, slug);
    requireValue(waveIds.has(waveId));
    covered.add(waveId);
    const w = waves.find((x) => row(x).id === waveId) as Row;
    const cs = list(m.conditions, 20);
    requireValue(cs.length);
    conditions += cs.length;
    const seen = new Set<string>();
    for (const item of cs) {
      const c = row(item);
      keys(c, ["kind", "component_id", "task_id"]);
      requireValue(
        c.kind === "component_verified" || c.kind === "task_receipted",
      );
      matching(c.component_id, slug);
      requireValue(
        (w.component_ids as string[]).includes(c.component_id as string),
      );
      requireValue(
        c.kind === "component_verified"
          ? c.task_id === null
          : phases.includes(c.task_id as string),
      );
      const key = `${c.kind}:${c.component_id}:${c.task_id}`;
      requireValue(!seen.has(key));
      seen.add(key);
    }
    strings(m.feature_ids, 100);
    strings(m.journey_refs, 50);
    strings(m.acceptance_refs, 100);
  }
  requireValue(conditions <= 200 && covered.size === waveIds.size);
  requireValue(new TextEncoder().encode(JSON.stringify(v)).length <= 1_048_576);
  return value as DeliveryInput;
}
export async function readDeliveryPlanReceipt(
  value: unknown,
  scope: DeliveryScope,
  expected?: {
    actor: string;
    requestId: string;
    inputDigest: string;
    receiptDigest?: string;
    revision?: number;
  },
): Promise<DeliveryReceipt> {
  const v = row(value);
  keys(v, [...identityKeys, "request"]);
  identity(v, scope);
  const request = readDeliveryInput(v.request);
  const source = row(v.source);
  requireValue(
    request.request_id === v.request_id &&
      request.expected_previous_digest === v.previous_digest &&
      request.expected_project_digest === source.project_digest &&
      request.expected_campaign_request_digest === source.request_digest &&
      (await attemptDigest(request)) === v.request_digest,
  );
  if (expected)
    requireValue(
      v.actor_id === expected.actor &&
        v.request_id === expected.requestId &&
        v.request_digest === expected.inputDigest &&
        (expected.receiptDigest === undefined ||
          v.digest === expected.receiptDigest) &&
        (expected.revision === undefined || v.revision === expected.revision),
    );
  await seal(v);
  return value as DeliveryReceipt;
}
export async function readDeliveryPlanPage(
  value: unknown,
  scope: DeliveryScope,
): Promise<{
  project_id: string;
  campaign_id: string;
  items: DeliverySummary[];
}> {
  const v = row(value);
  keys(v, ["project_id", "campaign_id", "items"]);
  requireValue(
    v.project_id === scope.project && v.campaign_id === scope.campaign,
  );
  const items = list(v.items, 50);
  let previous: string | null = null;
  requireValue(
    new TextEncoder().encode(JSON.stringify(value)).length <= 1_048_576,
  );
  const requests = new Set<string>();
  for (const [i, item] of items.entries()) {
    const s = row(item);
    keys(s, [...identityKeys, "title", "receipt_digest"]);
    identity(s, scope);
    text(s.title);
    matching(s.receipt_digest, hash);
    requireValue(
      s.revision === i + 1 &&
        s.previous_digest === previous &&
        !requests.has(s.request_id as string),
    );
    requests.add(s.request_id as string);
    await seal(s);
    previous = s.receipt_digest as string;
  }
  return value as {
    project_id: string;
    campaign_id: string;
    items: DeliverySummary[];
  };
}
export async function readDeliveryProgress(
  value: unknown,
  plan: DeliveryReceipt,
  archive: CampaignArchiveReceipt,
): Promise<DeliveryProgress> {
  const v = row(value);
  keys(v, [
    "version",
    "criteria_binding",
    "enforced_at_admission",
    "project_id",
    "campaign_id",
    "plan_request_id",
    "plan_revision",
    "plan_digest",
    "report_request_id",
    "report_archive_digest",
    "report_digest",
    "plan_prepared_at",
    "report_prepared_at",
    "source",
    "scm_freshness",
    "state",
    "counts",
    "milestones",
    "waves",
    "digest",
  ]);
  requireValue(
    v.version === 1 &&
      v.criteria_binding === "reporting_only_post_admission" &&
      v.enforced_at_admission === false &&
      v.scm_freshness === "not_observed",
  );
  requireValue(
    v.project_id === plan.project_id &&
      v.campaign_id === plan.campaign_id &&
      archive.project_id === plan.project_id &&
      archive.campaign_id === plan.campaign_id &&
      v.plan_request_id === plan.request_id &&
      v.plan_revision === plan.revision &&
      v.plan_digest === plan.digest &&
      v.report_request_id === archive.request_id &&
      v.report_archive_digest === archive.digest &&
      v.report_digest === archive.report.digest &&
      v.plan_prepared_at === plan.prepared_at &&
      v.report_prepared_at === archive.prepared_at,
  );
  requireValue(
    JSON.stringify(v.source) === JSON.stringify(archive.source) ||
      (await attemptDigest(v.source)) === (await attemptDigest(archive.source)),
  );
  requireValue(
    plan.source.project_digest === archive.source.project_digest &&
      plan.source.request_digest === archive.source.request_digest,
  );
  const state = (x: unknown) => {
    requireValue(deliveryStates.includes(x as DeliveryState));
    return x as DeliveryState;
  };
  const counts = (x: unknown, states: string[]) => {
    const r = row(x);
    for (const [k, n] of Object.entries(r)) {
      state(k);
      number(n, 1, 200);
    }
    const expected: Record<string, number> = {};
    for (const s of states) expected[s] = (expected[s] ?? 0) + 1;
    requireValue(
      JSON.stringify(Object.entries(r).sort()) ===
        JSON.stringify(Object.entries(expected).sort()),
    );
  };
  const aggregate = (states: unknown[]) =>
    states.length
      ? ([
          "unknown",
          "blocked",
          "dependency_blocked",
          "in_progress",
          "not_started",
        ].find((s) => states.includes(s)) ?? "satisfied")
      : "unknown";
  state(v.state);
  const ms = list(v.milestones, 20),
    ws = list(v.waves, 20);
  requireValue(
    ms.length === plan.request.milestones.length &&
      ws.length === plan.request.waves.length,
  );
  const all: string[] = [];
  for (const [i, item] of ms.entries()) {
    const m = row(item),
      original = plan.request.milestones[i];
    keys(m, [
      "id",
      "name",
      "wave_id",
      "conditions",
      "feature_ids",
      "journey_refs",
      "acceptance_refs",
      "state",
      "counts",
    ]);
    for (const k of [
      "id",
      "name",
      "wave_id",
      "feature_ids",
      "journey_refs",
      "acceptance_refs",
    ] as const)
      requireValue(JSON.stringify(m[k]) === JSON.stringify(original[k]));
    state(m.state);
    const cs = list(m.conditions, 20);
    requireValue(cs.length === original.conditions.length);
    const states: string[] = [];
    for (const [j, item] of cs.entries()) {
      const c = row(item),
        originalC = original.conditions[j];
      keys(c, [
        "kind",
        "component_id",
        "task_id",
        "state",
        "reason",
        "run_id",
        "component_attempt",
        "task_attempt",
        "evidence_digest",
        "receipt",
      ]);
      for (const k of ["kind", "component_id", "task_id"] as const)
        requireValue(c[k] === originalC[k]);
      states.push(state(c.state));
      if (c.reason !== null) text(c.reason, 2000);
      if (c.run_id !== null) matching(c.run_id, id);
      if (c.component_attempt !== null) number(c.component_attempt);
      if (c.task_attempt !== null) number(c.task_attempt);
      hashOrNull(c.evidence_digest);
      if (c.receipt !== null) {
        const r = row(c.receipt);
        keys(r, ["sequence", "digest"]);
        number(r.sequence);
        matching(r.digest, hash);
      }
      const component = archive.report.components.find(
        (x) => x.component_id === c.component_id,
      );
      if (component)
        requireValue(
          c.run_id === component.run_id &&
            c.component_attempt === component.attempt,
        );
      else requireValue(c.state === "unknown" && c.run_id === null);
      if (c.state === "satisfied" && component) {
        if (c.kind === "component_verified")
          requireValue(
            component.state === "verified" &&
              component.verification === "verified",
          );
        else {
          const task = component.tasks.find((t) => t.id === c.task_id);
          requireValue(
            task?.state === "receipted" &&
              task.receipt !== null &&
              task.evidence_digest !== null &&
              task.attempt === c.task_attempt &&
              task.evidence_digest === c.evidence_digest &&
              (await attemptDigest(task.receipt)) ===
                (await attemptDigest(c.receipt)),
          );
        }
      }
    }
    requireValue(m.state === aggregate(states));
    counts(m.counts, states);
    all.push(...states);
  }
  for (const [i, item] of ws.entries()) {
    const w = row(item),
      original = plan.request.waves[i];
    keys(w, [
      "id",
      "name",
      "component_ids",
      "ordinal",
      "component_states",
      "milestone_ids",
      "observed_state",
      "state",
      "blocking_wave_ids",
    ]);
    for (const k of ["id", "name", "component_ids"] as const)
      requireValue(JSON.stringify(w[k]) === JSON.stringify(original[k]));
    requireValue(w.ordinal === i + 1);
    state(w.observed_state);
    state(w.state);
    const components = row(w.component_states);
    keys(components, original.component_ids);
    for (const [name, s] of Object.entries(components))
      requireValue(
        s ===
          (archive.report.components.find((c) => c.component_id === name)
            ?.state ?? "unknown"),
      );
    const mids = strings(w.milestone_ids, 20, slug);
    requireValue(
      mids.join() ===
        plan.request.milestones
          .filter((m) => m.wave_id === original.id)
          .map((m) => m.id)
          .join(),
    );
    const blocked = strings(w.blocking_wave_ids, 20, slug);
    requireValue(
      blocked.join() ===
        ws
          .slice(0, i)
          .filter((x) => row(x).state !== "satisfied")
          .map((x) => row(x).id)
          .join(),
    );
    const observed = aggregate([
      ...ms.filter((x) => row(x).wave_id === w.id).map((x) => row(x).state),
      ...Object.entries(components).map(([name, state]) =>
        state === "verified"
          ? archive.report.components.find((c) => c.component_id === name)
              ?.verification === "verified"
            ? "satisfied"
            : "unknown"
          : state,
      ),
    ]);
    requireValue(
      w.observed_state === observed &&
        w.state ===
          (blocked.length && observed === "satisfied"
            ? "dependency_blocked"
            : observed),
    );
  }
  requireValue(v.state === aggregate(ws.map((x) => row(x).state)));
  const total = row(v.counts);
  keys(total, ["conditions", "milestones", "waves"]);
  counts(total.conditions, all);
  counts(
    total.milestones,
    ms.map((x) => row(x).state as string),
  );
  counts(
    total.waves,
    ws.map((x) => row(x).state as string),
  );
  await seal(v, 16_777_216);
  return value as DeliveryProgress;
}
export type DeliveryGraph = {
  projectDigest: string;
  campaignDigest: string;
  components: { id: string; name: string; depends_on: string[] }[];
  phases: DeliveryPhase[];
};
export async function readDeliveryGraph(
  value: unknown,
  report: import("./CampaignReportData").CampaignReport,
): Promise<DeliveryGraph> {
  requireValue(
    new TextEncoder().encode(JSON.stringify(value)).length <= 16_777_216,
  );
  const v = row(value),
    project = row(v.project_snapshot),
    definition = row(project.definition),
    launch = row(v.launch);
  requireValue(
    v.id === report.campaign.id &&
      v.project_id === report.project_id &&
      project.id === report.project_id &&
      project.digest === report.campaign.project_digest &&
      v.request_digest === report.campaign.request_digest &&
      report.counts !== null &&
      report.components.length > 0 &&
      report.components.length <= 50,
  );
  const { digest, ...body } = report;
  requireValue((await attemptDigest(body)) === digest);
  matching(project.digest, hash);
  matching(v.request_digest, hash);
  requireValue((await attemptDigest(launch)) === v.request_digest);
  const frozen = list(definition.components, 50).map(row),
    children = list(v.children, 250)
      .map(row)
      .filter((c) => c.current === true);
  requireValue(
    children.length === report.components.length &&
      new Set(children.map((c) => c.component_id)).size === children.length,
  );
  const components = report.components.map((c) => {
    const f = frozen.find((f) => f.id === c.component_id),
      child = children.find((x) => x.component_id === c.component_id);
    requireValue(
      f &&
        child &&
        child.run_id === c.run_id &&
        child.attempt === c.attempt &&
        f.repository_id === c.repository_id,
    );
    const dependencies = strings(f.depends_on, 50, slug);
    requireValue(
      [...dependencies].sort().join() === [...c.depends_on].sort().join(),
    );
    return { id: c.component_id, name: text(f.name), depends_on: dependencies };
  });
  requireValue(
    components.every((c) =>
      c.depends_on.every((id) => components.some((x) => x.id === id)),
    ),
  );
  requireValue(
    ["build", "maintain", "test"].includes(launch.operation as string),
  );
  const allowed: DeliveryPhase[] = ["prepare", "verify", "checkpoint"];
  if (launch.operation !== "test") allowed.splice(1, 0, "implement");
  if (row(launch.assurance).review_agent) allowed.push("review");
  return {
    projectDigest: project.digest as string,
    campaignDigest: v.request_digest as string,
    components,
    phases: allowed,
  };
}
export function validateDeliveryGraph(
  input: DeliveryInput,
  graph: DeliveryGraph,
) {
  readDeliveryInput(input);
  requireValue(
    input.expected_project_digest === graph.projectDigest &&
      input.expected_campaign_request_digest === graph.campaignDigest,
  );
  const placements = new Map(
    input.waves.flatMap((w, i) =>
      w.component_ids.map((id) => [id, i] as const),
    ),
  );
  requireValue(
    placements.size === graph.components.length &&
      graph.components.every(
        (c) =>
          placements.has(c.id) &&
          c.depends_on.every(
            (id) =>
              placements.has(id) &&
              placements.get(id)! <= placements.get(c.id)!,
          ),
      ),
  );
  requireValue(
    input.milestones.every((m) =>
      m.conditions.every(
        (c) => c.task_id === null || graph.phases.includes(c.task_id),
      ),
    ),
  );
}
