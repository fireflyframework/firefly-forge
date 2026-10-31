/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
import { expect, test, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import fixture from "./git-delivery-contract-fixture.json" with { type: "json" };
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";

const component = {
  id: "api",
  name: "Inventory API",
  repository_id: fixture.repository_id,
  path: ".",
  kind: "api",
  backend: "pyfly",
  frontend: "none",
  verification: [],
  depends_on: [],
  responsibility: "Keep stock reservations consistent",
};
const project = {
  id: fixture.project_id,
  definition: {
    name: "Warehouse inventory",
    description: "A controlled Studio fixture for saved development progress.",
    topology: "monorepo",
    owners: ["Product team"],
    components: [component],
  },
  revision: 1,
  digest: "d".repeat(64),
  created_at: "2026-10-23T18:00:00Z",
  updated_at: "2026-10-23T18:00:00Z",
};

async function openProject(
  page: Page,
  options: {
    configure?: boolean;
    publish?: boolean;
    supported?: boolean;
    initialEnabled?: boolean;
    save?: "hold" | "lost" | "stale" | "foreign";
  } = {},
) {
  let writes = 0,
    reads = 0,
    refuseRead = false;
  let current = structuredClone(
    options.initialEnabled ? fixture.policy_enabled : fixture.policy_default,
  );
  current.permissions.configure = options.configure ?? true;
  current.permissions.publish = options.publish ?? true;
  current.remote_capabilities[0].draft_updates = options.supported ?? true;
  if (options.supported === false)
    current.remote_capabilities[0].reason =
      "This provider supports local checkpoints only." as never;
  let savedBody: Record<string, unknown> | null = null;
  let releaseSave: (() => void) | null = null;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockFactoryDashboard(route)) return;
    if (await mockIdentity(route)) return;
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/repositories")) {
      json = [
        {
          id: fixture.repository_id,
          name: "Inventory API",
          provider: "gitlab",
          connection_provider: "gitlab",
          remote_repository_id: "71",
          url: "https://gitlab.example.test/warehouse/inventory",
          default_branch: "main",
        },
      ];
    } else if (path.endsWith("/projects")) json = [project];
    else if (path.endsWith(`/projects/${project.id}`)) {
      json = {
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
      };
    } else if (path.endsWith("/git-delivery-policy")) {
      if (request.method() !== "GET") {
        writes++;
        savedBody = request.postDataJSON();
        if (options.save === "hold")
          await new Promise<void>((resolve) => {
            releaseSave = resolve;
          });
        if (options.save === "lost") {
          await route.abort("failed");
          return;
        }
        if (options.save === "stale") {
          await route.fulfill({
            status: 409,
            json: { detail: "Saved policy changed" },
          });
          return;
        }
        const body = savedBody as {
          policy: typeof current.policy;
          request_id: string;
        };
        current = {
          ...current,
          policy: body.policy,
          request_id: body.request_id as never,
          digest: "f".repeat(64) as never,
        };
        json =
          options.save === "foreign"
            ? { ...current, project_id: "9".repeat(32) }
            : current;
        await route.fulfill({ json });
        return;
      } else {
        reads++;
        if (refuseRead) {
          await route.fulfill({
            status: 403,
            json: { detail: "Current read permission was revoked" },
          });
          return;
        }
      }
      json = current;
    } else if (
      ["/runs", "/providers", "/deployment-targets", "/analyses"].some(
        (suffix) => path.endsWith(suffix),
      )
    )
      json = [];
    else if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    else if (path.endsWith("/skills") || path.endsWith("/policies"))
      json = { skills: [] };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("controlled-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product projects");
  await expect(
    page.getByRole("heading", { name: project.definition.name, exact: true }),
  ).toBeVisible();
  return {
    writes: () => writes,
    reads: () => reads,
    body: () => savedBody,
    release: () => releaseSave?.(),
    revokeRead: () => {
      refuseRead = true;
    },
  };
}

