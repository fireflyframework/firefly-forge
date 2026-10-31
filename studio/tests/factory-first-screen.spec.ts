/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import { factoryDashboardFixture } from "./factory-dashboard-fixture";

test.use({ launchOptions: { args: ["--enable-unsafe-swiftshader"] } });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
])
  for (const theme of ["light", "dark"]) {
    test(`the production scene and controls fit their first screen ${viewport.width} ${theme}`, async ({
      page,
    }, testInfo) => {
      test.setTimeout(60_000);
      await page.setViewportSize(viewport);
      const writes: string[] = [];
      const repository = {
        id: "1".repeat(32),
        name: "Fixture Inventory API",
        provider: "git",
        url: "/fixture/inventory",
        default_branch: "main",
      };
      const run = {
        id: "2".repeat(32),
        repository_id: repository.id,
        project_id: "a".repeat(32),
        project_name: "Fixture inventory product",
        brief: "Fixture inventory reservation",
        status: "developing",
        agent: "codex",
        frontend: "none",
      };
      const projects = [
        {
          id: run.project_id,
          name: run.project_name,
          component_count: 2,
          repository_count: 1,
        },
        {
          id: "b".repeat(32),
          name: "Fixture empty product",
          component_count: 1,
          repository_count: 1,
        },
      ];
      const plan = {
        tasks: [
          {
            id: "prepare",
            status: "done",
            instructions: "Fixture source was inspected.",
          },
          {
            id: "implement",
            status: "running",
            instructions: "Fixture implementation is recorded as active.",
          },
          {
            id: "verify",
            status: "queued",
            instructions: "Fixture checks have not run.",
          },
          {
            id: "checkpoint",
            status: "queued",
            instructions: "Fixture checkpoint is not recorded.",
          },
        ],
      };
      const planReads: string[] = [],
        bulkReads: string[] = [];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        if (request.method() !== "GET")
          writes.push(request.method() + " " + path);
        if (path.endsWith("/repositories") || path.endsWith("/runs"))
          bulkReads.push(path);
        if (path.endsWith("/plan")) planReads.push(path);
        const dashboard = factoryDashboardFixture(new URL(request.url()), {
          projects,
          runs: [run],
          plans: { [run.id]: plan },
        });
        const json =
          dashboard !== undefined
            ? dashboard
            : path.endsWith("/repositories")
              ? [
                  repository,
                  {
                    ...repository,
                    id: "fixture-empty-repo",
                    name: "Fixture empty project",
                  },
                ]
              : path.endsWith("/runs")
                ? [run]
                : path.endsWith("/plan")
                  ? plan
                  : path.endsWith("/health")
                    ? {
                        status: "ok",
                        service_role: "api",
                        admission_ready: true,
                        execution_ready: false,
                        execution: {
                          local_enabled: false,
                          remote_state: "unknown",
                        },
                      }
                    : path.endsWith("/profiles")
                      ? {}
                      : [];
        await route.fulfill({ json });
      });
      await page.goto("/");
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fictional-factory-token");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await expectAuthenticated(page);
      await expect(page.locator(".app")).toHaveAttribute("aria-busy", "false");
      bulkReads.length = 0;
      await navigate(page, "Factory");
      await page
        .getByRole("combobox", { name: "Appearance", exact: true })
        .selectOption(theme);
      await page.evaluate(() => document.fonts.ready);
      const scene = page.getByRole("figure", { name: "Operational factory" });
      await expect(scene).toHaveAttribute("data-renderer", "webgl");
      const startsFocused = viewport.width <= 600;
      await expect(scene).toHaveAttribute(
        "data-camera",
        startsFocused ? "selected-stage" : "overview",
      );
      await expect(
        scene.getByRole("button", {
          name: startsFocused ? "Show full floor" : "Focus selected stage",
          exact: true,
        }),
      ).toHaveAttribute("aria-pressed", String(startsFocused));
      await expect(
        page.getByRole("heading", { name: "Factory overview", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("navigation", { name: "Current factory context" }),
      ).toContainText("All projects and standalone work");
      await expect(page.locator(".factory-records-heading")).toContainText(
        "1 shown on this page",
      );
      expect(planReads).toEqual([]);
      expect(bulkReads).toEqual([]);
      await expect
        .poll(() => page.locator("main").evaluate((node) => node.scrollTop))
        .toBe(0);
      const geometry = await page.evaluate(() => {
        const rect = (selector: string) => {
          const box = document.querySelector(selector)!.getBoundingClientRect();
          return {
            top: box.top,
            bottom: box.bottom,
            left: box.left,
            right: box.right,
            width: box.width,
            height: box.height,
          };
        };
        return {
          main: rect("main"),
          footer: rect(".workspace-main > footer"),
          canvas: rect(".factory-live-viewport"),
          controls: rect(".production-context-dock"),
          explanation: rect(".factory-scope-caption"),
          pageOverflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      await testInfo.attach("first-screen-geometry", {
        body: JSON.stringify(geometry),
        contentType: "application/json",
      });
      await page.screenshot({
        path: testInfo.outputPath(
          `factory-first-screen-${viewport.width}-${theme}.png`,
        ),
      });
      for (const box of [
        geometry.controls,
        geometry.explanation,
        geometry.canvas,
      ]) {
        expect(box.top).toBeGreaterThanOrEqual(geometry.main.top);
        expect(box.bottom).toBeLessThanOrEqual(geometry.footer.top);
        expect(box.left).toBeGreaterThanOrEqual(geometry.main.left);
        expect(box.right).toBeLessThanOrEqual(geometry.main.right);
      }
      expect(geometry.canvas.height).toBeGreaterThanOrEqual(180);
      expect(geometry.pageOverflow).toBe(false);
      await page
        .locator(".factory-run-record")
        .filter({ hasText: run.brief })
        .getByRole("button", { name: "Focus run", exact: true })
        .click();
      await expect(page.locator(".factory-inspection")).toContainText(
        "Fixture source was inspected.",
      );
      expect(
        planReads.every((path) => path.endsWith(`/runs/${run.id}/plan`)),
      ).toBe(true);
      expect(planReads.length).toBeGreaterThan(0);
      const stages = page.getByLabel("Recorded execution stages");
      await expect(stages.getByRole("button")).toHaveCount(7);
      await stages.getByRole("button", { name: /^Test / }).click();
      await expect(
        stages.getByRole("button", { name: /^Test / }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(page.locator(".factory-inspection")).toContainText(
        "Fixture checks have not run.",
      );
      if (startsFocused)
        await scene
          .getByRole("button", { name: "Show full floor", exact: true })
          .click();
      const focus = scene.getByRole("button", {
        name: "Focus selected stage",
        exact: true,
      });
      await focus.click();
      await expect(scene).toHaveAttribute("data-camera", "selected-stage");
      const overview = scene.getByRole("button", {
        name: "Show full floor",
        exact: true,
      });
      await expect(overview).toHaveAttribute("aria-pressed", "true");
      expect((await overview.boundingBox())!.height).toBeGreaterThanOrEqual(40);
      await expect(
        stages.getByRole("button", { name: /^Test / }),
      ).toHaveAttribute("aria-pressed", "true");
      await page.screenshot({
        path: testInfo.outputPath(
          `factory-focused-${viewport.width}-${theme}.png`,
        ),
      });
      await overview.focus();
      await page.keyboard.press("Enter");
      await expect(scene).toHaveAttribute("data-camera", "overview");
      await expect(focus).toBeFocused();
      await expect(
        stages.getByRole("button", { name: /^Test / }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(page.locator(".factory-inspection")).toContainText(
        "Fixture checks have not run.",
      );
      await stages.getByRole("button", { name: /^Test / }).focus();
      await page.keyboard.press("ArrowRight");
      await expect(
        stages.getByRole("button", { name: /^Checkpoint / }),
      ).toBeFocused();
      await page.locator(".production-guide summary").focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(".production-guide")).toHaveAttribute(
        "open",
        "",
      );
      await expect(page.locator(".production-guide")).toContainText(
        "require separate recorded actions",
      );
      await page
        .getByRole("button", { name: "Browse work", exact: true })
        .click();
      await page
        .getByRole("dialog", { name: "Factory work" })
        .getByRole("button", { name: /^Fixture empty product / })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Fixture empty product",
          exact: true,
        }),
      ).toBeVisible();
      await expect(page.locator(".factory-empty")).toContainText(
        "No recorded runs are visible in this scope",
      );
      await expect(page.locator(".factory-run-record")).toHaveCount(0);
      expect(writes).toEqual([]);
    });
  }
