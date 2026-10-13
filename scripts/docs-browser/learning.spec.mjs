/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readyPictureImage } from "./image-readiness.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

for (const width of [1440, 375])
  for (const theme of ["light", "dark"]) {
    test(`learning diagrams and eligibility boundaries fit ${width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.goto("/learning/");
      await expect(
        page.getByRole("heading", {
          name: "Learn from evidence and reuse knowledge safely",
          level: 1,
        }),
      ).toBeVisible();
      await expect(
        page.getByText("Implementation boundary", { exact: true }),
      ).toBeVisible();
      const figures = page.locator(".forge-architecture-diagram");
      await expect(figures).toHaveCount(2);
      for (const figure of await figures.all()) {
        await figure.scrollIntoViewIfNeeded();
        const image = figure.locator("img");
        await readyPictureImage(image);
        const actual = await image.evaluate((element) => ({
          left: element.getBoundingClientRect().left,
          right: element.getBoundingClientRect().right,
          width: element.getBoundingClientRect().width,
          src: element.currentSrc,
        }));
        expect(actual.width).toBeGreaterThan(250);
        expect(actual.left).toBeGreaterThanOrEqual(0);
        expect(actual.right).toBeLessThanOrEqual(width);
        expect(actual.src.endsWith("-narrow.svg")).toBe(width === 375);
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page
        .getByRole("link", { name: "knowledge configuration", exact: true })
        .click();
      await expect(page).toHaveURL(/\/knowledge\/$/);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    });
  }

test("learning SVG labels fit each canvas without collisions", async ({
  page,
}) => {
  await page.goto("/learning/");
  const links = await page
    .locator(".forge-architecture-diagram a")
    .evaluateAll((nodes) => nodes.map((node) => node.href));
  expect(links).toHaveLength(4);
  for (const url of links) {
    await page.goto(url);
    const errors = await page.evaluate(async () => {
      await document.fonts.ready;
      const svg = document.querySelector("svg");
      const view = svg.viewBox.baseVal;
      const labels = [...svg.querySelectorAll("text")].map((node) => ({
        text: node.textContent,
        box: node.getBBox(),
      }));
      const errors = [];
      for (let i = 0; i < labels.length; i++) {
        const a = labels[i].box;
        if (
          a.x < 0 ||
          a.y < 0 ||
          a.x + a.width > view.width ||
          a.y + a.height > view.height
        )
          errors.push(labels[i].text + " clipped");
        for (let j = i + 1; j < labels.length; j++) {
          const b = labels[j].box;
          if (
            Math.min(a.x + a.width, b.x + b.width) > Math.max(a.x, b.x) &&
            Math.min(a.y + a.height, b.y + b.height) > Math.max(a.y, b.y)
          )
            errors.push(labels[i].text + " overlaps " + labels[j].text);
        }
      }
      return errors;
    });
    expect(errors, url).toEqual([]);
  }
});
