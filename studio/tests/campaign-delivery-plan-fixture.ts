/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
const cryptoModule = "node:crypto";
const { createHash } = await import(/* @vite-ignore */ cryptoModule);
import archive from "./campaign-report-archive-fixture.json" with { type: "json" };
// Constructed reporting-plan envelopes anchored to the retained report fixture; not native plan acceptance.
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`)
      .join(",")}}`;
  return JSON.stringify(value);
}
export const digest = (value: unknown) =>
  createHash("sha256").update(canonical(value)).digest("hex");
export function sealed<T extends Record<string, unknown>>(row: T) {
  const body = Object.fromEntries(
    Object.entries(row).filter(([key]) => key !== "digest"),
  );
  return { ...row, digest: digest(body) };
}
export function deliveryFixture() {
  const launch = { operation: "test", assurance: { review_agent: null } };
  const report = sealed({
    ...structuredClone(archive.receipt.report),
    campaign: {
      ...archive.receipt.report.campaign,
      request_digest: digest(launch),
    },
  });
  const saved = sealed({
    ...structuredClone(archive.receipt),
    report,
    source: {
      ...archive.receipt.source,
      request_digest: report.campaign.request_digest,
    },
    request_digest: digest({
      request_id: archive.receipt.request_id,
      expected_report_digest: report.digest,
    }),
  });
  const component = report.components[0];
  const request = {
    request_id: "a".repeat(32),
    expected_previous_digest: null as string | null,
    expected_campaign_request_digest: report.campaign.request_digest,
    expected_project_digest: report.campaign.project_digest,
    title: "Inventory evidence — café 🚀",
    waves: [
      {
        id: "first",
        name: "API delivery",
        component_ids: [component.component_id],
      },
    ],
    milestones: [
      {
        id: "ready",
        name: "API checked",
        wave_id: "first",
        conditions: [
          {
            kind: "component_verified",
            component_id: component.component_id,
            task_id: null,
          },
        ],
        feature_ids: [],
        journey_refs: [],
        acceptance_refs: [],
      },
    ],
  };
  const identity = {
    version: 1,
    criteria_binding: "reporting_only_post_admission",
    enforced_at_admission: false,
    project_id: report.project_id,
    campaign_id: report.campaign.id,
    request_id: request.request_id,
    actor_id: "f".repeat(32),
    revision: 1,
    previous_digest: null,
    prepared_at: "2026-10-15T12:00:00+00:00",
    repositories: archive.receipt.repositories,
    source: {
      ...saved.source,
      report_digest: report.digest,
      product_definitions: [],
    },
    request_digest: digest(request),
  };
  const receipt = sealed({ ...identity, request });
  const summary = sealed({
    ...identity,
    title: request.title,
    receipt_digest: receipt.digest,
  });
  const page = {
    project_id: report.project_id,
    campaign_id: report.campaign.id,
    items: [summary],
  };
  const progress = sealed({
    version: 1,
    criteria_binding: "reporting_only_post_admission",
    enforced_at_admission: false,
    project_id: report.project_id,
    campaign_id: report.campaign.id,
    plan_request_id: request.request_id,
    plan_revision: 1,
    plan_digest: receipt.digest,
    report_request_id: archive.receipt.request_id,
    report_archive_digest: saved.digest,
    report_digest: report.digest,
    plan_prepared_at: identity.prepared_at,
    report_prepared_at: archive.receipt.prepared_at,
    source: saved.source,
    scm_freshness: "not_observed",
    state: "in_progress",
    counts: {
      conditions: { in_progress: 1 },
      milestones: { in_progress: 1 },
      waves: { in_progress: 1 },
    },
    milestones: [
      {
        ...request.milestones[0],
        state: "in_progress",
        counts: { in_progress: 1 },
        conditions: [
          {
            ...request.milestones[0].conditions[0],
            state: "in_progress",
            reason: "component_not_verified",
            run_id: component.run_id,
            component_attempt: component.attempt,
            task_attempt: null,
            evidence_digest: null,
            receipt: null,
          },
        ],
      },
    ],
    waves: [
      {
        ...request.waves[0],
        ordinal: 1,
        component_states: { [component.component_id]: component.state },
        milestone_ids: ["ready"],
        observed_state: "in_progress",
        state: "in_progress",
        blocking_wave_ids: [],
      },
    ],
  });
  return { launch, report, archive: saved, request, receipt, page, progress };
}
