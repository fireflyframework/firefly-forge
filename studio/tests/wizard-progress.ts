/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Locator } from "@playwright/test";

export async function expectProgressFits(progress: Locator, current: string) {
  await expect(progress.locator('[aria-current="step"]')).toHaveText(
    new RegExp(`${current}$`),
  );
  const geometry = await progress.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const buttons = [...element.querySelectorAll("button")].map((button) => {
      const box = button.getBoundingClientRect();
      return box.left >= bounds.left && box.right <= bounds.right;
    });
    const label = element
      .querySelector('[aria-current="step"] > span:last-child')!
      .getBoundingClientRect();
    return {
      scroll: element.scrollWidth,
      width: element.clientWidth,
      buttonsFit: buttons.every(Boolean),
      currentLabelFits:
        label.left >= bounds.left && label.right <= bounds.right,
    };
  });
  expect.soft(geometry.scroll, current).toBeLessThanOrEqual(geometry.width);
  expect.soft(geometry.buttonsFit, current).toBe(true);
  expect.soft(geometry.currentLabelFits, current).toBe(true);
}
