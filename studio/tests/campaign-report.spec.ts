/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import { verifiedReport, unknownReport } from "./campaign-report-fixture";
// These controlled API fixtures qualify read presentation, not native campaign execution.
const projectId = verifiedReport.project_id,
  campaignId = verifiedReport.campaign.id,
  repositoryId = verifiedReport.components[0].repository_id;
const component = {
  id: "api",
  name: "Inventory API",
  repository_id: repositoryId,
  path: ".",
  responsibility: "Fictional inventory service",
  kind: "api",
  backend: "pyfly",
  frontend: "none",
  verification: [],
  depends_on: [],
};
const project = {
  id: projectId,
  revision: 1,
  digest: "a".repeat(64),
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  definition: {
    name: "Owned campaign product",
    description: "Fictional local receipt inspection",
    owners: ["Owned fixture"],
    topology: "monorepo",
    product_repository_id: repositoryId,
    components: [component],
  },
};
async function setup(page: Page) {
  const state = {
    lists: 0,
    reports: 0,
    writes: 0,
    campaigns: [] as unknown[],
    listStatus: 200,
    reportStatus: 200,
    report: null as unknown,
    reportGate: null as Promise<void> | null,
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.includes("/campaigns")) {
      if (route.request().method() !== "GET") state.writes++;
      if (path.endsWith("/report")) {
        state.reports++;
        const report = state.report;
        if (state.reportGate) await state.reportGate;
        return route.fulfill({
          status: state.reportStatus,
          json: report ?? { detail: "private report error canary" },
        });
      }
      state.lists++;
      return route.fulfill({
        status: state.listStatus,
        json:
          state.listStatus === 200
            ? state.campaigns
            : { detail: "private list error canary" },
      });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: repositoryId,
            name: "Owned fixture",
            url: "/fixture",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/projects")
        ? [project]
        : path.endsWith("/projects/" + projectId)
          ? {
              ...project,
              components: [
                {
                  ...component,
                  latest_status: "not_started",
                  repository_run_count: 0,
                  runs: [],
                },
              ],
              counts: { not_started: 1 },
            }
          : path.endsWith("/health")
            ? { status: "ok", execution_ready: false }
            : [];
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("owned-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expectAuthenticated(page);
  await navigate(page, "Product projects");
  await expect(
    page.getByRole("heading", { name: "Owned campaign product", exact: true }),
  ).toBeVisible();
  return state;
}
test("campaign listing is an explicit GET and an empty list is not an unknown or verified report", async ({
  page,
}) => {
  const state = await setup(page);
  expect(state.lists).toBe(0);
  await page
    .getByRole("button", { name: "Load campaigns", exact: true })
    .click();
  await expect(
    page.getByText("No campaigns are recorded for this product."),
  ).toBeVisible();
  expect(state.lists).toBe(1);
  expect(state.reports).toBe(0);
  expect(state.writes).toBe(0);
});
test("unsupported or denied listing stays distinct from empty and never reflects private error text", async ({
  page,
}) => {
  const state = await setup(page);
  state.listStatus = 404;
  await page
    .getByRole("button", { name: "Load campaigns", exact: true })
    .click();
  await expect(
    page.getByText(
      "Campaign reading is unavailable for this product or server.",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("No campaigns are recorded for this product."),
  ).toHaveCount(0);
  await expect(page.getByText("private list error canary")).toHaveCount(0);
  expect(state.writes).toBe(0);
});

async function selectCampaign(
  page: Page,
  state: Awaited<ReturnType<typeof setup>>,
) {
  state.campaigns = [
    {
      id: campaignId,
      project_id: projectId,
      status: "running",
      created_at: "2026-10-15T00:00:00Z",
    },
  ];
  await page
    .getByRole("button", { name: "Load campaigns", exact: true })
    .click();
  await page.getByLabel("Campaign", { exact: true }).selectOption(campaignId);
}
test("explicit report read shows bounded recorded evidence and never dispatches or claims live SCM", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = verifiedReport;
  let planReads = 0;
  await page.route("**/delivery-plan-revisions", (route) => {
    expect(route.request().method()).toBe("GET");
    planReads++;
    return route.fulfill({
      json: { project_id: projectId, campaign_id: campaignId, items: [] },
    });
  });
  await selectCampaign(page, state);
  const reviewPlan = page.getByRole("button", {
    name: "Review a reporting plan",
    exact: true,
  });
  await expect(reviewPlan).toBeDisabled();
  expect(state.reports).toBe(0);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  const report = page.getByRole("region", {
    name: "Campaign report snapshot",
    exact: true,
  });
  await expect(report).toBeVisible();
  await expect(reviewPlan).toBeEnabled();
  await expect(
    report.getByText(
      "Based on saved checks and execution records. Evidence state and recorded workflow status can differ.",
    ),
  ).toBeVisible();
  await expect(report).not.toContainText(/\d+%/);
  await report
    .getByRole("button", { name: "Inspect component api", exact: true })
    .click();
  await expect(
    report.getByRole("heading", { name: "Component api", exact: true }),
  ).toBeVisible();
  await expect(
    report.getByRole("heading", { name: "Task checkpoint", exact: true }),
  ).toBeVisible();
  await report.getByRole("tab", { name: "Checks", exact: true }).click();
  await expect(
    report.getByRole("heading", { name: "Recorded check 1", exact: true }),
  ).toBeVisible();
  await report.getByRole("tab", { name: "Source", exact: true }).click();
  await expect(
    report.getByText(verifiedReport.components[0].branch, { exact: true }),
  ).toBeVisible();
  await report.getByRole("tab", { name: "References", exact: true }).click();
  await report.getByText("About this report", { exact: true }).click();
  await expect(
    report.getByText(
      "SCM freshness: not observed. This report format does not include the separate reporting-plan criteria.",
    ),
  ).toBeVisible();
  await expect(
    report.getByText("Server report digest (reference)", { exact: true }),
  ).toBeVisible();
  await expect(
    report.getByText(verifiedReport.digest, { exact: true }),
  ).toBeVisible();
  await expect(
    report.getByText("Advanced record", { exact: false }),
  ).toHaveCount(0);
  await expect(
    page.getByText("No reporting plan is saved for this campaign.", {
      exact: true,
    }),
  ).toHaveCount(0);
  expect(planReads).toBe(0);
  await page
    .getByRole("button", { name: "Read reporting plans", exact: true })
    .click();
  await expect(
    page.getByText("No reporting plan is saved for this campaign.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(planReads).toBe(1);
  await page.clock.install();
  await page.clock.fastForward(15000);
  expect(state.lists).toBe(1);
  expect(state.reports).toBe(1);
  expect(state.writes).toBe(0);
});
test("unknown retained evidence and unavailable snapshot counts are not zero or empty campaigns", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = {
    ...unknownReport,
    project_id: projectId,
    campaign: { ...unknownReport.campaign, id: campaignId },
  };
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  const report = page.getByRole("region", {
    name: "Campaign report snapshot",
    exact: true,
  });
  await expect(
    report.getByText("Unknown", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("No campaigns are recorded for this product."),
  ).toHaveCount(0);
  state.report = {
    ...verifiedReport,
    state: "unknown",
    reason: "evidence_integrity",
    counts: null,
    components: [],
    campaign: {
      id: campaignId,
      recorded_status: null,
      project_digest: null,
      request_digest: null,
      journal_head: null,
      event_count: null,
    },
  };
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    report.getByText(
      "Component counts are unavailable; this snapshot does not establish an empty or verified campaign.",
    ),
  ).toBeVisible();
  await expect(
    report.getByRole("button", { name: /Inspect component/ }),
  ).toHaveCount(0);
  expect(state.writes).toBe(0);
});
test("a denied reread clears private report evidence and never reflects HTTP detail", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = verifiedReport;
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toBeVisible();
  state.reportStatus = 403;
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText(
      "Access to campaign evidence could not be confirmed. Sign in or ask an authorized project reader.",
    ),
  ).toBeVisible();
  await expect(page.getByText("private report error canary")).toHaveCount(0);
  expect(state.writes).toBe(0);
});

test("a late report for a deselected campaign cannot paint a current selection", async ({
  page,
}) => {
  const state = await setup(page);
  const other = "e".repeat(32);
  state.campaigns = [campaignId, other].map((id) => ({
    id,
    project_id: projectId,
    status: "running",
  }));
  state.report = verifiedReport;
  await page
    .getByRole("button", { name: "Load campaigns", exact: true })
    .click();
  await page.getByLabel("Campaign", { exact: true }).selectOption(campaignId);
  let release!: () => void;
  state.reportGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect.poll(() => state.reports).toBe(1);
  await page.getByLabel("Campaign", { exact: true }).selectOption(other);
  release();
  await expect(page.getByLabel("Campaign", { exact: true })).toHaveValue(other);
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "View report", exact: true }),
  ).toBeEnabled();
  expect(state.reports).toBe(1);
  expect(state.writes).toBe(0);
});

