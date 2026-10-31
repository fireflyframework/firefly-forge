/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { factoryDashboardFixture } from "./factory-dashboard-fixture";
test.beforeEach(async ({ page }) => {
  const first = "a".repeat(32),
    chosen = "b".repeat(32);
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/repositories"))
      json = [
        {
          id: first,
          name: "Inventory API",
          url: "file:///inventory",
          provider: "git",
          default_branch: "main",
        },
        {
          id: chosen,
          name: "Billing API",
          url: "file:///billing",
          provider: "git",
          default_branch: "main",
        },
      ];
    if (
      [
        "/runs",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((p) => path.endsWith(p))
    )
      json = [];
    if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    if (path.endsWith("/skills")) json = { skills: [] };
    if (path.endsWith("/policies")) json = { skills: [] };
    if (path.endsWith("/health")) json = { execution_ready: true };
    const dashboard = factoryDashboardFixture(new URL(route.request().url()), {
      projects: [],
      runs: [],
    });
    await route.fulfill({ json: dashboard ?? json });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
});
const chosen = "b".repeat(32);
test("repository action keeps the chosen source and test purpose explains model-free execution", async ({
  page,
}) => {
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await nav.getByRole("button", { name: /^Repositories/ }).click();
  await page
    .locator(".repository")
    .filter({
      has: page.getByRole("heading", { name: "Billing API", exact: true }),
    })
    .getByRole("button", { name: "Start a run", exact: true })
    .click();
  await expect(
    page.getByLabel("Source repository", { exact: true }),
  ).toHaveValue(chosen);
  await page.getByLabel("Run purpose", { exact: true }).selectOption("test");
  await expect(
    page.getByText("Check existing behavior", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByText(
      "Match the current repository’s frameworks. Test runs execute commands without a coding provider.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByLabel("Execution agent", { exact: true })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(
    page.getByLabel("Source repository", { exact: true }),
  ).toHaveValue(chosen);
  await expect(page.getByLabel("Run purpose", { exact: true })).toHaveValue(
    "test",
  );
});

test("phone run and deployment wizards expose their initial action without moving the page", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const name of ["Start a run", "Deployment"]) {
    await navigate(page, name);
    await page
      .locator(".workspace-main>main")
      .evaluate((element) => (element.scrollTop = 0));
    const next = page.getByRole("button", { name: "Continue", exact: true });
    await expect
      .poll(() =>
        next.evaluate((button) =>
          (
            button.closest(".wizard-frame") as HTMLElement
          ).style.getPropertyValue("--wizard-frame-height"),
        ),
      )
      .toMatch(/^[0-9]/);
    const geometry = await next.evaluate((button) => {
      const r = button.getBoundingClientRect(),
        footer = document
          .querySelector(".workspace-main>footer")!
          .getBoundingClientRect();
      const frame = button.closest(".wizard-frame")!;
      return {
        top: r.top,
        bottom: r.bottom,
        footer: footer.top,
        layout: frame.getAttribute("data-layout"),
        frameHeight: frame.getBoundingClientRect().height,
        allocated: (frame as HTMLElement).style.getPropertyValue(
          "--wizard-frame-height",
        ),
        chrome: [".wizard-steps", ".wizard-heading", ".wizard-actions"].map(
          (selector) => ({
            selector,
            height: frame.querySelector(selector)!.getBoundingClientRect()
              .height,
          }),
        ),
        fieldsHeight: frame
          .querySelector(".wizard-field-scroll")!
          .getBoundingClientRect().height,
      };
    });
    expect(geometry.top).toBeGreaterThanOrEqual(0);
    expect(Math.round(geometry.fieldsHeight)).toBeGreaterThanOrEqual(160);
    expect(
      geometry.bottom,
      `${name}: ${JSON.stringify(geometry)}`,
    ).toBeLessThanOrEqual(geometry.footer - 8);
  }
});

test("phone menu presents full section labels and restores focus on Escape", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 844 });
  const open = page.getByRole("button", {
    name: "Open navigation",
    exact: true,
  });
  await open.click();
  await expect(
    page.getByRole("dialog", { name: "Workspace navigation" }),
  ).toBeVisible();
  const workflow = page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name: "Administration", exact: true });
  const bounds = await workflow.evaluate((element) => ({
    scroll: element.scrollWidth,
    width: element.clientWidth,
  }));
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.width + 1);
  await page.keyboard.press("Escape");
  await expect(open).toBeFocused();
  await navigate(page, "Deployment");
  await expect(page.locator(".mobile-section-label")).toHaveText("Deployment");
  await expect(
    page.getByRole("dialog", { name: "Workspace navigation" }),
  ).not.toBeVisible();
});

test("phone test result keeps checks reachable and retains the full brief without implying model execution", async ({
  page,
}) => {
  const brief =
    "Verify authorized reservations and reject overselling across warehouse boundaries. " +
    "Preserve stock when requests are invalid or unauthorized. ".repeat(8);
  const run = {
    id: "c".repeat(32),
    repository_id: chosen,
    project_id: "e".repeat(32),
    project_name: "Billing product",
    brief,
    operation: "test",
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "succeeded",
    verification: [{ argv: ["pytest"], exit_code: 0, output: "6 passed" }],
    changed_files: [],
    events: [],
  };
  await page.route("**/api/v1/runs**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({ json: path.endsWith("/runs") ? [run] : run });
  });
  await page.route("**/api/v1/factory-dashboard**", async (route) =>
    route.fulfill({
      json: factoryDashboardFixture(new URL(route.request().url()), {
        projects: [
          {
            id: run.project_id,
            name: run.project_name,
            component_count: 2,
            repository_count: 1,
          },
        ],
        runs: [run],
      }),
    }),
  );
  await navigate(page, "Factory");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Browse work", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Factory work" })
    .getByRole("button", { name: /^Billing product / })
    .click();
  await page
    .locator(".factory-run-record")
    .getByRole("button", { name: "Open run", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Verify existing behavior",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/No coding provider/)).toBeVisible();
  const verification = page.getByRole("tab", {
    name: "Verification",
    exact: true,
  });
  const box = await verification.boundingBox();
  expect(box!.y + box!.height).toBeLessThan(800);
  await verification.click();
  await expect(page.getByText("6 passed", { exact: true })).toBeVisible();
  await page.locator(".run-brief-details>summary").click();
  await expect(page.locator(".run-brief-details p")).toHaveText(brief.trim());
});
