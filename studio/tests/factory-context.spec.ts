/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated, openAccount } from "./navigation";
import { factoryDashboardFixture } from "./factory-dashboard-fixture";
const a = "a".repeat(32),
  b = "b".repeat(32);
const runA = "1".repeat(32),
  runB1 = "2".repeat(32),
  runB2 = "3".repeat(32);
async function fixture(page: Page) {
  let writes = 0;
  let actor = "f".repeat(32);
  const projects = [a, b].map((id) => ({
    id,
    name: id === a ? "Project A" : "Project B",
    component_count: 2,
    repository_count: id === a ? 1 : 2,
  }));
  const repositories = ["4".repeat(32), "5".repeat(32)].map((id) => ({
    id,
    name:
      id === "4".repeat(32)
        ? "Shared component repository"
        : "Second component repository",
    url: `https://github.com/example/${id}`,
    provider: "github",
    default_branch: "main",
  }));
  const runs = [
    [runA, a, repositories[0].id, "a-1"],
    [runB1, b, repositories[0].id, "b-1"],
    [runB2, b, repositories[1].id, "b-2"],
  ].map(([id, project_id, repository_id, label]) => ({
    id,
    project_id,
    project_name: project_id === a ? "Project A" : "Project B",
    repository_id,
    brief: `Recorded ${label}`,
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "succeeded",
    changed_files: [],
    verification: [],
    artifacts: [],
    pr_url: "",
  }));
  await page.route("**/api/v1/**", async (route) => {
    const authPath = new URL(route.request().url()).pathname;
    if (actor !== "f".repeat(32) && authPath.endsWith("/auth/session")) {
      await route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: actor,
            display_name: "Other operator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: true,
          memberships: [],
          roles: {},
        },
      });
      return;
    }
    if (await mockIdentity(route)) return;
    if (route.request().method() !== "GET") writes++;
    const path = new URL(route.request().url()).pathname;
    let json: unknown = [];
    const url = new URL(route.request().url());
    const plans = Object.fromEntries(
      runs.map((run) => [
        run.id,
        {
          tasks: [
            {
              id: "prepare",
              status: "done",
              instructions: `Plan ${run.brief.replace("Recorded ", "")}`,
            },
          ],
        },
      ]),
    );
    if (
      path.endsWith("/factory-dashboard") &&
      url.searchParams.get("kind") === "run" &&
      !runs.some((run) => run.id === url.searchParams.get("run_id"))
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: "Recorded run is unavailable." },
      });
      return;
    }
    const dashboard = factoryDashboardFixture(url, { projects, runs, plans });
    if (dashboard !== undefined) json = dashboard;
    else if (path.endsWith("/repositories")) json = repositories;
    else if (path.endsWith("/runs")) json = runs;
    else if (path.endsWith("/plan")) {
      const id = path.split("/").at(-2);
      json = {
        tasks: [
          {
            id: "prepare",
            title: `Plan ${id}`,
            status: "done",
            instructions: `Plan ${runs.find((run) => run.id === id)?.brief.replace("Recorded ", "")}`,
          },
        ],
      };
    } else if (path.endsWith("/health")) json = { execution_ready: true };
    else if (runs.some((run) => path.endsWith(`/runs/${run.id}`)))
      json = runs.find((run) => path.endsWith(`/runs/${run.id}`));
    await route.fulfill({ json });
  });
  await page.goto("/");
  await signIn(page);
  return {
    writes: () => writes,
    projects,
    runs,
    switchActor: () => {
      actor = "e".repeat(32);
    },
  };
}
async function signIn(page: Page) {
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Factory");
  await expectAuthenticated(page);
}

