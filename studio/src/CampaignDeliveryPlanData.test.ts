/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  deliveryFixture,
  sealed,
} from "../tests/campaign-delivery-plan-fixture";
async function api() {
  const path = "./CampaignDeliveryPlanData";
  const value = await import(/* @vite-ignore */ path).catch(() => null);
  expect(value, "Reporting plan response binding is missing").not.toBeNull();
  return value!;
}
it("binds a canonical Unicode plan receipt to its actor and reviewed input", async () => {
  const data = deliveryFixture();
  const parser = await api();
  expect(
    await parser.readDeliveryPlanReceipt(
      data.receipt,
      { project: data.report.project_id, campaign: data.report.campaign.id },
      {
        actor: data.receipt.actor_id,
        requestId: data.request.request_id,
        inputDigest: data.receipt.request_digest,
      },
    ),
  ).toEqual(data.receipt);
  await expect(
    parser.readDeliveryPlanReceipt(
      sealed({ ...data.receipt, actor_id: "b".repeat(32) }),
      { project: data.report.project_id, campaign: data.report.campaign.id },
      {
        actor: data.receipt.actor_id,
        requestId: data.request.request_id,
        inputDigest: data.receipt.request_digest,
      },
    ),
  ).rejects.toThrow();
});
it("rejects an unsealed or reordered revision history", async () => {
  const d = deliveryFixture(),
    parser = await api(),
    scope = { project: d.report.project_id, campaign: d.report.campaign.id };
  expect((await parser.readDeliveryPlanPage(d.page, scope)).items).toHaveLength(
    1,
  );
  for (const summary of [
    { ...d.page.items[0], title: "forged" },
    sealed({ ...d.page.items[0], revision: 2 }),
  ])
    await expect(
      parser.readDeliveryPlanPage({ ...d.page, items: [summary] }, scope),
    ).rejects.toThrow();
});
it("binds progress to both exact immutable selections and refuses invented enforcement", async () => {
  const d = deliveryFixture(),
    parser = await api();
  expect(
    await parser.readDeliveryProgress(d.progress, d.receipt, d.archive),
  ).toEqual(d.progress);
  for (const change of [
    { plan_digest: "9".repeat(64) },
    { report_archive_digest: "9".repeat(64) },
    { enforced_at_admission: true },
    { scm_freshness: "current" },
  ])
    await expect(
      parser.readDeliveryProgress(
        sealed({ ...d.progress, ...change }),
        d.receipt,
        d.archive,
      ),
    ).rejects.toThrow();
});
it("rejects a validly re-signed progress result that promotes an unresolved milestone", async () => {
  const d = deliveryFixture(),
    parser = await api();
  const changed = sealed({
    ...d.progress,
    state: "satisfied",
    milestones: d.progress.milestones.map((m) => ({
      ...m,
      state: "satisfied",
    })),
    counts: { ...d.progress.counts, milestones: { satisfied: 1 } },
  });
  await expect(
    parser.readDeliveryProgress(changed, d.receipt, d.archive),
  ).rejects.toThrow();
});
it("enforces input uniqueness, scope and bounded reporting conditions", async () => {
  const d = deliveryFixture(),
    parser = await api();
  for (const change of [
    { title: "x".repeat(201) },
    { waves: [...d.request.waves, { ...d.request.waves[0], id: "duplicate" }] },
    { milestones: [] },
    {
      milestones: [
        {
          ...d.request.milestones[0],
          conditions: [
            {
              kind: "task_receipted",
              component_id: "foreign",
              task_id: "publish",
            },
          ],
        },
      ],
    },
  ])
    expect(() =>
      parser.readDeliveryInput({ ...d.request, ...change }),
    ).toThrow();
  const graph = {
    projectDigest: d.request.expected_project_digest,
    campaignDigest: d.request.expected_campaign_request_digest,
    components: [
      { id: "api", name: "API", depends_on: ["web"] },
      { id: "web", name: "Web", depends_on: [] },
    ],
    phases: ["prepare", "verify", "checkpoint"],
  };
  const bad = {
    ...d.request,
    waves: [
      { id: "first", name: "First", component_ids: ["api"] },
      { id: "later", name: "Later", component_ids: ["web"] },
    ],
    milestones: [
      d.request.milestones[0],
      {
        ...d.request.milestones[0],
        id: "web-ready",
        wave_id: "later",
        conditions: [
          { kind: "component_verified", component_id: "web", task_id: null },
        ],
      },
    ],
  };
  bad.milestones[0] = { ...bad.milestones[0], wave_id: "first" };
  expect(() => parser.validateDeliveryGraph(bad, graph)).toThrow();
});
it("loads only the frozen campaign graph and rejects altered launch capabilities or source cuts", async () => {
  const d = deliveryFixture(),
    parser = await api(),
    component = d.report.components[0];
  const campaign = {
    id: d.report.campaign.id,
    project_id: d.report.project_id,
    request_digest: d.report.campaign.request_digest,
    launch: d.launch,
    project_snapshot: {
      id: d.report.project_id,
      digest: d.report.campaign.project_digest,
      definition: {
        components: [
          {
            id: component.component_id,
            name: "Frozen API",
            repository_id: component.repository_id,
            depends_on: component.depends_on,
          },
        ],
      },
    },
    children: [
      {
        current: true,
        component_id: component.component_id,
        run_id: component.run_id,
        attempt: component.attempt,
      },
    ],
  };
  const graph = await parser.readDeliveryGraph(campaign, d.report);
  expect(graph.phases).toEqual(["prepare", "verify", "checkpoint"]);
  await expect(
    parser.readDeliveryGraph(
      { ...campaign, launch: { ...d.launch, operation: "build" } },
      d.report,
    ),
  ).rejects.toThrow();
  await expect(
    parser.readDeliveryGraph(
      { ...campaign, children: [{ ...campaign.children[0], attempt: 2 }] },
      d.report,
    ),
  ).rejects.toThrow();
  await expect(
    parser.readDeliveryGraph(
      {
        ...campaign,
        project_snapshot: {
          ...campaign.project_snapshot,
          digest: "9".repeat(64),
        },
      },
      d.report,
    ),
  ).rejects.toThrow();
});
it("rejects foreign scope, unknown receipt fields and a re-signed inconsistent source cut", async () => {
  const d = deliveryFixture(),
    parser = await api(),
    scope = { project: d.report.project_id, campaign: d.report.campaign.id };
  await expect(
    parser.readDeliveryPlanReceipt(d.receipt, {
      ...scope,
      campaign: "9".repeat(32),
    }),
  ).rejects.toThrow();
  await expect(
    parser.readDeliveryPlanReceipt(
      sealed({ ...d.receipt, private: "not-allowed" }),
      scope,
    ),
  ).rejects.toThrow();
  await expect(
    parser.readDeliveryPlanReceipt(
      sealed({
        ...d.receipt,
        source: { ...d.receipt.source, project_digest: "9".repeat(64) },
      }),
      scope,
    ),
  ).rejects.toThrow();
});
it("an exact history read must match the selected immutable receipt digest and revision", async () => {
  const d = deliveryFixture(),
    parser = await api();
  await expect(
    parser.readDeliveryPlanReceipt(
      d.receipt,
      { project: d.report.project_id, campaign: d.report.campaign.id },
      {
        actor: d.receipt.actor_id,
        requestId: d.request.request_id,
        inputDigest: d.receipt.request_digest,
        receiptDigest: "9".repeat(64),
        revision: 1,
      },
    ),
  ).rejects.toThrow();
});
