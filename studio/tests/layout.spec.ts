/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";

async function alignedFooter(page: Page) {
  const geometry = await page.evaluate(() => {
    const rail = document.querySelector(".sidebar-bottom")!;
    const footer = document.querySelector(".workspace-main > footer")!;
    const left = rail.getBoundingClientRect();
    const right = footer.getBoundingClientRect();
    const leftLine = rail.querySelector("a")!.getBoundingClientRect();
    const rightLine = footer.querySelector("span")!.getBoundingClientRect();
    return {
      top: Math.abs(left.top - right.top),
      height: Math.abs(left.height - right.height),
      baseline: Math.abs(leftLine.top - rightLine.top),
      footerHeight: right.height,
      overflow: document.documentElement.scrollWidth > innerWidth,
    };
  });
  expect(geometry.top).toBeLessThanOrEqual(1);
  expect(geometry.height).toBeLessThanOrEqual(1);
  expect(geometry.baseline).toBeLessThanOrEqual(1);
  expect(geometry.footerHeight).toBe(64);
  expect(geometry.overflow).toBe(false);
}

test("desktop footer borders and text align with the rail in both themes", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    for (const width of [1440, 1280, 1024]) {
      await page.setViewportSize({ width, height: 1100 });
      await alignedFooter(page);
    }
  }
  await expect(page.locator(".sidebar-bottom")).not.toContainText(
    /Built on evidence|Built around your workspace|Local changes, visible evidence/i,
  );
  await expect(page.locator(".workspace-main > footer")).not.toContainText(
    /Local changes, visible evidence|made for local work/i,
  );
});

test("footer alignment holds at a two-times display scale with desktop CSS width", async ({
  browser,
}) => {
  const context = await browser.newContext({
    viewport: { width: 960, height: 550 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();
  await page.goto("/");
  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await alignedFooter(page);
  }
  await context.close();
});
