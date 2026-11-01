/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Source-only fixture: navigation, filtering and selection send no inspection POST.
import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import {
  setupSourceSelector,
  selectorRepositories,
} from "./source-component-selector-fixture";
test.use({ launchOptions: { ignoreDefaultArgs: ["--hide-scrollbars"] } });
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 320, height: 568 },
])
  for (const theme of ["light", "dark"] as const)
    test(`source inspection flow preserves scope and native page access at ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      const { view, inspections } = await setupSourceSelector(page, theme);
      const main = page.locator(".workspace-main > main");
      const fields = view.locator(".wizard-field-scroll");
      await expect(
        view.getByRole("heading", { name: "Source inspection", exact: true }),
      ).toBeFocused();
      await expect(
        page.getByRole("dialog", { name: "Source inspection review" }),
      ).toHaveCount(0);
      await expect(view.locator(".wizard")).toHaveAttribute(
        "data-layout",
        "flow",
      );
      await expect(view.locator(".wizard")).toHaveClass(/wizard-rail/);
      await expect(main).toHaveCSS("overflow-y", "auto");
      await expect(fields).toHaveCSS("overflow-y", "visible");
      await expect(
        view
          .getByRole("list", { name: "Component results" })
          .getByRole("checkbox"),
      ).toHaveCount(25);
      await expect(page.locator(".form-scroll-status")).toHaveCount(0);
      const geometry = await fields.evaluate((node) => ({
        height: node.clientHeight,
        full: node.scrollHeight,
        position: node.scrollTop,
      }));
      expect(geometry.full).toBeLessThanOrEqual(geometry.height + 1);
      expect(geometry.position).toBe(0);
      await writeFile(
        info.outputPath("entry-geometry.json"),
        JSON.stringify(geometry, null, 2),
      );
      await expect.poll(() => main.evaluate((node) => node.scrollTop)).toBe(0);
      if (viewport.width === 320) {
        const searchBox = await view
          .getByLabel("Search components", { exact: true })
          .boundingBox();
        const footerBox = await page
          .locator(".workspace-main > footer")
          .boundingBox();
        expect(searchBox!.y + searchBox!.height).toBeLessThanOrEqual(
          footerBox!.y,
        );
      }
      await page.screenshot({
        path: info.outputPath(`source-entry-${theme}.png`),
      });

      await main.focus();
      await main.evaluate((node) => {
        node.dataset.sourceScrollEnd = "waiting";
        node.addEventListener(
          "scrollend",
          () => {
            node.dataset.sourceScrollEnd = "done";
          },
          { once: true },
        );
      });
      await page.keyboard.press("End");
      await expect(main).toHaveAttribute("data-source-scroll-end", "done");
      const endPosition = await main.evaluate((node) => node.scrollTop);
      expect(endPosition).toBeGreaterThan(0);
      await main.hover();
      await page.mouse.wheel(0, -200);
      await expect
        .poll(() => main.evaluate((node) => node.scrollTop))
        .toBeLessThan(endPosition);

      await main.evaluate((node) => {
        node.scrollTop = 0;
      });
      await view
        .getByLabel("Search components", { exact: true })
        .fill("component-60");
      await view.locator(".source-component-filters > summary").click();
      await view
        .getByLabel("Component type", { exact: true })
        .selectOption("worker");
      await view
        .getByLabel("Repository", { exact: true })
        .selectOption(selectorRepositories[1].id);
      await expect(
        view.getByRole("status", { name: "Component selection" }),
      ).toHaveText("60 of 60 selected · 1 result");
      const search = view.getByLabel("Search components", { exact: true });
      await search.evaluate((node) => {
        const main = node.closest("main")!;
        main.scrollTop +=
          node.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
      });
      await page.screenshot({
        path: info.outputPath(`source-filters-${theme}.png`),
      });
      const last = view.getByRole("checkbox", { name: /Inventory worker 60/ });
      await last.evaluate((node) => {
        const main = node.closest("main")!;
        main.scrollTop +=
          node.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
        (node as HTMLInputElement).focus({ preventScroll: true });
      });
      await expect(last).toBeFocused();
      await expect(last).toBeInViewport();
      await page.screenshot({
        path: info.outputPath(`source-result-${theme}.png`),
      });
      const action = view.getByRole("button", {
        name: "Inspect 60 components",
        exact: true,
      });
      await action.focus();
      await expect(action).toBeFocused();
      const actionBox = await action.boundingBox(),
        footerBox = await page
          .locator(".workspace-main > footer")
          .boundingBox();
      expect(actionBox!.y + actionBox!.height).toBeLessThanOrEqual(
        footerBox!.y,
      );
      await expect(page.locator(".workspace-main > header")).toBeInViewport();
      expect(
        await page
          .locator(".workspace-main")
          .evaluate((node) => node.scrollTop),
      ).toBe(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: info.outputPath(`source-actions-${theme}.png`),
      });
      expect(inspections).toEqual([]);
    });
