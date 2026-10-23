/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
const a = "a".repeat(32),
  b = "b".repeat(32);
async function fixture(page: Page) {
  let writes = 0;
  let actor = "f".repeat(32);
  const repositories = [a, b].map((id) => ({
    id,
    name: id === a ? "Project A" : "Project B",
    url: `https://github.com/example/${id}`,
    provider: "github",
    default_branch: "main",
  }));
  const runs = [
    ["a-1", a],
    ["b-1", b],
    ["b-2", b],
  ].map(([id, repository_id]) => ({
    id,
    repository_id,
    brief: `Recorded ${id}`,
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
    if (path.endsWith("/repositories")) json = repositories;
    else if (path.endsWith("/runs")) json = runs;
    else if (path.endsWith("/plan")) {
      const id = path.split("/").at(-2);
      json = {
        tasks: [
          {
            id: `task-${id}`,
            title: `Plan ${id}`,
            status: "done",
            instructions: "Fictional task",
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
    repositories,
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
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const) {
    test(`factory preserves project, board and run after inspection at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: width === 1440 ? 960 : 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      const state = await fixture(page);
      await page.getByLabel("Project", { exact: true }).selectOption(b);
      await page.getByRole("button", { name: "Board", exact: true }).click();
      await page
        .getByRole("button", { name: "Recorded b-2", exact: true })
        .click();
      const selected = page.locator(".flow-card.selected");
      await expect(selected).toContainText("Recorded b-2");
      await selected
        .getByRole("button", { name: "Inspect run", exact: true })
        .click();
      await expect(page.locator(".run-id")).toHaveText("Run b-2");
      await navigate(page, "Factory");
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({
        path: info.outputPath("factory-return.png"),
        animations: "disabled",
      });
      await expect(page.getByLabel("Project", { exact: true })).toHaveValue(b);
      await expect(
        page.getByRole("button", { name: "Board", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(
        page.getByLabel("Manufacturing run", { exact: true }),
      ).toHaveValue("b-2");
      await expect(page.locator(".flow-card.selected")).toContainText(
        "Recorded b-2",
      );
      await expect(page.locator(".flow-board")).not.toContainText(
        "Recorded a-1",
      );
      if (width === 390) {
        const shape = await page
          .locator(".production-filters")
          .evaluate((element) => {
            const style = getComputedStyle(element);
            const inner =
              element.getBoundingClientRect().width -
              parseFloat(style.paddingLeft) -
              parseFloat(style.paddingRight);
            return {
              inner,
              controls: [...element.querySelectorAll("select")].map(
                (control) => control.getBoundingClientRect().width,
              ),
            };
          });
        for (const control of shape.controls)
          expect(control).toBeGreaterThanOrEqual(shape.inner - 2);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
      }
      if (width === 1440 && theme === "dark") {
        const inspect = page
          .locator(".flow-card.selected")
          .getByRole("button", { name: "Inspect run", exact: true });
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
      await expect(page.getByLabel("Project", { exact: true })).toHaveValue(b);
      await expect(
        page.getByRole("button", { name: "Board", exact: true }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(
        page.getByLabel("Manufacturing run", { exact: true }),
      ).toHaveValue("b-2");
      expect(state.writes()).toBe(0);
    });
  }

async function selectB(page: Page) {
  await page.getByLabel("Project", { exact: true }).selectOption(b);
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await page.getByRole("button", { name: "Recorded b-2", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Selected run tasks" }),
  ).toContainText("Plan b-2");
}
test("removed run and repository resolve only within current accessible data", async ({
  page,
}) => {
  const state = await fixture(page);
  await selectB(page);
  state.runs.splice(
    state.runs.findIndex((run) => run.id === "b-2"),
    1,
  );
  await page
    .locator(".runs-section")
    .getByRole("button", { name: "Refresh", exact: true })
    .click();
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue(b);
  await expect(
    page.getByLabel("Manufacturing run", { exact: true }),
  ).toHaveValue("b-1");
  const tasks = page.getByRole("region", { name: "Selected run tasks" });
  await expect(tasks).toContainText("Plan b-1");
  await expect(tasks).not.toContainText("Plan b-2");
  state.repositories.splice(1, 1);
  await page
    .locator(".runs-section")
    .getByRole("button", { name: "Refresh", exact: true })
    .click();
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue(a);
  await expect(
    page.getByLabel("Manufacturing run", { exact: true }),
  ).toHaveValue("a-1");
  await expect(page.locator(".flow-board")).not.toContainText("Recorded b-");
  await expect(tasks).toContainText("Plan a-1");
  await expect(tasks).not.toContainText("Plan b-");
  expect(state.writes()).toBe(0);
});
test("sign-out and reconnect clear the previous factory selection", async ({
  page,
}) => {
  const state = await fixture(page);
  await selectB(page);
  await page
    .getByRole("button", { name: "Signed in as Test operator", exact: true })
    .click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByLabel("Project", { exact: true })).toHaveCount(0);
  await signIn(page);
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue(a);
  await expect(
    page.getByRole("button", { name: "Floor", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByLabel("Manufacturing run", { exact: true }),
  ).toHaveValue("a-1");
  expect(state.writes()).toBe(0);
});
test("actor and API address changes clear selection and loaded plan", async ({
  page,
}) => {
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
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue(a);
  await expect(
    page.getByRole("button", { name: "Floor", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByLabel("Manufacturing run", { exact: true }),
  ).toHaveValue("a-1");
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
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue(a);
  await expect(
    page.getByRole("button", { name: "Floor", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByLabel("Manufacturing run", { exact: true }),
  ).toHaveValue("a-1");
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Selected run tasks" }),
  ).toContainText("Plan a-1");
  await expect(
    page.getByRole("region", { name: "Selected run tasks" }),
  ).not.toContainText("Plan b-2");
  expect(state.writes()).toBe(0);
});
