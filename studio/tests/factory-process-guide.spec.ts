/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { expectAuthenticated, navigate } from "./navigation";
import { factoryDashboardFixture } from "./factory-dashboard-fixture";

for (const width of [1440, 320])
  for (const theme of ["light", "dark"]) {
    test(`factory process entry points at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: width === 320 ? 568 : 900 });
      const writes: string[] = [];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          url = new URL(request.url());
        if (request.method() !== "GET") writes.push(url.pathname);
        const dashboard = factoryDashboardFixture(url, {
          projects: [],
          runs: [],
        });
        await route.fulfill({
          json:
            dashboard ??
            (url.pathname.endsWith("/health")
              ? { status: "ok", service_role: "api", admission_ready: true }
              : url.pathname.endsWith("/profiles")
                ? {}
                : []),
        });
      });
      await page.goto("/");
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fixture-process-token");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await expectAuthenticated(page);
      await navigate(page, "Factory");
      await page
        .getByRole("combobox", { name: "Appearance", exact: true })
        .selectOption(theme);
      await page.evaluate(() => document.fonts.ready);
      const guide = page.locator(".factory-process-guide");
      await expect(guide).not.toHaveAttribute("open");
      const summary = guide.locator("summary");
      await summary.focus();
      await page.keyboard.press("Enter");
      await expect(guide).toHaveAttribute("open", "");
      await expect(guide.getByRole("listitem")).toHaveCount(5);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: info.outputPath(`process-${width}-${theme}.png`),
      });
      await guide
        .getByRole("button", { name: "Product definition", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Product definition", exact: true }),
      ).toBeVisible();
      await navigate(page, "Factory");
      await page.locator(".factory-process-guide summary").click();
      await page
        .locator(".factory-process-guide")
        .getByRole("button", { name: "Product projects", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Product projects", exact: true }),
      ).toBeVisible();
      expect(writes).toEqual([]);
    });
  }
