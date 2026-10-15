/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { renderedDiagrams } from "./mermaid-runtime.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

const flows = [
  {
    name: "Product project and campaign relationships diagram",
    nodes: ["Product", "Project", "Component", "Campaign", "Run", "Evidence"],
  },
  {
    name: "Source control conversation and factory feedback diagram",
    nodes: ["Comment", "Inbox", "Intent", "Action", "Receipt", "Reply"],
  },
];

for (const width of [1440, 390]) {
  for (const theme of ["light", "dark"]) {
    test(`factory journeys retain readable relationships and screenshots at ${width}px in ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 1050 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto("/factory-journeys/");
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(
        "Understand the factory and follow a delivery",
      );
      await expect
        .poll(async () => (await renderedDiagrams(page)).length)
        .toBe(flows.length);
      const diagrams = await renderedDiagrams(page);
      for (const [index, flow] of flows.entries()) {
        const region = page.getByRole("region", {
          name: flow.name,
          exact: true,
        });
        await expect(region).toHaveAttribute("tabindex", "0");
        const diagram = diagrams[index];
        const nodes = flow.nodes.map((id) =>
          diagram.nodes.find(
            (node) => node.source === id || node.id.includes("-" + id + "-"),
          ),
        );
        expect(nodes.every(Boolean)).toBe(true);
        for (let position = 1; position < nodes.length; position++)
          expect(nodes[position].left).toBeGreaterThan(
            nodes[position - 1].right,
          );
        expect(diagram.error).toBe(false);
        expect(Math.min(...diagram.labelPixels)).toBeGreaterThanOrEqual(12);
        if (width === 390) {
          await region.focus();
          await page.keyboard.press("ArrowRight");
          await expect
            .poll(() => region.evaluate((element) => element.scrollLeft))
            .toBeGreaterThan(0);
        }
        await region.evaluate((element) => (element.scrollLeft = 0));
        await region.screenshot({
          path: testInfo.outputPath(`journey-diagram-${index + 1}.png`),
        });
      }
      const images = page.locator(".forge-shot img:visible");
      await expect(images).toHaveCount(4);
      for (const image of await images.all()) {
        await image.scrollIntoViewIfNeeded();
        await expect
          .poll(() =>
            image.evaluate(
              (element) => element.complete && element.naturalWidth > 0,
            ),
          )
          .toBe(true);
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(errors).toEqual([]);
      await testInfo.attach("journey-diagram-geometry", {
        body: JSON.stringify({ width, theme, diagrams }),
        contentType: "application/json",
      });
    });
  }
}
