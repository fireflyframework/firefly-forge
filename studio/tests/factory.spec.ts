/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";

test.use({ launchOptions: { args: ["--enable-unsafe-swiftshader"] } });

async function fixture(page: Page) {
  await page.goto("/");
  await page.evaluate(async () => {
    const reactPath = "/node_modules/.vite/deps/react.js";
    const domPath = "/node_modules/.vite/deps/react-dom_client.js";
    const scenePath = "/src/FactoryScene.tsx";
    const [
      {
        default: { createElement },
      },
      {
        default: { createRoot },
      },
      { FactoryScene },
    ] = await Promise.all([
      import(reactPath),
      import(domPath),
      import(scenePath),
    ]);
    (document.querySelector("#root") as HTMLElement).style.display = "none";
    const mount = document.createElement("div");
    mount.className = "factory-fixture";
    mount.style.cssText = "width:1100px;margin:24px auto";
    document.body.appendChild(mount);
    const root = createRoot(mount);
    const ids = [
      "prepare",
      "implement",
      "verify",
      "checkpoint",
      "publish",
      "deploy",
      "health",
    ];
    let state = "waiting",
      selectedId = "implement",
      suspended = false;
    const render = () =>
      root.render(
        createElement(FactoryScene, {
          stations: ids.map((id) => ({
            id,
            label: id,
            state:
              id === "implement"
                ? state
                : id === "prepare"
                  ? "done"
                  : "waiting",
            detail: "Renderer fixture; no server production is inferred.",
          })),
          theme: "dark",
          selectedId,
          suspended,
          onSelect: (id: string) => {
            selectedId = id;
            render();
          },
        }),
      );
    document.addEventListener("factory-fixture-state", (event) => {
      const detail = (
        event as CustomEvent<{ state: string; suspended: boolean }>
      ).detail;
      state = detail.state;
      suspended = detail.suspended;
      render();
    });
    document.addEventListener("factory-fixture-visibility", (event) => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: (event as CustomEvent<boolean>).detail,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    document.addEventListener("factory-fixture-unmount", () => root.unmount(), {
      once: true,
    });
    render();
  });
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
  test.setTimeout(60_000);
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
  expect(await still(page)).toBe(true);
  await page.emulateMedia({ reducedMotion: "no-preference" });
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