test("existing projects keep intermediate progress disabled until a reviewed save", async ({
  page,
}) => {
  const state = await openProject(page);
  const panel = page.getByRole("region", {
    name: "Development workflow",
    exact: true,
  });
  await expect(
    panel.getByRole("heading", { name: "Development workflow", exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByLabel("Save development checkpoints", { exact: true }),
  ).toBeEnabled();
  await expect(
    panel.getByLabel("Save development checkpoints", { exact: true }),
  ).not.toBeChecked();
  await expect(
    panel.getByRole("button", { name: "Save workflow", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(0);
  expect(state.reads()).toBeGreaterThanOrEqual(1);
  expect(state.reads()).toBeLessThanOrEqual(2); // React StrictMode verifies effect cleanup with an extra read.
});

test("a current read permission failure clears editable workflow evidence", async ({
  page,
}) => {
  const state = await openProject(page);
  const panel = page.getByRole("region", {
    name: "Development workflow",
    exact: true,
  });
  const enabled = panel.getByLabel("Save development checkpoints", {
    exact: true,
  });
  await expect(enabled).toBeEnabled();
  state.revokeRead();
  await panel
    .getByRole("button", { name: "Refresh workflow", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toContainText(
    "could not be read and verified",
  );
  await expect(enabled).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "Save workflow", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(0);
});

test("saving waits for a scoped native acknowledgment and does not repeat the write", async ({
  page,
}) => {
  const state = await openProject(page, { save: "hold" });
  const panel = page.getByRole("region", {
    name: "Development workflow",
    exact: true,
  });
  await panel
    .getByLabel("Save development checkpoints", { exact: true })
    .check();
  await panel
    .getByLabel("Completed implementation rounds between checkpoints")
    .fill("2");
  await panel.getByLabel("Where to save progress").selectOption("draft_pr");
  await panel
    .getByRole("button", { name: "Save workflow", exact: true })
    .click();
  await expect.poll(state.writes).toBe(1);
  await expect(
    panel.getByRole("button", { name: "Save workflow", exact: true }),
  ).toBeDisabled();
  await expect(panel.getByRole("status")).toHaveCount(0);
  expect(state.body()).toEqual({
    version: 1,
    request_id: expect.stringMatching(/^[a-f0-9]{32}$/),
    expected_previous_digest: null,
    policy: {
      version: 1,
      enabled: true,
      every_units: 2,
      on_pause: true,
      remote: "draft_pr",
      max_checkpoints: 50,
    },
  });
  state.release();
  await expect(panel.getByRole("status")).toContainText("saved for new runs");
  await expect(
    panel.getByRole("button", { name: "Save workflow", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(1);
});

for (const mode of ["lost", "stale", "foreign"] as const)
  test(`${mode} policy outcome is read back without automatic write retry`, async ({
    page,
  }) => {
    const state = await openProject(page, { save: mode });
    const panel = page.getByRole("region", {
      name: "Development workflow",
      exact: true,
    });
    await panel
      .getByLabel("Save development checkpoints", { exact: true })
      .check();
    await panel
      .getByRole("button", { name: "Save workflow", exact: true })
      .click();
    const message =
      mode === "stale" ? panel.getByRole("alert") : panel.getByRole("status");
    await expect(message).toContainText(
      mode === "stale" ? "server declined" : "outcome was not confirmed",
    );
    await expect.poll(state.reads).toBeGreaterThanOrEqual(2);
    expect(state.writes()).toBe(1);
  });

for (const options of [
  { configure: false },
  { publish: false },
  { supported: false },
])
  test(`current server permissions constrain workflow ${JSON.stringify(options)}`, async ({
    page,
  }) => {
    const state = await openProject(page, options);
    const panel = page.getByRole("region", {
      name: "Development workflow",
      exact: true,
    });
    const enabled = panel.getByLabel("Save development checkpoints", {
      exact: true,
    });
    if (options.configure === false) {
      await expect(enabled).toBeDisabled();
      await expect(panel).toContainText("factory administrator");
    } else {
      await enabled.check();
      await expect(panel.locator('option[value="draft_pr"]')).toBeDisabled();
      await expect(
        panel.getByRole("button", { name: "Save workflow", exact: true }),
      ).toBeEnabled();
    }
    expect(state.writes()).toBe(0);
  });

async function openRun(
  page: Page,
  remote: keyof typeof fixture.deliveries = "draft",
  malformed = false,
  configured = true,
) {
  let writes = 0,
    reads = 0;
  const run = {
    id: fixture.run_id,
    repository_id: fixture.repository_id,
    project_id: fixture.project_id,
    ...(configured
      ? { git_delivery_policy: fixture.policy_enabled.policy }
      : {}),
    brief: "Save inventory progress for review",
    status: "paused",
    agent: "agentic",
    operation: "build",
    publish: false,
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
      return;
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let json: unknown;
    if (path.endsWith("/git-deliveries")) {
      reads++;
      if (request.method() !== "GET") writes++;
      json = malformed
        ? { ...fixture.deliveries[remote], run_id: "9".repeat(32) }
        : fixture.deliveries[remote];
    } else if (path.endsWith("/runs")) json = [run];
    else if (path.endsWith(`/runs/${run.id}`)) json = run;
    else if (path.endsWith("/plan"))
      json = {
        revision: 1,
        tasks: [
          {
            id: "implement",
            title: "Develop inventory reservations",
            instructions:
              "Preserve existing reservations and record a safe checkpoint.",
            attempt: 1,
            status: "done",
          },
        ],
      };
    else if (path.endsWith("/journal")) json = { valid: true, entries: [] };
    else {
      await route.fallback();
      return;
    }
    await route.fulfill({ json });
  });
  await page.reload();
  await expectAuthenticated(page);
  await navigate(page, "Factory");
  await openFactoryRun(page, /Save inventory progress for review/);
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
  return { writes: () => writes, reads: () => reads };
}

test("run history refuses a foreign run receipt rather than presenting its commit", async ({
  page,
}) => {
  await openProject(page);
  const state = await openRun(page, "draft", true);
  const panel = page.getByRole("region", {
    name: "Saved development progress",
    exact: true,
  });
  await expect(panel.getByRole("alert")).toContainText(
    "could not be read and verified",
  );
  await expect(panel.getByText("b".repeat(40), { exact: true })).toHaveCount(0);
  expect(state.writes()).toBe(0);
});

test("an unknown remote outcome remains readable without retry or final readiness claims", async ({
  page,
}) => {
  await openProject(page);
  const state = await openRun(page, "unknown");
  const panel = page.getByRole("region", {
    name: "Saved development progress",
    exact: true,
  });
  await expect(
    panel.getByText("Remote outcome needs review", { exact: true }),
  ).toBeVisible();
  await expect(panel).toContainText("replacement push is not sent");
  await expect(panel.getByRole("button")).toHaveCount(0);
  await expect(
    panel.getByText("Review marked ready", { exact: true }),
  ).toHaveCount(0);
  expect(state.writes()).toBe(0);
});

for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`actual fixture development workflow frames ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await openProject(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const settings = page.getByRole("region", {
        name: "Development workflow",
        exact: true,
      });
      await settings
        .getByLabel("Save development checkpoints", { exact: true })
        .check();
      await settings
        .getByLabel("Where to save progress")
        .selectOption("draft_pr");
      await expect(settings).toContainText(
        "Includes the final verified checkpoint. Choose at least 2 for intermediate progress.",
      );
      await settings.scrollIntoViewIfNeeded();
      const settingsMetrics = await settings.evaluate((element) => ({
        width: element.getBoundingClientRect().width,
        scheduleGap:
          element.querySelector("legend")!.getBoundingClientRect().top -
          element
            .querySelector(".git-delivery-activation")!
            .getBoundingClientRect().bottom,
        controls: [
          ...element.querySelectorAll(
            "input:not([type=checkbox]),select,button",
          ),
        ].map((node) => ({
          label: node.getAttribute("aria-label") || node.id || node.textContent,
          height: node.getBoundingClientRect().height,
          font: parseFloat(getComputedStyle(node).fontSize),
        })),
        overflow: document.documentElement.scrollWidth > window.innerWidth,
        selectedOptionFits: (() => {
          const select = element.querySelector("select")!;
          const canvas = document.createElement("canvas");
          const context = canvas.getContext("2d")!;
          const style = getComputedStyle(select);
          context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
          return (
            context.measureText(select.selectedOptions[0].textContent ?? "")
              .width <=
            select.clientWidth - 48
          );
        })(),
      }));
      expect(settingsMetrics.scheduleGap).toBeLessThanOrEqual(
        width === 390 ? 40 : 32,
      );
      expect(settingsMetrics.selectedOptionFits).toBe(true);
      expect(settingsMetrics.overflow).toBe(false);
      for (const control of settingsMetrics.controls) {
        expect(control.height).toBeGreaterThanOrEqual(44);
        expect(control.font).toBeGreaterThanOrEqual(14);
      }
      const captureOptions = {
        producer: "studio/tests/development-workflow.spec.ts",
        resetFields: false,
      };
      const device = width === 390 ? "mobile" : "desktop";
      await captureWalkthrough(
        page,
        "development-workflow-settings",
        theme,
        device,
        { ...captureOptions, actionSelector: ".git-delivery-settings h3" },
      );
      await page.screenshot({
        path: info.outputPath(`workflow-${width}-${theme}.png`),
        fullPage: true,
      });
      await settings
        .getByRole("button", { name: "Save workflow", exact: true })
        .focus();
      await expect(
        settings.getByRole("button", { name: "Save workflow", exact: true }),
      ).toBeFocused();
      const action = settings.getByRole("button", {
        name: "Save workflow",
        exact: true,
      });
      await action.scrollIntoViewIfNeeded();
      const actionGeometry = await action.evaluate((element) => {
        const bounds = element.getBoundingClientRect();
        const center = document.elementFromPoint(
          bounds.x + bounds.width / 2,
          bounds.y + bounds.height / 2,
        );
        return {
          visible: bounds.top >= 0 && bounds.bottom <= innerHeight,
          unobscured:
            element === center || (!!center && element.contains(center)),
        };
      });
      expect(actionGeometry).toEqual({ visible: true, unobscured: true });
      await captureWalkthrough(
        page,
        "development-workflow-actions",
        theme,
        device,
        {
          ...captureOptions,
          actionSelector: ".git-delivery-settings .primary",
        },
      );
      await page.screenshot({
        path: info.outputPath(`workflow-actions-${width}-${theme}.png`),
        fullPage: true,
      });
      const state = await openRun(page);
      const history = page.getByRole("region", {
        name: "Saved development progress",
        exact: true,
      });
      await expect(
        history.getByText("Draft review updated", { exact: true }),
      ).toBeVisible();
      await history.scrollIntoViewIfNeeded();
      await expect(
        history.getByText("b".repeat(40), { exact: true }),
      ).toBeVisible();
      await expect(
        history.getByText("c".repeat(40), { exact: true }),
      ).toBeVisible();
      await expect(history).toContainText(
        "Final checks, approval and run completion are recorded separately",
      );
      const historyMetrics = await history.evaluate((element) => ({
        overflow: document.documentElement.scrollWidth > window.innerWidth,
        codes: [...element.querySelectorAll("code")].map((node) => ({
          text: node.textContent,
          font: parseFloat(getComputedStyle(node).fontSize),
          width: node.getBoundingClientRect().width,
          parentWidth: node.parentElement!.getBoundingClientRect().width,
        })),
        linkHeight: element.querySelector("a")!.getBoundingClientRect().height,
        font: getComputedStyle(element).fontFamily,
      }));
      expect(historyMetrics.overflow).toBe(false);
      expect(historyMetrics.codes).toHaveLength(2);
      expect(historyMetrics.linkHeight).toBeGreaterThanOrEqual(44);
      for (const code of historyMetrics.codes)
        expect(code.font).toBeGreaterThanOrEqual(13);
      expect(state.writes()).toBe(0);
      await captureWalkthrough(
        page,
        "development-workflow-history",
        theme,
        device,
        {
          ...captureOptions,
          actionSelector: ".git-delivery-history .git-delivery-link",
        },
      );
      await page.screenshot({
        path: info.outputPath(`checkpoint-${width}-${theme}.png`),
        fullPage: true,
      });
      const metricsPath = info.outputPath("measured-layout.json");
      await writeFile(
        metricsPath,
        JSON.stringify(
          {
            settings: settingsMetrics,
            action: actionGeometry,
            history: historyMetrics,
          },
          null,
          2,
        ),
      );
      await info.attach("measured-layout.json", {
        path: metricsPath,
        contentType: "application/json",
      });
    });

test("a configuration administrator can turn off existing draft delivery after release permission is removed", async ({
  page,
}) => {
  const state = await openProject(page, {
    initialEnabled: true,
    publish: false,
  });
  const panel = page.getByRole("region", {
    name: "Development workflow",
    exact: true,
  });
  const enabled = panel.getByLabel("Save development checkpoints", {
    exact: true,
  });
  await expect(enabled).toBeChecked();
  await enabled.uncheck();
  const save = panel.getByRole("button", {
    name: "Save workflow",
    exact: true,
  });
  await expect(save).toBeEnabled();
  await save.click();
  await expect(panel.getByRole("status")).toContainText(
    "Checkpoints turned off",
  );
  expect(state.body()).toMatchObject({
    expected_previous_digest: "a".repeat(64),
    policy: { enabled: false, remote: "draft_pr" },
  });
  expect(state.writes()).toBe(1);
});

test("legacy runs without a native checkpoint marker do not fetch the new history endpoint", async ({
  page,
}) => {
  await openProject(page);
  const state = await openRun(page, "draft", false, false);
  await expect(
    page.getByRole("heading", { name: "Development plan", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", {
      name: "Saved development progress",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.reads()).toBe(0);
  expect(state.writes()).toBe(0);
});
