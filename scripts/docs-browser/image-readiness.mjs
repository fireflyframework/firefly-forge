/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
const { expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

export async function readyImages(images) {
  for (const image of await images.all()) {
    await image.scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        image.evaluate((element) => {
          const declared = element.getAttribute("src");
          return (
            declared &&
            element.currentSrc === new URL(declared, document.baseURI).href &&
            element.complete &&
            element.naturalWidth > 0 &&
            element.naturalHeight > 0
          );
        }),
      )
      .toBe(true);
    await image.evaluate((element) => element.decode());
  }
}

export async function readyPictureImage(image) {
  const expectedSource = await image.evaluate((element) => {
    const sources = [...element.closest("picture").querySelectorAll("source")];
    const selected = sources.find(
      (source) => !source.media || matchMedia(source.media).matches,
    );
    return new URL(
      selected?.getAttribute("srcset") || element.getAttribute("src"),
      document.baseURI,
    ).href;
  });
  await expect
    .poll(() =>
      image.evaluate((element) => ({
        source: element.currentSrc,
        ready:
          element.complete &&
          element.naturalWidth > 0 &&
          element.naturalHeight > 0,
      })),
    )
    .toEqual({ source: expectedSource, ready: true });
  await image.evaluate((element) => element.decode());
}
