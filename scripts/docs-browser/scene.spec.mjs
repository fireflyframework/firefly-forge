/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
 * https://www.apache.org/licenses/LICENSE-2.0 */
import { createRequire } from "node:module";
const { test, expect } = createRequire(
  new URL("../../studio/package.json", import.meta.url),
)("@playwright/test");

test("local 3D illustration responds to motion, resize, visibility and theme", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  const scene = page.locator("[data-docs-scene]");
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  await expect(scene).toHaveAttribute("data-motion", "running");
  const canvas = scene.locator("canvas");
  await expect(canvas).toBeVisible();
  const dimensions = () =>
    canvas.evaluate((element) => `${element.width}x${element.height}`);
  const original = await dimensions();
  await scene.getByRole("button", { name: "Pause illustration" }).click();
  await expect(scene).toHaveAttribute("data-motion", "paused");
  await page.waitForTimeout(100);
  const paused = await canvas.screenshot();
  await page.waitForTimeout(250);
  expect(await canvas.screenshot()).toEqual(paused);
  await expect(scene.getByRole("button")).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  await scene.getByRole("button", { name: "Play illustration" }).click();
  await expect(scene).toHaveAttribute("data-motion", "running");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(dimensions).not.toBe(original);
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  await page.locator("footer").scrollIntoViewIfNeeded();
  await expect(scene).toHaveAttribute("data-motion", "hidden");
  await scene.scrollIntoViewIfNeeded();
  await expect(scene).toHaveAttribute("data-motion", "running");
  const currentTheme = await page
    .locator("body")
    .getAttribute("data-md-color-scheme");
  const label =
    currentTheme === "slate"
      ? "Switch to light appearance"
      : "Switch to dark appearance";
  await page.locator(`label[title="${label}"]`).click();
  await expect(page.locator("body")).toHaveAttribute(
    "data-md-color-scheme",
    currentTheme === "slate" ? "default" : "slate",
  );
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  await page.evaluate(() =>
    window.dispatchEvent(new PageTransitionEvent("pagehide")),
  );
  await expect(scene).toHaveAttribute("data-motion", "disposed");
  await expect(canvas).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("reduced motion renders a static scene and never opts the visitor into animation", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  const scene = page.locator("[data-docs-scene]");
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  await expect(scene).toHaveAttribute("data-motion", "reduced");
  await expect(
    scene.getByRole("button", { name: "Motion reduced" }),
  ).toBeDisabled();
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(scene).toHaveAttribute("data-motion", "running");
});

test("WebGL failure preserves the accessible fallback and normal documentation", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, ...args) {
      if (String(type).startsWith("webgl")) return null;
      return original.call(this, type, ...args);
    };
  });
  await page.goto("/");
  const scene = page.locator("[data-docs-scene]");
  await expect(scene.locator("[data-scene-status]")).toContainText(
    "3D is unavailable",
  );
  await expect(scene).toHaveAttribute("data-renderer", "fallback");
  await expect(scene.locator(".docs-scene__fallback")).toBeVisible();
  await expect(scene.locator("canvas")).toHaveCount(0);
  await expect(scene.getByRole("button")).toBeDisabled();
  await expect(page.locator("h1")).toBeVisible();
});

test("animation changes the rendered pixels and context loss releases the GPU canvas", async ({
  page,
}) => {
  await page.goto("/");
  const scene = page.locator("[data-docs-scene]");
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  const canvas = scene.locator("canvas");
  const before = await canvas.screenshot();
  await page.waitForTimeout(350);
  expect(await canvas.screenshot()).not.toEqual(before);
  await canvas.evaluate((element) => {
    const gl = element.getContext("webgl2");
    const extension = gl.getExtension("WEBGL_lose_context");
    if (!extension)
      throw new Error("The test browser cannot exercise real context loss");
    extension.loseContext();
  });
  await expect(scene.locator("[data-scene-status]")).toContainText(
    "3D is unavailable",
  );
  await expect(scene).toHaveAttribute("data-renderer", "fallback");
  await expect(canvas).toHaveCount(0);
  await expect(scene.locator(".docs-scene__fallback")).toBeVisible();
});

test("a persisted page transition restores the prior paused scene", async ({
  page,
}) => {
  await page.goto("/");
  const scene = page.locator("[data-docs-scene]");
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  await scene.getByRole("button", { name: "Pause illustration" }).click();
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent("pagehide", { persisted: true }),
    ),
  );
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    ),
  );
  await expect(scene).toHaveAttribute("data-motion", "paused");
  await expect(scene.locator("canvas")).toHaveCount(1);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(scene).toHaveAttribute("data-motion", "reduced");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(scene).toHaveAttribute("data-motion", "paused");
});

test("the short phone hero has no visual filler and the header controls remain reachable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 720 });
  await page.goto("/");
  const scene = page.locator("[data-docs-scene]");
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  await expect(scene.locator(".docs-scene__labels")).toHaveCount(0);
  await expect(scene.locator("a")).toHaveCount(0);
  await expect(scene.locator("[data-scene-status]")).toHaveCSS(
    "clip-path",
    "inset(50%)",
  );
  await expect(page.locator("#scene-demo-note")).toHaveCount(1);
  const header = page.locator(".md-header__button.md-logo");
  await expect(header).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  for (const target of await page
    .locator(
      "header label[for=__drawer], header label[for=__search], header label[title]",
    )
    .all()) {
    if (!(await target.isVisible())) continue;
    const box = await target.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
  }
});
