/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
import { setup, selectCampaign } from "./campaign-report-history-fixture";
import {
  deliveryFixture,
  digest,
  sealed,
} from "./campaign-delivery-plan-fixture";
export async function setupDelivery(page: Page, reader = false) {
  const base = await setup(page, reader),
    fixture = deliveryFixture();
  const state = {
    base,
    fixture,
    posts: [] as Record<string, unknown>[],
    lists: 0,
    reads: 0,
    progressReads: 0,
    markerBefore: false,
    postMode: "success" as "success" | "lost" | "conflict",
    readStatus: 200,
    listStatus: 200,
    progressGate: null as Promise<void> | null,
    receiptGate: null as Promise<void> | null,
    receipts: new Map<string, ReturnType<typeof deliveryFixture>["receipt"]>(),
    summaries: [] as ReturnType<typeof deliveryFixture>["page"]["items"],
  };
  base.report = fixture.report;
  base.archiveReceipt = fixture.archive;
  const {
    report: ignoredReport,
    request_digest: ignoredRequest,
    ...archiveIdentity
  } = fixture.archive;
  void ignoredReport;
  void ignoredRequest;
  base.archivePage = {
    project_id: fixture.report.project_id,
    campaign_id: fixture.report.campaign.id,
    items: [
      sealed({
        ...archiveIdentity,
        state: fixture.report.state,
        report_digest: fixture.report.digest,
        receipt_digest: fixture.archive.digest,
      }),
    ],
    next_after: null,
  };
  const graph = {
    id: fixture.report.campaign.id,
    project_id: fixture.report.project_id,
    request_digest: fixture.report.campaign.request_digest,
    launch: fixture.launch,
    project_snapshot: {
      id: fixture.report.project_id,
      digest: fixture.report.campaign.project_digest,
      definition: {
        components: fixture.report.components.map((c) => ({
          id: c.component_id,
          name: "Frozen inventory API",
          repository_id: c.repository_id,
          depends_on: c.depends_on,
        })),
      },
    },
    children: fixture.report.components.map((c) => ({
      component_id: c.component_id,
      current: true,
      run_id: c.run_id,
      attempt: c.attempt,
    })),
  };
  await page.route(
    `**/projects/${fixture.report.project_id}/campaigns/${fixture.report.campaign.id}`,
    (route) => route.fulfill({ json: graph }),
  );
  await page.route("**/delivery-plan-revisions**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      state.posts.push(body);
      state.markerBefore = await page.evaluate(
        (id) =>
          Object.entries(localStorage).some(
            ([k, raw]) =>
              k.startsWith("forge.campaign-delivery-plan.v1:") &&
              JSON.parse(raw).requestId === id,
          ),
        body.request_id,
      );
      if (state.postMode === "conflict")
        return route.fulfill({
          status: 409,
          json: { detail: "History advanced" },
        });
      const receipt = sealed({
        ...fixture.receipt,
        request_id: body.request_id,
        revision: state.summaries.length + 1,
        previous_digest: body.expected_previous_digest,
        request: body,
        request_digest: digest(body),
      });
      state.receipts.set(body.request_id, receipt);
      const { request: ignored, ...identity } = receipt;
      void ignored;
      state.summaries.push(
        sealed({
          ...identity,
          title: body.title,
          receipt_digest: receipt.digest,
        }),
      );
      if (state.postMode === "lost") return route.abort("failed");
      return route.fulfill({ json: receipt });
    }
    if (pathname.endsWith("/progress")) {
      state.progressReads++;
      const requestId = pathname.split("/").at(-2)!;
      const plan = state.receipts.get(requestId)!;
      const progress = sealed({
        ...fixture.progress,
        plan_request_id: requestId,
        plan_revision: plan.revision,
        plan_digest: plan.digest,
        milestones: plan.request.milestones.map((m) => ({
          ...m,
          state: "in_progress",
          counts: { in_progress: m.conditions.length },
          conditions: m.conditions.map((c) => ({
            ...fixture.progress.milestones[0].conditions[0],
            ...c,
          })),
        })),
        waves: plan.request.waves.map((w, i) => ({
          ...fixture.progress.waves[0],
          ...w,
          ordinal: i + 1,
          milestone_ids: plan.request.milestones
            .filter((m) => m.wave_id === w.id)
            .map((m) => m.id),
        })),
        counts: {
          conditions: {
            in_progress: plan.request.milestones.reduce(
              (n, m) => n + m.conditions.length,
              0,
            ),
          },
          milestones: { in_progress: plan.request.milestones.length },
          waves: { in_progress: plan.request.waves.length },
        },
      });
      if (state.progressGate) await state.progressGate;
      return route.fulfill({ json: progress });
    }
    if (pathname.endsWith("/delivery-plan-revisions")) {
      state.lists++;
      return route.fulfill({
        status: state.listStatus,
        json: {
          project_id: fixture.report.project_id,
          campaign_id: fixture.report.campaign.id,
          items: state.summaries,
        },
      });
    }
    state.reads++;
    if (state.receiptGate) await state.receiptGate;
    return route.fulfill({
      status: state.readStatus,
      json: state.receipts.get(pathname.split("/").at(-1)!) ?? {
        detail: "Missing",
      },
    });
  });
  await selectCampaign(page, base);
  return state;
}
export async function reviewPlan(page: Page, task?: "prepare") {
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await page
    .getByRole("button", { name: "Review a reporting plan", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Review reporting plan",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await dialog
    .getByLabel("Plan name", { exact: true })
    .fill("Inventory delivery");
  await dialog.getByRole("button", { name: "Add wave", exact: true }).click();
  await dialog.getByLabel("Wave 1 name", { exact: true }).fill("Core API");
  await dialog
    .getByRole("checkbox", { name: "Frozen inventory API", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "Continue to milestones", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Add milestone to Core API", exact: true })
    .click();
  await dialog
    .getByLabel("Milestone 1 name in Core API", { exact: true })
    .fill("Inventory checked");
  if (task) {
    await dialog
      .getByRole("combobox", { name: "Evidence required", exact: true })
      .selectOption("task_receipted");
    await expect(
      dialog.getByRole("option", { name: "implement", exact: true }),
    ).toHaveCount(0);
    await expect(
      dialog.getByRole("option", { name: "review", exact: true }),
    ).toHaveCount(0);
    await dialog
      .getByRole("combobox", { name: "Task phase", exact: true })
      .selectOption(task);
  }
  await dialog
    .getByRole("button", { name: "Review reporting criteria", exact: true })
    .click();
  await dialog
    .getByRole("checkbox", {
      name: "I understand: reporting only.",
      exact: true,
    })
    .check();
  return dialog;
}
