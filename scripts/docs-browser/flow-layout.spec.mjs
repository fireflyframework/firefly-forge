/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { renderedDiagrams } from "./mermaid-runtime.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

const flows = [
  {
    path: "git-diff-archive/",
    nodes: ["A", "B", "C", "D", "E", "F"],
  },
  {
    path: "as-is-handoff/",
    nodes: ["Source", "Review", "Context", "Proposal", "Confirm"],
  },
  {
    path: "product-lifecycle/",
    phases: ["ChangePhase", "ReviewPhase", "BuildPhase"],
  },
  {
    path: "structured-knowledge/",
    phases: ["PreparePhase", "QueryPhase", "EvidencePhase"],
  },
];

for (const flow of flows) {
  for (const width of [1440, 390]) {
    for (const theme of ["light", "dark"]) {
      test(`phase flow on ${flow.path} reads left to right at ${width}px in ${theme}`, async ({
        page,
      }, testInfo) => {
        await page.setViewportSize({ width, height: 1200 });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.goto(flow.path);
        const region = page.getByRole("region", { name: /diagram/ });
        await expect(region).toHaveCount(1);
        await expect(region).toHaveAttribute("tabindex", "0");
        let diagram;
        await expect
          .poll(async () => {
            [diagram] = await renderedDiagrams(page);
            return flow.phases
              ? diagram?.phases.length
              : diagram?.nodes.filter((node) =>
                  flow.nodes.some(
                    (id) =>
                      node.source === id || node.id.includes("-" + id + "-"),
                  ),
                ).length;
          })
          .toBe((flow.phases ?? flow.nodes).length);
        const phases = flow.phases
          ? flow.phases.map((id) =>
              diagram.phases.find((phase) => phase.id.endsWith("-" + id)),
            )
          : flow.nodes.map((id) =>
              diagram.nodes.find(
                (node) =>
                  node.source === id || node.id.includes("-" + id + "-"),
              ),
            );
        expect(phases.every(Boolean)).toBe(true);
        for (let index = 1; index < phases.length; index++) {
          expect(phases[index].left).toBeGreaterThan(phases[index - 1].right);
        }
        expect(Math.min(...diagram.labelPixels)).toBeGreaterThanOrEqual(12);
        expect(diagram.error).toBe(false);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        const dimensions = await region.evaluate((element) => ({
          width: element.clientWidth,
          content: element.scrollWidth,
        }));
        if (width === 390) {
          expect(dimensions.content).toBeGreaterThan(dimensions.width);
          await region.focus();
          await page.keyboard.press("ArrowRight");
          await expect
            .poll(() => region.evaluate((element) => element.scrollLeft))
            .toBeGreaterThan(0);
          await region.evaluate(
            (element) => (element.scrollLeft = element.scrollWidth),
          );
          expect(
            await region.evaluate(
              (element) =>
                element.scrollLeft + element.clientWidth >=
                element.scrollWidth - 1,
            ),
          ).toBe(true);
          await region.evaluate((element) => (element.scrollLeft = 0));
        } else {
          expect(dimensions.content).toBeLessThanOrEqual(dimensions.width + 1);
        }
        const screenshot = testInfo.outputPath("phase-flow.png");
        await region.screenshot({ path: screenshot });
        await testInfo.attach("phase-flow", {
          path: screenshot,
          contentType: "image/png",
        });
        await testInfo.attach("phase-geometry", {
          body: JSON.stringify({
            path: flow.path,
            width,
            theme,
            phases,
            dimensions,
            labelPixels: diagram.labelPixels,
          }),
          contentType: "application/json",
        });
        expect(errors).toEqual([]);
      });
    }
  }
}