test("malformed and foreign report reads clear prior evidence instead of establishing unknown", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = verifiedReport;
  await selectCampaign(page, state);
  const read = page.getByRole("button", {
    name: "View report",
    exact: true,
  });
  await read.click();
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toBeVisible();
  state.report = { ...verifiedReport, project_id: "e".repeat(32) };
  await read.click();
  await expect(
    page
      .getByRole("region", { name: "Campaign reports", exact: true })
      .getByRole("alert"),
  ).toContainText("No report state has been established");
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toHaveCount(0);
  state.reportStatus = 500;
  await read.click();
  await expect(
    page
      .getByRole("region", { name: "Campaign reports", exact: true })
      .getByRole("alert"),
  ).toContainText("No report state has been established");
  expect(state.writes).toBe(0);
});

test("large retained reports render only one bounded component and evidence page at a time", async ({
  page,
}) => {
  const state = await setup(page),
    item = verifiedReport.components[0];
  const tasks = Array.from({ length: 128 }, (_, i) => ({
    ...item.tasks[0],
    id: "task-" + i,
  }));
  state.report = {
    ...verifiedReport,
    counts: { verified: 60 },
    components: Array.from({ length: 60 }, (_, i) => ({
      ...item,
      component_id: "component-" + i,
      tasks,
    })),
  };
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  const report = page.getByRole("region", {
    name: "Campaign report snapshot",
    exact: true,
  });
  await expect(
    report.getByRole("button", { name: /^Inspect component/ }),
  ).toHaveCount(20);
  await expect(report.getByRole("tabpanel")).toHaveCount(0);
  await report
    .getByRole("button", { name: "Inspect component component-0", exact: true })
    .click();
  await expect(
    report.getByRole("heading", { name: /^Task task-/ }),
  ).toHaveCount(20);
  await report.getByRole("button", { name: "Next tasks", exact: true }).click();
  await expect(
    report.getByRole("heading", { name: "Task task-20", exact: true }),
  ).toBeVisible();
  await expect(
    report.getByRole("heading", { name: /^Task task-/ }),
  ).toHaveCount(20);
  await report
    .getByRole("button", { name: "Next components", exact: true })
    .click();
  await expect(report.getByRole("tabpanel")).toHaveCount(0);
  await expect(
    report.getByRole("button", { name: /^Inspect component/ }),
  ).toHaveCount(20);
  expect(state.writes).toBe(0);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 390, height: 600 },
  { width: 844, height: 390 },
])
  for (const theme of ["light", "dark"])
    test(`report keyboard and painted controls ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      const state = await setup(page);
      state.report = verifiedReport;
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await selectCampaign(page, state);
      const read = page.getByRole("button", {
        name: "View report",
        exact: true,
      });
      await read.click();
      const report = page.getByRole("region", {
        name: "Campaign report snapshot",
        exact: true,
      });
      await expect(
        report.getByRole("heading", {
          name: "Campaign report snapshot",
          exact: true,
        }),
      ).toBeFocused();
      const inspect = report.getByRole("button", {
        name: "Inspect component api",
        exact: true,
      });
      await inspect.scrollIntoViewIfNeeded();
      const box = (await inspect.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
      expect(box.height).toBeGreaterThanOrEqual(36);
      expect(
        await inspect.evaluate((node) => {
          const r = node.getBoundingClientRect();
          return node.contains(
            document.elementFromPoint(
              r.left + r.width / 2,
              r.top + r.height / 2,
            ),
          );
        }),
      ).toBe(true);
      await page.evaluate(() => document.fonts.ready.then(() => undefined));
      await page.screenshot({
        path: testInfo.outputPath(`report-${viewport.width}-${theme}.png`),
      });
      await inspect.click();
      const tasks = report.getByRole("tab", { name: "Tasks", exact: true });
      await tasks.focus();
      await page.keyboard.press("ArrowRight");
      await expect(
        report.getByRole("tab", { name: "Checks", exact: true }),
      ).toBeFocused();
      await expect(
        report.getByRole("heading", { name: "Recorded check 1", exact: true }),
      ).toBeVisible();
      await page.keyboard.press("End");
      const refs = report.getByRole("tab", { name: "References", exact: true });
      await expect(refs).toBeFocused();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(
        await refs.evaluate((node) => {
          const r = node.getBoundingClientRect();
          return node.contains(
            document.elementFromPoint(
              r.left + r.width / 2,
              r.top + r.height / 2,
            ),
          );
        }),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(`references-${viewport.width}-${theme}.png`),
      });
      expect(state.lists).toBe(1);
      expect(state.reports).toBe(1);
      expect(state.writes).toBe(0);
    });

test("actor and membership changes discard private reports and late reads", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = verifiedReport;
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toBeVisible();
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "e".repeat(32),
          display_name: "Changed operator",
          email: null,
          authentication_method: "local_bootstrap",
        },
        global_admin: false,
        memberships: [],
        roles: {},
      },
    }),
  );
  await page.evaluate(() =>
    window.dispatchEvent(new Event("forge-session-expired")),
  );
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Campaign reports", exact: true }),
  ).toHaveCount(0);
  expect(state.reports).toBe(1);
  expect(state.writes).toBe(0);
});

test("a pending report cannot survive a changed current actor", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = verifiedReport;
  await selectCampaign(page, state);
  let release!: () => void;
  state.reportGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect.poll(() => state.reports).toBe(1);
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "e".repeat(32),
          display_name: "Changed operator",
          email: null,
          authentication_method: "local_bootstrap",
        },
        global_admin: true,
        memberships: [],
        roles: {},
      },
    }),
  );
  await page.evaluate(() =>
    window.dispatchEvent(new Event("forge-session-expired")),
  );
  await expect(
    page.getByRole("button", { name: "Load campaigns", exact: true }),
  ).toBeEnabled();
  release();
  await expect(
    page.getByRole("region", { name: "Campaign report snapshot", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Campaign", { exact: true })).toHaveCount(0);
  expect(state.reports).toBe(1);
  expect(state.writes).toBe(0);
});
