/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
const { expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

export async function readyImages(images) {
  await images.evaluateAll((nodes) => {
    for (const image of nodes)
      if (image.loading === "lazy" && !image.currentSrc)
        image.loading = "eager";
  });
  await expect
    .poll(() =>
      images.evaluateAll((nodes) =>
        nodes.every((image) => {
          const declared = image.getAttribute("src");
          return (
            declared &&
            image.currentSrc === new URL(declared, document.baseURI).href &&
            image.complete &&
            image.naturalWidth > 0 &&
            image.naturalHeight > 0
          );
        }),
      ),
    )
    .toBe(true);
  await images.evaluateAll((nodes) =>
    Promise.all(nodes.map((image) => image.decode())),
  );
}
