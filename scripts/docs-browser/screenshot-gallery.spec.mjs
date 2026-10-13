/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readyImages } from "./image-readiness.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

test("fixture boundaries render as a linked footnote", async ({ page }) => {
  await page.goto("/console-tour/");
  await expect(page.locator("article")).not.toContainText("[^captures]");
  const reference = page.locator('a.footnote-ref[href="#fn:captures"]');
  await expect(reference).toHaveCount(1);
  await reference.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#fn:captures$/);
  const note = page.locator('[id="fn:captures"]');
  await expect(note).toContainText("controlled API fixtures");
  await expect(note).toContainText("makes no real model");
  await note.locator("a.footnote-backref").click();
  await expect(page).toHaveURL(/#fnref:captures$/);
});

for (const width of [1440, 390, 375]) {
  for (const theme of ["light", "dark"]) {
    test(`Studio walkthrough screenshots fit at ${width}px in ${theme} appearance`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      let phoneCaptures = 0;
      for (const guide of [
        "console-tour",
        "search-providers",
        "quickstart",
        "guide",
      ]) {
        await page.goto(`/${guide}/`);
        await expect(page.locator("body")).toHaveAttribute(
          "data-md-color-scheme",
          theme === "dark" ? "slate" : "default",
        );
        await expect(page.locator('[data-md-component="top"]')).toHaveCount(0);
        const article = page.locator("article");
        await expect(article.locator("h1")).toBeVisible();
        expect(
          await article
            .locator("p")
            .evaluateAll((nodes) =>
              nodes.some((node) => /^\s*\|/.test(node.textContent)),
            ),
        ).toBe(false);
        for (const summary of await article
          .locator(".forge-gallery > summary")
          .all()) {
          await summary.focus();
          await page.keyboard.press("Enter");
          await expect(summary.locator("..")).toHaveAttribute("open", "");
        }
        await readyImages(article.locator(".forge-shot__link:visible img"));
        await expect
          .poll(() =>
            article
              .locator(".forge-shot__link:visible img")
              .evaluateAll(
                (images) =>
                  images.length > 0 &&
                  images.every(
                    (image) => image.complete && image.naturalWidth > 0,
                  ),
              ),
          )
          .toBe(["console-tour", "search-providers"].includes(guide));
        const links = article.locator(".forge-shot__link:visible");
        for (const link of await links.all()) {
          const image = link.locator("img");
          const measure = await image.evaluate((element) => ({
            width: element.getBoundingClientRect().width,
            natural: element.naturalWidth,
            phone: Boolean(element.closest(".forge-shot--phone")),
            right: element.getBoundingClientRect().right,
            left: element.getBoundingClientRect().left,
            pairCount: [
              ...element.closest("p, td").querySelectorAll("a"),
            ].filter((node) => getComputedStyle(node).display !== "none")
              .length,
            href: element.parentElement.getAttribute("href"),
            src: element.getAttribute("src"),
          }));
          expect(measure.natural).toBeGreaterThan(0);
          expect(measure.width).toBeLessThanOrEqual(measure.natural + 1);
          if (measure.phone) {
            phoneCaptures += 1;
            expect(measure.width).toBeLessThanOrEqual(321);
          }
          expect(measure.left).toBeGreaterThanOrEqual(0);
          expect(measure.right).toBeLessThanOrEqual(width + 1);
          expect(measure.href).toBe(measure.src);
          expect(measure.pairCount).toBe(1);
          await expect(link).toHaveAccessibleName(/Open full screenshot:/);
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
      }
      expect(phoneCaptures).toBeGreaterThan(0);
      await page.goto("/console-tour/");
      const phoneSummary = page.getByText(
        "Follow connection setup on a phone",
        { exact: true },
      );
      await phoneSummary.focus();
      await page.keyboard.press("Enter");
      const imageLink = page
        .locator(".forge-shot--phone .forge-shot__link:visible")
        .first();
      await imageLink.focus();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(
        /connect-workspace-mobile-(light|dark)\.png$/,
      );
    });
  }
}

for (const guide of ["console-tour", "search-providers"])
  test(`${guide} gallery remains readable with text enlarged to 200 percent`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 720, height: 900 });
    await page.goto(`/${guide}/`);
    await page.addStyleTag({
      content:
        ".md-typeset { font-size: 32px; } .forge-shot figcaption, .md-typeset details, .forge-gallery > summary { font-size: 28px; }",
    });
    const summary = page.locator(".forge-gallery > summary").first();
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(summary.locator("..")).toHaveAttribute("open", "");
    await expect(summary).toHaveCSS("font-size", "28px");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const grid = summary.locator("..").locator(".forge-shot-grid");
    const box = await grid.boundingBox();
    expect(box.x + box.width).toBeLessThanOrEqual(720);
  });
