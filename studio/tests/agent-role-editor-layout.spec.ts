/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { openRoleEditor } from "./agent-lifecycle-fixture";
for (const theme of ["light", "dark"]) {
  for (const width of [1440, 390]) {
    test(`role editor aligns its instructions and controls ${width} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      await openRoleEditor(page, theme);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const panel = page.locator(".agent-lifecycle"),
        editor = page.locator(".lifecycle-editor");
      const geometry = await panel.evaluate((node) => {
        const panel = node.getBoundingClientRect(),
          editor = node
            .querySelector(".lifecycle-editor")!
            .getBoundingClientRect(),
          textarea = node.querySelector("textarea")!.getBoundingClientRect();
        return {
          inset: editor.left - panel.left,
          editor: editor.width,
          textarea: textarea.width,
          height: textarea.height,
        };
      });
      expect(geometry.inset).toBeGreaterThanOrEqual(width === 390 ? 16 : 28);
      expect(geometry.textarea).toBeGreaterThanOrEqual(geometry.editor - 2);
      expect(geometry.height).toBeGreaterThanOrEqual(200);
      await expect(page.getByLabel("Role instructions")).toBeFocused();
      await page.keyboard.press("ControlOrMeta+A");
      await page.keyboard.insertText(
        "Preserve the customer's reviewed boundaries.",
      );
      await page.keyboard.press("Tab");
      await expect(
        page.getByRole("checkbox", { name: "Implementation", exact: true }),
      ).toBeFocused();
      await editor.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`instructions-${theme}-${width}.png`),
      });
      await page
        .getByRole("button", { name: "Close draft", exact: true })
        .scrollIntoViewIfNeeded();
      expect(
        await panel.evaluate((node) => node.scrollWidth <= node.clientWidth),
      ).toBe(true);
      await page.screenshot({
        path: info.outputPath(`controls-${theme}-${width}.png`),
      });
    });
  }
  test(`role editor remains usable at actual CSS zoom200 ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await openRoleEditor(page, theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.evaluate(() => {
      document.documentElement.style.zoom = "2";
    });
    const instructions = page.getByLabel("Role instructions");
    await instructions.fill("Retain the draft at200percent.");
    await page
      .getByRole("button", { name: "Close draft", exact: true })
      .scrollIntoViewIfNeeded();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const choices = await page
      .locator(".lifecycle-editor fieldset")
      .evaluateAll((groups) =>
        groups.map((group) => ({
          right: group.getBoundingClientRect().right,
          viewport: innerWidth,
          labelsFit: Array.from(group.querySelectorAll("label")).every(
            (label) => label.scrollWidth <= label.clientWidth,
          ),
        })),
      );
    for (const group of choices) {
      expect(group.right).toBeLessThanOrEqual(group.viewport);
      expect(group.labelsFit).toBe(true);
    }
    await expect(instructions).toHaveValue("Retain the draft at200percent.");
    await page.screenshot({
      path: info.outputPath(`role-editor-css-zoom200-${theme}.png`),
    });
  });
}
