/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readyImages } from "./image-readiness.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

for (const width of [1440, 390, 375])
  for (const theme of ["light", "dark"])
    test(`native connected-account guide fits ${width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.goto("/connected-accounts/");
      await expect(page.locator("body")).toHaveAttribute(
        "data-md-color-scheme",
        theme === "dark" ? "slate" : "default",
      );
      const article = page.locator("article");
      await expect(article).toContainText("API-only qualification");
      await expect(article).toContainText("Prerequisite required");
      for (const summary of await article
        .locator(".forge-gallery > summary")
        .all()) {
        await summary.focus();
        await page.keyboard.press("Enter");
        await expect(summary.locator("..")).toHaveAttribute("open", "");
      }
      const images = article.locator(".forge-shot img");
      await expect(images).toHaveCount(4);
      for (const image of await images.all()) {
        await image.scrollIntoViewIfNeeded();
        await readyImages(image);
        const bounds = await image.evaluate((element) => ({
          left: element.getBoundingClientRect().left,
          right: element.getBoundingClientRect().right,
          width: element.getBoundingClientRect().width,
          naturalWidth: element.naturalWidth,
        }));
        expect(bounds.left).toBeGreaterThanOrEqual(0);
        expect(bounds.right).toBeLessThanOrEqual(width);
        expect(bounds.width).toBeLessThanOrEqual(bounds.naturalWidth);
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    });
