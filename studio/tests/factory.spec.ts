/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";

test.use({ launchOptions: { args: ["--enable-unsafe-swiftshader"] } });

async function fixture(page: Page) {
  const rootURL = new URL("/", test.info().project.use.baseURL);
  await page.route(
    (url) => url.href === rootURL.href,
    async (route) => {
      if (
        route.request().method() !== "GET" ||
        route.request().resourceType() !== "document"
      )
        return route.fallback();
      const response = await route.fetch();
      const html = await response.text();
      const entry = '<script type="module" src="/src/main.tsx"></script>';
      expect(
        html.match(
          /<script\b[^>]*\bsrc=["']\/src\/main\.tsx(?:\?[^"']*)?["'][^>]*>[\s\S]*?<\/script>/g,
        ),
      ).toEqual([entry]);
      await route.fulfill({
        response,
        body: html.replace(
          entry,
          '<script type="module" src="/tests/factory-renderer-fixture.ts"></script>',
        ),
      });
    },
  );
  await page.goto("/");
  await expect(page.locator(".factory-fixture .factory-live")).toHaveAttribute(
    "data-renderer",
    "webgl",
  );
}

async function state(page: Page, value: string, suspended = false) {
  await page.evaluate(
    (detail) =>
      document.dispatchEvent(
        new CustomEvent("factory-fixture-state", { detail }),
      ),
    { state: value, suspended },
  );
}
async function still(page: Page) {
  const viewport = page.locator(".factory-fixture .factory-live-viewport");
  await page.waitForTimeout(300);
  const first = await viewport.screenshot();
  await page.waitForTimeout(500);
  return first.equals(await viewport.screenshot());
}

test("machinery freezes for inactive, paused, stale updates and reduced motion", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await fixture(page);
  expect(await still(page)).toBe(true);
  await state(page, "active");
  await page.waitForTimeout(1100);
  expect(await still(page)).toBe(false);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("factory-fixture-visibility", { detail: true }),
    ),
  );
  expect(await still(page)).toBe(true);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("factory-fixture-visibility", { detail: false }),
    ),
  );
  expect(await still(page)).toBe(false);
  await state(page, "paused");
  expect(await still(page)).toBe(true);
  await state(page, "active", true);
  expect(await still(page)).toBe(true);
  await expect(
    page.locator(".factory-fixture .factory-live-caption"),
  ).toContainText("Updates unavailable; showing last recorded state");
  await expect(
    page.locator(".factory-fixture .factory-live-station.active"),
  ).toContainText("implement");
  await state(page, "active");
  expect(await still(page)).toBe(false);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(page.locator(".factory-fixture .factory-live")).toHaveAttribute(
    "data-reduced-motion",
    "true",
  );
  await expect.poll(() => still(page)).toBe(true);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await expect(page.locator(".factory-fixture .factory-live")).toHaveAttribute(
    "data-reduced-motion",
    "false",
  );
  expect(await still(page)).toBe(false);
  await state(page, "done");
  expect(await still(page)).toBe(true);
  expect(errors).toEqual([]);
});

test("context loss preserves stage controls and disposing removes the canvas", async ({
  page,
}) => {
  await fixture(page);
  const scene = page.locator(".factory-fixture .factory-live");
  await scene.locator("canvas").evaluate((canvas) => {
    const context = (canvas as HTMLCanvasElement).getContext("webgl2")!;
    context.getExtension("WEBGL_lose_context")!.loseContext();
  });
  await expect(scene).toHaveAttribute("data-renderer", "svg");
  await expect(scene.locator(".factory-live-fallback")).toContainText(
    "Recorded stage states remain available",
  );
  await scene.locator(".factory-live-station").nth(2).focus();
  await page.keyboard.press("ArrowRight");
  await expect(scene.locator(".factory-live-station").nth(3)).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(scene.locator(".factory-live-station").nth(3)).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await scene.getByRole("button", { name: "Try 3D again" }).click();
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  await page.evaluate(() =>
    document.dispatchEvent(new Event("factory-fixture-unmount")),
  );
  await expect(page.locator(".factory-fixture canvas")).toHaveCount(0);
});
