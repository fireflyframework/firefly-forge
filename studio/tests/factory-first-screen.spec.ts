/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

test.use({ launchOptions: { args: ["--enable-unsafe-swiftshader"] } });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
])
  for (const theme of ["light", "dark"]) {
    test(`the whole production scene fits its first screen ${viewport.width} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      const writes: string[] = [];
      const repository = {
        id: "fixture-factory-repo",
        name: "Fixture Inventory API",
        provider: "git",
        url: "/fixture/inventory",
        default_branch: "main",
      };
      const run = {
        id: "fixture-factory-run",
        repository_id: repository.id,
        brief: "Fixture inventory reservation",
        status: "developing",
        agent: "codex",
        frontend: "none",
      };
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        if (request.method() !== "GET")
          writes.push(request.method() + " " + path);
        const json = path.endsWith("/repositories")
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
              ? {
                  tasks: [
                    {
                      id: "prepare",
                      status: "done",
                      instructions: "Fixture source was inspected.",
                    },
                    {
                      id: "implement",
                      status: "running",
                      instructions:
                        "Fixture implementation is recorded as active.",
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
                }
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
      await expect(
        page.getByRole("button", { name: "API connected" }),
      ).toBeVisible();
      await navigate(page, "Factory");
      await page
        .getByRole("combobox", { name: "Appearance", exact: true })
        .selectOption(theme);
      await page.evaluate(() => document.fonts.ready);
      const scene = page.getByRole("figure", { name: "Operational factory" });
      await expect(scene).toHaveAttribute("data-renderer", "webgl");
      await expect(page.getByLabel("Project", { exact: true })).toHaveValue(
        repository.id,
      );
      await expect(
        page.getByLabel("Manufacturing run", { exact: true }),
      ).toHaveValue(run.id);
      await expect(page.locator(".production-status")).toContainText(
        "1/4 tasks recorded",
      );
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
          controls: rect(".production-filters"),
          explanation: rect(".production-explanation"),
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
      const stages = page.getByLabel("Recorded execution stages");
      await expect(stages.getByRole("button")).toHaveCount(7);
      await stages.getByRole("button", { name: /^Test / }).click();
      await expect(
        stages.getByRole("button", { name: /^Test / }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(page.locator(".production-inspector")).toContainText(
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
        "separate recorded actions",
      );
      await page
        .getByLabel("Project", { exact: true })
        .selectOption("fixture-empty-repo");
      await expect(page.locator(".production-status")).toContainText(
        "Ready for your first run",
      );
      expect(writes).toEqual([]);
    });
  }
