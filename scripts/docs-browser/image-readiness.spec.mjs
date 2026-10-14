/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readyImages, readyPictureImage } from "./image-readiness.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

test("bulk readiness visits offscreen lazy images without changing loading", async ({
  page,
}) => {
  await page.goto("/console-tour/");
  const source = await page
    .locator(".forge-shot__link img")
    .first()
    .evaluate(
      (image) => new URL(image.getAttribute("src"), document.baseURI).href,
    );
  await page.setContent(
    `<img src="${source}" loading="lazy" width="320" height="200">
     <div style="height:20000px"></div>
     <img src="${source}" loading="lazy" width="320" height="200">`,
  );
  await page.evaluate(() => {
    const decode = HTMLImageElement.prototype.decode;
    HTMLImageElement.prototype.decode = function () {
      this.dataset.decodes = String(Number(this.dataset.decodes || 0) + 1);
      return decode.call(this);
    };
  });
  const images = page.locator("img");
  expect((await images.last().boundingBox()).y).toBeGreaterThan(10000);
  await readyImages(images);
  for (const image of await images.all()) {
    await expect(image).toHaveAttribute("loading", "lazy");
    await expect(image).toHaveAttribute("data-decodes", "1");
  }
  await expect(images.last()).toBeInViewport();
});

for (const kind of ["screenshot", "picture"])
  test(`ready ${kind} does not restart an already loaded image`, async ({
    page,
  }) => {
    await page.goto(kind === "picture" ? "/learning/" : "/console-tour/");
    const image = page
      .locator(
        kind === "picture"
          ? ".forge-architecture-diagram img"
          : ".forge-shot__link:visible img",
      )
      .first();
    await image.scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        image.evaluate(
          (element) =>
            element.complete &&
            element.naturalWidth > 0 &&
            element.naturalHeight > 0,
        ),
      )
      .toBe(true);
    const before = await image.evaluate((element) => ({
      source: element.currentSrc,
      loading: element.getAttribute("loading"),
    }));
    await (kind === "picture" ? readyPictureImage(image) : readyImages(image));
    expect(
      await image.evaluate((element) => ({
        source: element.currentSrc,
        loading: element.getAttribute("loading"),
      })),
    ).toEqual(before);
  });
