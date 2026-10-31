/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mockFactoryDashboard } from "./factory-dashboard-fixture";
import { expect, test, type Page } from "@playwright/test";
import { navigate, expectAuthenticated } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";

const repository = {
  id: "a".repeat(32),
  name: "Inventory API",
  url: "https://github.com/example/inventory",
  provider: "github",
  default_branch: "main",
};
async function fixture(
  page: Page,
  options: {
    uncertain?: boolean;
    pool?: boolean;
    admin?: boolean;
  } = {},
) {
  let saved = {
    repository_id: repository.id,
    revision: "b".repeat(32),
    source: "repository",
    sandbox: options.pool
      ? { mode: "pool" }
      : {
          mode: "host",
          timeout: 300,
          max_output_bytes: 1000000,
          command_limit: 22,
          process_monitor: false,
        },
  };
  const writes: Record<string, unknown>[] = [];
  let changed = false;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockFactoryDashboard(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: { mode: "local_bootstrap", issuer: "", cookie_secure: false },
      });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name: "Local operator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: options.admin !== false,
          memberships: [],
          roles: {},
        },
      });
    if (path.endsWith("/runner/readiness"))
      return route.fulfill({
        json: { ...saved, execution: { available: true }, tools: [] },
      });
    if (path.endsWith("/runner")) {
      if (req.method() === "PUT") {
        const body = req.postDataJSON();
        writes.push(body);
        saved = { ...saved, revision: "c".repeat(32), sandbox: body.sandbox };
        if (options.uncertain) return route.abort("failed");
      } else if (changed) saved = { ...saved, revision: "d".repeat(32) };
      return route.fulfill({ json: saved });
    }
    if (path.endsWith("/repositories"))
      return route.fulfill({ json: [repository] });
    return route.fulfill({
      json: [
        "/runs",
        "/providers",
        "/projects",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((s) => path.endsWith(s))
        ? []
        : {},
    });
  });
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  if (options.admin !== false) await navigate(page, "Administration");
  else await expectAuthenticated(page);
  return {
    writes,
    changeRevision: () => {
      changed = true;
    },
  };
}
async function open(page: Page) {
  await page
    .locator("main")
    .getByRole("button", { name: "Runners & processes", exact: true })
    .click();
  await page
    .getByLabel("Repository", { exact: true })
    .selectOption(repository.id);
  await expect(page.getByLabel("Execution environment")).toHaveValue("host");
  await expect(
    page.getByRole("checkbox", { name: "Set a work deadline", exact: true }),
  ).not.toBeChecked();
}
async function next(page: Page) {
  await page.getByRole("button", { name: "Continue", exact: true }).click();
}
for (const view of [
  { width: 1440, height: 900, theme: "light" },
  { width: 1024, height: 640, theme: "dark" },
  { width: 390, height: 844, theme: "light" },
]) {
  test(`runner setup saves with readable layout ${view.width} ${view.theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize(view);
    await page.emulateMedia({ colorScheme: view.theme as "light" | "dark" });
    const { writes } = await fixture(page);
    await open(page);
    await next(page);
    await page
      .getByRole("checkbox", { name: /Enable process monitoring/ })
      .check();
    await expect(
      page.getByRole("heading", { name: "What can agents wait for?" }),
    ).toBeVisible();
    await page.screenshot({
      path: info.outputPath("runner-monitor-processes.png"),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const action = page.getByRole("button", { name: "Continue", exact: true });
    await action.scrollIntoViewIfNeeded();
    const rect = await action.boundingBox(),
      footer = await page.locator(".workspace-main > footer").boundingBox();
    if (rect && footer)
      expect(rect.y + rect.height).toBeLessThanOrEqual(footer.y + 1);
    await next(page);
    await page
      .getByRole("button", { name: "Save runner settings", exact: true })
      .click();
    await expect(
      page.getByText("Runner settings saved for future runs.", {
        exact: false,
      }),
    ).toBeVisible();
    expect(writes).toHaveLength(1);
    expect(writes[0].sandbox).toMatchObject({
      mode: "host",
      process_monitor: true,
      command_limit: 22,
      work_duration: {
        protocol: "forge-work-duration-v2",
        timeout_seconds: null,
      },
    });
    await page
      .getByRole("button", { name: "Check availability", exact: true })
      .click();
    await expect(
      page.getByText("The worker can use this runner.", { exact: false }),
    ).toBeVisible();
  });
}
test("an optional long deadline can be saved and removed without changing legacy controls", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  await open(page);
  await page
    .getByRole("checkbox", { name: "Set a work deadline", exact: true })
    .check();
  await page.getByLabel("Stop after (seconds)").fill("864000");
  await next(page);
  await next(page);
  await expect(
    page.getByText("After 864,000 seconds", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save runner settings", exact: true })
    .click();
  await expect(
    page.getByText("Runner settings saved for future runs.", { exact: false }),
  ).toBeVisible();
  expect(writes[0].sandbox).toMatchObject({
    timeout: 300,
    command_limit: 22,
    work_duration: {
      protocol: "forge-work-duration-v2",
      timeout_seconds: 864000,
    },
  });
  await page
    .getByRole("button", { name: "Reload saved settings", exact: true })
    .click();
  await expect(page.getByLabel("Stop after (seconds)")).toHaveValue("864000");
  await page
    .getByRole("checkbox", { name: "Set a work deadline", exact: true })
    .uncheck();
  await next(page);
  await next(page);
  await expect(
    page.getByText("Until completion or cancellation", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save runner settings", exact: true })
    .click();
  await expect(
    page.getByText("Runner settings saved for future runs.", { exact: false }),
  ).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1].sandbox).toMatchObject({
    timeout: 300,
    command_limit: 22,
    work_duration: {
      protocol: "forge-work-duration-v2",
      timeout_seconds: null,
    },
  });
});

test("uncertain save only permits reload, without a repeated PUT", async ({
  page,
}) => {
  const { writes } = await fixture(page, { uncertain: true });
  await open(page);
  await next(page);
  await next(page);
  await page
    .getByRole("button", { name: "Save runner settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("save result is unknown");
  await expect(
    page.getByRole("button", { name: "Save runner settings", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Reload saved settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(writes).toHaveLength(1);
});
test("changed runner revision prevents a stale write", async ({ page }) => {
  const { writes, changeRevision } = await fixture(page);
  await open(page);
  await next(page);
  await next(page);
  changeRevision();
  await page
    .getByRole("button", { name: "Save runner settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("settings changed");
  expect(writes).toHaveLength(0);
});
test("saved pool remains read-only and is never replaced", async ({ page }) => {
  const { writes } = await fixture(page, { pool: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Runners & processes", exact: true })
    .click();
  await page
    .getByLabel("Repository", { exact: true })
    .selectOption(repository.id);
  await expect(
    page.getByRole("heading", { name: "Managed pool runner", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save runner settings", exact: true }),
  ).toHaveCount(0);
  expect(writes).toHaveLength(0);
});
test("administration does not expose runner configuration to nonadministrators", async ({
  page,
}) => {
  await fixture(page, { admin: false });
  await expect(
    page.getByRole("button", { name: "Administration", exact: true }),
  ).toHaveCount(0);
  await expect(
    page
      .locator("main")
      .getByRole("button", { name: "Runners & processes", exact: true }),
  ).toHaveCount(0);
});

for (const theme of ["light", "dark"] as const) {
  for (const device of ["desktop", "mobile"] as const) {
    test(`guided runner process setup frames ${device} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      await page.emulateMedia({ colorScheme: theme });
      await fixture(page);
      await open(page);
      const capture = (stage: string) =>
        captureWalkthrough(page, stage, theme, device, {
          producer: "studio/tests/runner-setup.spec.ts",
          ...(device === "mobile" && stage !== "runner-setup-review"
            ? { framing: "initial" as const }
            : {}),
        });
      await capture("runner-setup-execution");
      await next(page);
      await page
        .getByRole("checkbox", { name: /Enable process monitoring/ })
        .check();
      await capture("runner-setup-processes");
      await next(page);
      await capture("runner-setup-review");
    });
  }
}