async function chooseProject(page: Page, name: string) {
  await page.getByRole("button", { name: "Browse work", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Factory work" })
    .getByRole("button", { name: new RegExp(`^${name} `) })
    .click();
  await expect(page.locator(".factory-scope-caption h2")).toHaveText(name);
}
function card(page: Page, name: string) {
  return page
    .locator(".factory-run-record")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}
async function expectPlan(page: Page, text: string) {
  const board = page.getByRole("button", {
    name: "Show run board",
    exact: true,
  });
  const wasBoard = (await board.getAttribute("aria-pressed")) === "true";
  await page
    .getByRole("button", { name: "Show production floor", exact: true })
    .click();
  await expect(page.locator(".factory-inspection")).toContainText(text);
  if (wasBoard) await board.click();
}
async function focusRun(page: Page, name: string) {
  await card(page, name)
    .getByRole("button", { name: "Focus run", exact: true })
    .click();
  await expectPlan(page, `Plan ${name.replace("Recorded ", "")}`);
}
async function expectOverview(page: Page) {
  await expect(page.locator(".factory-scope-caption h2")).toHaveText(
    "Factory overview",
  );
  await expect(
    page.getByRole("button", { name: "Show production floor", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("Plan b-2", { exact: true })).toHaveCount(0);
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const) {
    test(`factory preserves project, board and run after inspection at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: width === 1440 ? 960 : 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      const state = await fixture(page);
      await selectB(page);
      await card(page, "Recorded b-2")
        .getByRole("button", { name: "Open run", exact: true })
        .click();
      await expect(page.locator(".run-id")).toHaveText(`Run ${runB2}`);
      await navigate(page, "Factory");
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({
        path: info.outputPath("factory-return.png"),
        animations: "disabled",
      });
      await expect(page.locator(".factory-scope-caption h2")).toHaveText(
        `Run ${runB2.slice(0, 8)}`,
      );
      await expect(
        page.getByRole("navigation", { name: "Current factory context" }),
      ).toContainText("Project B");
      await expect(
        page.getByRole("button", { name: "Show run board", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(card(page, "Recorded b-2")).toBeVisible();
      await expect(page.locator(".factory-status-board")).not.toContainText(
        "Recorded a-1",
      );
      await expectPlan(page, "Plan b-2");
      if (width === 390) {
        const shape = await page
          .locator(".production-context-dock")
          .evaluate((element) => {
            const box = element.getBoundingClientRect();
            return {
              left: box.left,
              right: box.right,
              controls: [
                ...element.querySelectorAll(
                  ":scope > button, .factory-view-toggle button",
                ),
              ].map((control) => {
                const bounds = control.getBoundingClientRect();
                return {
                  left: bounds.left,
                  right: bounds.right,
                  height: bounds.height,
                };
              }),
            };
          });
        expect(shape.controls.length).toBeGreaterThan(0);
        for (const control of shape.controls) {
          expect(control.height).toBeGreaterThanOrEqual(40);
          expect(control.left).toBeGreaterThanOrEqual(shape.left);
          expect(control.right).toBeLessThanOrEqual(shape.right);
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
      }
      if (width === 1440 && theme === "dark") {
        const inspect = card(page, "Recorded b-2").getByRole("button", {
          name: "Open run",
          exact: true,
        });
        await inspect.scrollIntoViewIfNeeded();
        const bounds = await inspect.boundingBox(),
          main = await page.locator(".workspace-main > main").boundingBox(),
          footer = await page.locator(".workspace-main > footer").boundingBox();
        expect(bounds).not.toBeNull();
        expect(main).not.toBeNull();
        expect(footer).not.toBeNull();
        expect(bounds!.y).toBeGreaterThanOrEqual(main!.y);
        expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
          Math.min(main!.y + main!.height, footer!.y),
        );
        await page.screenshot({
          path: info.outputPath("selected-run-scrolled.png"),
          animations: "disabled",
        });
      }
      await navigate(page, "Knowledge");
      await navigate(page, "Factory");
      await expect(page.locator(".factory-scope-caption h2")).toHaveText(
        `Run ${runB2.slice(0, 8)}`,
      );
      await expect(
        page.getByRole("button", { name: "Show run board", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      await expectPlan(page, "Plan b-2");
      expect(state.writes()).toBe(0);
    });
  }
async function selectB(page: Page) {
  await chooseProject(page, "Project B");
  await page
    .getByRole("button", { name: "Show run board", exact: true })
    .click();
  await focusRun(page, "Recorded b-2");
}
test("removed run and project resolve only through current accessible choices", async ({
  page,
}) => {
  const state = await fixture(page);
  await selectB(page);
  state.runs.splice(
    state.runs.findIndex((run) => run.id === runB2),
    1,
  );
  await page
    .getByRole("button", { name: "Refresh factory records", exact: true })
    .click();
  await expect(page.getByText("Plan b-2", { exact: true })).toHaveCount(0);
  await expect(card(page, "Recorded b-2")).toHaveCount(0);
  await chooseProject(page, "Project B");
  await focusRun(page, "Recorded b-1");
  state.projects.splice(
    state.projects.findIndex((project) => project.id === b),
    1,
  );
  state.runs.splice(
    state.runs.findIndex((run) => run.id === runB1),
    1,
  );
  await page
    .getByRole("button", { name: "Refresh factory records", exact: true })
    .click();
  await expect(page.getByText(/^Plan b-/)).toHaveCount(0);
  await page.getByRole("button", { name: "Browse work", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Factory work" });
  await expect(drawer.getByRole("button", { name: /^Project B / })).toHaveCount(
    0,
  );
  await drawer.getByRole("button", { name: /^Project A / }).click();
  await focusRun(page, "Recorded a-1");
  await expect(page.locator(".factory-run-record")).not.toContainText(
    "Recorded b-",
  );
  await expectPlan(page, "Plan a-1");
  expect(state.writes()).toBe(0);
});
test("sign-out and reconnect clear the previous factory selection", async ({
  page,
}) => {
  const state = await fixture(page);
  await selectB(page);
  await openAccount(page);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.locator(".production-floor")).toHaveCount(0);
  await signIn(page);
  await expectOverview(page);
  expect(state.writes()).toBe(0);
});
test("actor and API address changes clear selection and loaded plan", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const state = await fixture(page);
  await selectB(page);
  state.switchActor();
  await page.evaluate(() =>
    window.dispatchEvent(new Event("forge-session-expired")),
  );
  await expect(
    page.getByRole("button", {
      name: "Signed in as Other operator",
      exact: true,
    }),
  ).toBeVisible();
  await expectOverview(page);
  await selectB(page);
  await navigate(page, "Connections");
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  const base = new URL("/second/api/v1", page.url()).href;
  await page.getByLabel("API address", { exact: true }).fill(base);
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  const session = page.waitForResponse(`${base}/auth/session`);
  await page.getByRole("button", { name: "Load server", exact: true }).click();
  await session;
  await navigate(page, "Factory");
  await expectOverview(page);
  await chooseProject(page, "Project A");
  await focusRun(page, "Recorded a-1");
  await expect(page.getByText("Plan b-2", { exact: true })).toHaveCount(0);
  expect(state.writes()).toBe(0);
});
