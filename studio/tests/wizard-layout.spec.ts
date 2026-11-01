/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { mockFactoryDashboard } from "./factory-dashboard-fixture";
for (const scale of [1, 1.25, 2]) {
  test(`wizard actions stay above the footer and labels remain readable at ${scale}× scale`, async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: {
        width: Math.floor(1440 / scale),
        height: Math.floor(768 / scale),
      },
      deviceScaleFactor: scale,
    });
    const page = await context.newPage();
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      if (
        route.request().method() === "GET" &&
        (await mockFactoryDashboard(route, { projects: [], runs: [] }))
      )
        return;
      const path = new URL(route.request().url()).pathname;
      const json = path.endsWith("/repositories")
        ? [
            {
              id: "repo-1",
              name: "Inventory",
              provider: "git",
              url: "file:///inventory",
              default_branch: "main",
            },
          ]
        : path.endsWith("/product") || path.endsWith("/brand")
          ? null
          : path.endsWith("/runs") ||
              path.endsWith("/providers") ||
              path.endsWith("/assistant/conversations") ||
              path.endsWith("/deployment-targets")
            ? []
            : {};
      await route.fulfill({ json });
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("test-operator-token-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    for (const theme of ["light", "dark"]) {
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Product definition");
      await page.getByLabel("Customer or user").fill("Inventory operators");
      await page
        .getByLabel("Problem to solve")
        .fill("Prevent inventory over-reservation.");
      await page.getByLabel("Product owners").fill("Inventory team");
      const next = page.getByRole("button", { name: "Continue", exact: true });
      await next.scrollIntoViewIfNeeded();
      const geometry = await next.evaluate((button) => {
        const action = button.getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main>footer")!
            .getBoundingClientRect();
        return {
          bottom: action.bottom,
          top: action.top,
          footer: footer.top,
          width: action.width,
          scrollWidth: button.scrollWidth,
          clientWidth: button.clientWidth,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.footer - 8);
      expect(geometry.top).toBeGreaterThanOrEqual(0);
      expect(geometry.scrollWidth).toBeLessThanOrEqual(
        geometry.clientWidth + 1,
      );
      expect(geometry.overflow).toBe(false);
      if (scale === 2)
        await page.screenshot({
          path: test.info().outputPath(`zoom-${theme}.png`),
        });
      await next.click();
      await expect(
        page.getByRole("heading", {
          name: "What does the customer need to do?",
          exact: true,
        }),
      ).toBeFocused();
      await page.getByRole("button", { name: "Back", exact: true }).click();
      await expect(page.getByLabel("Customer or user")).toHaveValue(
        "Inventory operators",
      );
      await navigate(page, "Factory");
    }
    await context.close();
  });
}
