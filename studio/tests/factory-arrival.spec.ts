/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
test.use({ launchOptions: { args: ["--enable-unsafe-swiftshader"] } });

async function fixture(page: Page) {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({ status: 401, json: { detail: "Local renderer fixture" } }),
  );
  await page.route("**/src/FactoryScene.tsx*", async (route) => {
    const response = await route.fetch();
    const source = await response.text();
    await route.fulfill({
      response,
      body: source.replace(
        "renderer.setPixelRatio(Math.min(devicePixelRatio, 2));",
        `
window.factoryPaint = paint;
const renderFactoryOriginal = renderer.render;
renderer.render = function(scene, camera) {
 const carrier = scene.getObjectByName('receipted_output');
 if (carrier) window.factoryCarrier = carrier;
 return renderFactoryOriginal.call(this, scene, camera);
};
const playFactoryOriginal = THREE.AnimationAction.prototype.play;
THREE.AnimationAction.prototype.play = function() {
 (window.factoryClipStarts ??= []).push({id:this.getClip().name,x:window.factoryCarrier?.position.x});
 return playFactoryOriginal.call(this);
};
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));`,
      ),
    });
  });
  await page.goto("/");
  await page.evaluate(async () => {
    const reactPath = "/node_modules/.vite/deps/react.js",
      domPath = "/node_modules/.vite/deps/react-dom_client.js",
      scenePath = "/src/FactoryScene.tsx";
    const [
      {
        default: { createElement, Fragment, useLayoutEffect },
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
    mount.style.cssText = "width:1100px;margin:24px auto";
    document.body.append(mount);
    const root = createRoot(mount);
    let state = "waiting",
      suspended = false,
      target = "implement";
    function CommitProbe({ suspended }: { suspended: boolean }) {
      useLayoutEffect(() => {
        if (!suspended) return;
        const state = window as unknown as {
          factoryCarrier: { position: { x: number } };
          factoryPaint: (time: number) => void;
          factoryCommitFrame?: {
            before: number;
            after: number;
            caption: string;
          };
        };
        // Exercise a renderer frame after DOM commit, before passive effects can synchronize state.
        const before = state.factoryCarrier.position.x;
        state.factoryPaint(performance.now() + 50);
        state.factoryCommitFrame = {
          before,
          after: state.factoryCarrier.position.x,
          caption: document.querySelector(".factory-live-caption")!
            .textContent!,
        };
      }, [suspended]);
      return null;
    }
    const render = () =>
      root.render(
        createElement(
          Fragment,
          null,
          createElement(FactoryScene, {
            theme: "dark",
            selectedId: null,
            suspended,
            onSelect: () => {},
            stations: [
              "prepare",
              "implement",
              "verify",
              "checkpoint",
              "publish",
              "deploy",
              "health",
            ].map((id) => ({
              id,
              label: id,
              state:
                state === "reset"
                  ? "waiting"
                  : id === "prepare"
                    ? "done"
                    : id === target
                      ? state
                      : "waiting",
              detail: "Local renderer fixture; no factory execution",
            })),
          }),
          createElement(CommitProbe, { suspended }),
        ),
      );
    document.addEventListener("arrival-fixture-state", (event) => {
      state = (event as CustomEvent<string>).detail;
      render();
    });
    document.addEventListener("arrival-fixture-suspended", (event) => {
      suspended = (event as CustomEvent<boolean>).detail;
      render();
    });
    document.addEventListener("arrival-fixture-target", (event) => {
      target = (event as CustomEvent<string>).detail;
      render();
    });
    mount.id = "arrival-fixture";
    render();
  });
  const scene = page.locator(".factory-live");
  await expect(scene).toHaveAttribute("data-renderer", "webgl");
  return scene;
}

test("a real machinery clip begins only after the carrier arrives, and blocked work stays at that station", async ({
  page,
}) => {
  const scene = await fixture(page);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              factoryCarrier?: { position: { x: number } };
            }
          ).factoryCarrier?.position.x,
      ),
    )
    .toBe(-12.3);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-state", { detail: "active" }),
    ),
  );
  const position = () =>
    page.evaluate(
      () =>
        (window as unknown as { factoryCarrier: { position: { x: number } } })
          .factoryCarrier.position.x,
    );
  await expect.poll(position).toBeGreaterThan(-12.3);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-suspended", { detail: true }),
    ),
  );
  await expect(scene.locator(".factory-live-caption")).toContainText(
    "Updates unavailable",
  );
  const commitFrame = await page.evaluate(
    () =>
      (
        window as unknown as {
          factoryCommitFrame: {
            before: number;
            after: number;
            caption: string;
          };
        }
      ).factoryCommitFrame,
  );
  expect(commitFrame.caption).toContain("Updates unavailable");
  expect(commitFrame.after).toBe(commitFrame.before);
  let held = await position();
  await page.waitForTimeout(200);
  expect(await position()).toBe(held);
  expect(held).toBeLessThan(-8.2);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-suspended", { detail: false }),
    ),
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect.poll(position).toBe(-8.2);
  held = await position();
  await page.waitForTimeout(200);
  expect(await position()).toBe(held);
  await page.evaluate(() => {
    document.getElementById("arrival-fixture")!.style.marginTop = "2000px";
  });
  await page.waitForTimeout(150);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  held = await position();
  await page.waitForTimeout(200);
  expect(await position()).toBe(held);
  await page.evaluate(() => {
    document.getElementById("arrival-fixture")!.style.marginTop = "24px";
  });
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              factoryClipStarts?: { id: string; x: number }[];
            }
          ).factoryClipStarts?.find((item) => item.id === "implement")?.x,
      ),
    )
    .toBe(-8.2);
  for (const state of ["paused", "failed"]) {
    await page.evaluate(
      (detail) =>
        document.dispatchEvent(
          new CustomEvent("arrival-fixture-state", { detail }),
        ),
      state,
    );
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            (
              window as unknown as {
                factoryCarrier: { position: { x: number } };
              }
            ).factoryCarrier.position.x,
        ),
      )
      .toBe(-8.2);
    await expect(scene.locator(`.factory-live-station.${state}`)).toContainText(
      "implement",
    );
  }
});

test("reduced-motion target updates display each actual station and a new run starts at its first receipt", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page);
  const position = () =>
    page.evaluate(
      () =>
        (window as unknown as { factoryCarrier: { position: { x: number } } })
          .factoryCarrier.position.x,
    );
  await expect.poll(position).toBe(-12.3);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-state", { detail: "active" }),
    ),
  );
  await expect.poll(position).toBe(-8.2);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-target", { detail: "verify" }),
    ),
  );
  await expect.poll(position).toBe(-4.1);
  await page.waitForTimeout(200);
  expect(await position()).toBe(-4.1);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { factoryClipStarts?: unknown[] })
          .factoryClipStarts ?? [],
    ),
  ).toEqual([]);
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-state", { detail: "reset" }),
    ),
  );
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (window as unknown as { factoryCarrier: { visible: boolean } })
            .factoryCarrier.visible,
      ),
    )
    .toBe(false);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-target", { detail: "implement" }),
    ),
  );
  await page.evaluate(() =>
    document.dispatchEvent(
      new CustomEvent("arrival-fixture-state", { detail: "active" }),
    ),
  );
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              factoryClipStarts?: { id: string; x: number }[];
            }
          ).factoryClipStarts?.find((item) => item.id === "implement")?.x,
      ),
    )
    .toBe(-8.2);
});
