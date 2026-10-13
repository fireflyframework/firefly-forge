/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { readyImages, readyPictureImage } from "./image-readiness.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

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
