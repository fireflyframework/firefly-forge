/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import {
  buildRuntimeFixture,
  mermaidPages,
  renderedDiagrams,
} from "./mermaid-runtime.mjs";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");
const root = fileURLToPath(new URL("../..", import.meta.url));

test("Mermaid enumeration maps nested index pages to portable directory URLs", () => {
  const docs = mkdtempSync(join(tmpdir(), "docs-mermaid-paths-"));
  mkdirSync(join(docs, "guides", "nested"), { recursive: true });
  const fence = "  ```mermaid\nflowchart TD\n A --> B\n  ```\n";
  for (const name of ["index.md", "guides/index.md", "guides/nested/graph.md"])
    writeFileSync(join(docs, name), fence);
  writeFileSync(join(docs, "ordinary.md"), "# Ordinary prose\n");
  expect(
    mermaidPages(docs).sort((a, b) => a.path.localeCompare(b.path)),
  ).toEqual([
    { path: "/", count: 1 },
    { path: "guides/", count: 1 },
    { path: "guides/nested/graph/", count: 1 },
  ]);
});

async function optionalRepositoryBadges(page) {
  for (const endpoint of [
    "https://api.github.com/repos/fireflyframework/firefly-forge",
    "https://api.github.com/repos/fireflyframework/firefly-forge/releases/latest",
  ]) {
    await page.route(endpoint, (route) =>
      route.fulfill({
        contentType: "application/json",
        json: { stargazers_count: 0, forks_count: 0, tag_name: "docs-fixture" },
      }),
    );
  }
}

async function assertDiagrams(page, count) {
  await expect
    .poll(async () => (await renderedDiagrams(page)).length)
    .toBe(count);
  const diagrams = await renderedDiagrams(page);
  for (const diagram of diagrams) {
    expect(diagram.width).toBeGreaterThan(100);
    expect(diagram.height).toBeGreaterThan(30);
    expect(diagram.error).toBe(false);
  }
  expect(
    await page
      .locator("pre")
      .evaluateAll(
        (nodes) =>
          nodes.filter((node) =>
            /^\s*(flowchart|sequenceDiagram)\b/.test(node.textContent),
          ).length,
      ),
  ).toBe(0);
  return diagrams;
}

for (const entry of mermaidPages(join(root, "docs"))) {
  for (const theme of ["light", "dark"]) {
    for (const width of [1440, 390]) {
      test(`all Mermaid fences on ${entry.path} render legible SVG at ${width}px in ${theme}`, async ({
        page,
      }, testInfo) => {
        await optionalRepositoryBadges(page);
        await page.setViewportSize({ width, height: 2000 });
        await page.emulateMedia({ colorScheme: theme });
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => {
          if (["warning", "error"].includes(message.type()))
            errors.push(message.text() + " " + message.location().url);
        });
        page.on("response", (response) => {
          if (response.status() >= 400)
            errors.push(`${response.status()} ${response.url()}`);
        });
        await page.goto(entry.path);
        await expect(page.locator("body")).toHaveAttribute(
          "data-md-color-scheme",
          theme === "dark" ? "slate" : "default",
        );
        const diagrams = await assertDiagrams(page, entry.count);
        await page
          .locator(".mermaid")
          .first()
          .screenshot({ path: testInfo.outputPath("diagram.png") });
        await testInfo.attach("render-metrics", {
          body: JSON.stringify(
            diagrams.map(({ width, height, labelPixels, error }) => ({
              width,
              height,
              labelPixels,
              error,
            })),
          ),
          contentType: "application/json",
        });
        for (const diagram of diagrams) {
          expect(diagram.labelPixels.length).toBeGreaterThan(0);
          expect(Math.min(...diagram.labelPixels)).toBeGreaterThanOrEqual(12);
        }
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        expect(errors).toEqual([]);
      });
    }
  }
}

test("Mermaid survives theme changes, instant navigation and browser history", async ({
  page,
}, testInfo) => {
  await optionalRepositoryBadges(page);
  const fixture = await buildRuntimeFixture(root);
  try {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (["warning", "error"].includes(message.type()))
        errors.push(message.text() + " " + message.location().url);
    });
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(fixture.baseURL);
    const originalDocument = await page.evaluate(() => {
      window.diagramDocument = "retained";
      return window.diagramDocument;
    });
    const original = await assertDiagrams(page, 1);
    await page.getByRole("link", { name: "Next diagram", exact: true }).click();
    await expect(page).toHaveURL(/\/next\/$/);
    expect(await page.evaluate(() => window.diagramDocument)).toBe(
      originalDocument,
    );
    const next = await assertDiagrams(page, 1);
    await page.locator('label[title="Switch to dark appearance"]').click();
    await expect(page.locator("body")).toHaveAttribute(
      "data-md-color-scheme",
      "slate",
    );
    const dark = await assertDiagrams(page, 1);
    expect(dark[0].fill).not.toEqual(next[0].fill);
    await page
      .locator(".mermaid")
      .screenshot({ path: testInfo.outputPath("instant-dark.png") });
    await page.goBack();
    await assertDiagrams(page, 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await page
      .locator(".mermaid")
      .screenshot({ path: testInfo.outputPath("mobile-dark.png") });
    expect(original[0].html).toContain("Owned source");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(errors).toEqual([]);
  } finally {
    fixture.close();
  }
});
