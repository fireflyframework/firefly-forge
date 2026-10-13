/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readyPictureImage } from "./image-readiness.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

for (const width of [1440, 375])
  for (const theme of ["light", "dark"]) {
    test(`low-level architecture diagrams fit ${width}px ${theme} documentation`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.goto("/architecture/");
      await expect(page.locator("body")).toHaveAttribute(
        "data-md-color-scheme",
        theme === "dark" ? "slate" : "default",
      );
      const figures = page.locator(".forge-architecture-diagram");
      await expect(figures).toHaveCount(8);
      for (const summary of await page
        .locator(".forge-gallery > summary")
        .all()) {
        await summary.focus();
        await page.keyboard.press("Enter");
        await expect(summary.locator("..")).toHaveAttribute("open", "");
      }
      for (const figure of await figures.all()) {
        await figure.scrollIntoViewIfNeeded();
        const image = figure.locator("img");
        await readyPictureImage(image);
        const bounds = await image.evaluate((element) => ({
          width: element.getBoundingClientRect().width,
          figureWidth: element.closest("figure").getBoundingClientRect().width,
          right: element.getBoundingClientRect().right,
          left: element.getBoundingClientRect().left,
          src: element.currentSrc,
        }));
        expect(bounds.width).toBeGreaterThan(250);
        expect(bounds.width).toBeGreaterThanOrEqual(bounds.figureWidth - 1);
        expect(bounds.left).toBeGreaterThanOrEqual(0);
        expect(bounds.right).toBeLessThanOrEqual(width);
        expect(bounds.src.endsWith("-narrow.svg")).toBe(width === 375);
        await expect(
          figure.getByRole("link", { name: /Open full diagram/ }),
        ).toBeVisible();
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await expect(page.locator('[data-md-component="top"]')).toHaveCount(0);
    });
  }

test("diagram text stays inside its canvas and does not overlap other labels", async ({
  page,
}) => {
  await page.goto("/architecture/");
  const links = await page
    .locator(".forge-architecture-diagram a")
    .evaluateAll((nodes) => [...new Set(nodes.map((node) => node.href))]);
  expect(links).toHaveLength(16);
  for (const link of links) {
    await page.goto(link);
    const collisions = await page.evaluate(async () => {
      await document.fonts.ready;
      const svg = document.querySelector("svg");
      const canvas = svg.viewBox.baseVal;
      const text = [...svg.querySelectorAll("text")].map((node) => ({
        label: node.textContent,
        box: node.getBBox(),
      }));
      const errors = [];
      for (let i = 0; i < text.length; i++) {
        const a = text[i].box;
        if (
          a.x < 0 ||
          a.y < 0 ||
          a.x + a.width > canvas.width ||
          a.y + a.height > canvas.height
        )
          errors.push(`Outside canvas: ${text[i].label}`);
        for (const other of text.slice(i + 1)) {
          const b = other.box;
          if (
            a.x < b.x + b.width &&
            a.x + a.width > b.x &&
            a.y < b.y + b.height &&
            a.y + a.height > b.y
          )
            errors.push(
              `Overlapping labels: ${text[i].label} / ${other.label}`,
            );
        }
      }
      return errors;
    });
    expect(collisions, link).toEqual([]);
  }
});
