/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { expectAuthenticated } from "./navigation";

async function signedInLayout(page: Page) {
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({
      json: path.endsWith("/auth/configuration")
        ? { mode: "oidc", issuer: "https://identity.example.test" }
        : path.endsWith("/auth/session")
          ? {
              authenticated: true,
              user: {
                id: "f".repeat(32),
                display_name: "Layout reviewer",
                email: null,
                authentication_method: "oidc_session",
              },
              global_admin: false,
              memberships: [],
              roles: {},
            }
          : [],
    });
  });
}

async function alignedFooter(page: Page) {
  const geometry = await page.evaluate(() => {
    const rail = document.querySelector(".sidebar-bottom")!;
    const footer = document.querySelector(".workspace-main > footer")!;
    const left = rail.getBoundingClientRect();
    const right = footer.getBoundingClientRect();
    const account = rail
      .querySelector(".identity-trigger")!
      .getBoundingClientRect();
    return {
      top: Math.abs(left.top - right.top),
      height: Math.abs(left.height - right.height),
      accountCenter: Math.abs(
        account.top + account.height / 2 - (right.top + right.height / 2),
      ),
      accountInside:
        account.left >= left.left &&
        account.right <= left.right &&
        account.top >= left.top &&
        account.bottom <= left.bottom,
      footerHeight: right.height,
      overflow: document.documentElement.scrollWidth > innerWidth,
    };
  });
  expect(geometry.top).toBeLessThanOrEqual(1);
  expect(geometry.height).toBeLessThanOrEqual(1);
  expect(geometry.accountCenter).toBeLessThanOrEqual(1);
  expect(geometry.accountInside).toBe(true);
  expect(geometry.footerHeight).toBe(64);
  expect(geometry.overflow).toBe(false);
}

test("desktop footer borders align with the rail and its account control is centered in both themes", async ({
  page,
}) => {
  await signedInLayout(page);
  await page.goto("/");
  await expectAuthenticated(page);
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
  await signedInLayout(page);
  await page.goto("/");
  await expectAuthenticated(page);
  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await alignedFooter(page);
  }
  await context.close();
});
