/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

for (const width of [1440, 390]) {
  for (const theme of ["light", "dark"]) {
    test(`Pages navigation and screenshots stay in the repository path at ${width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      const failures = [];
      page.on("response", (response) => {
        if (
          new URL(response.url()).hostname === "127.0.0.1" &&
          response.status() >= 400
        )
          failures.push(response.url());
      });
      await page.goto("./console-tour/");
      await expect(page.locator('link[rel="canonical"]')).toHaveAttribute(
        "href",
        "https://fireflyframework.github.io/firefly-forge/console-tour/",
      );
      await expect(page.locator("body")).toHaveAttribute(
        "data-md-color-scheme",
        theme === "dark" ? "slate" : "default",
      );
      const figure = page.locator(".forge-shot__link:visible").first();
      await expect(figure.locator("img")).toBeVisible();
      const imageUrl = await figure.evaluate((link) => link.href);
      expect(new URL(imageUrl).pathname).toMatch(
        /^\/firefly-forge\/assets\/screenshots\//,
      );
      await figure.click();
      await expect(page).toHaveURL(imageUrl);
      await page.goBack();
      await page
        .locator('a[href="../knowledge/#initialize-the-managed-service"]')
        .click();
      await expect(page).toHaveURL(
        /\/firefly-forge\/knowledge\/#initialize-the-managed-service$/,
      );
      await expect(page.locator("h1")).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(failures).toEqual([]);
    });
  }
}

test("Pages search resolves results under the repository path", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("./");
  await page.locator('.md-header__button[for="__search"]').click();
  await page.getByPlaceholder("Search").pressSequentially("unresolved intent");
  const result = page.locator(".md-search-result__link").first();
  await expect(result).toBeVisible();
  expect(new URL(await result.evaluate((link) => link.href)).pathname).toMatch(
    /^\/firefly-forge\//,
  );
  await result.click();
  await expect(page).toHaveURL(/\/firefly-forge\//);
  await expect(page.locator("h1")).toBeVisible();
});
