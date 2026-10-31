/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Long source-inspection choices are synthetic; no inspection POST is sent.
import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
for (const theme of ["light", "dark"] as const)
  test(`bounded source review keeps its last control and actions reachable at320x568 ${theme}`, async ({
    page,
  }, info) => {
    const repository = {
      id: "a".repeat(32),
      name: "Modal fixture repository",
      provider: "git",
      url: "/fixture",
      default_branch: "main",
    };
    const components = Array.from({ length: 20 }, (_, i) => ({
      id: `component-${i + 1}`,
      name: `Component ${String(i + 1).padStart(2, "0")}`,
      repository_id: repository.id,
      path: `apps/component-${i + 1}`,
      kind: "api",
      backend: "pyfly",
      frontend: "none",
      verification: [],
      depends_on: [],
      responsibility: "Controlled source inspection choice",
    }));
    const project = {
      id: "c".repeat(32),
      definition: {
        name: "Long modal fixture",
        description: "A source-only modal layout fixture",
        owners: ["Team"],
        topology: "monorepo",
        components,
      },
      revision: 1,
      digest: "d".repeat(64),
      updated_at: "2026-10-31T00:00:00Z",
      created_at: "2026-10-31T00:00:00Z",
    };
    const writes: string[] = [];
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      const req = route.request(),
        path = new URL(req.url()).pathname;
      if (req.method() !== "GET") writes.push(path);
      let json: unknown = [];
      if (path.endsWith("/repositories")) json = [repository];
      if (path.endsWith("/projects")) json = [project];
      if (path.endsWith(`/projects/${project.id}`))
        json = {
          ...project,
          components: components.map((component) => ({
            ...component,
            latest_status: "not_started",
            repository_run_count: 0,
            runs: [],
          })),
          counts: { not_started: components.length },
        };
      await route.fulfill({ json });
    });
    await page.setViewportSize({ width: 320, height: 568 });
    await page.goto("/");
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("fixture-modal-bootstrap-at-least-24-chars");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await navigate(page, "Product projects");
    await page
      .getByRole("button", { name: "Open source inspection", exact: true })
      .click();
    const dialog = page.getByRole("dialog", {
        name: "Source inspection review",
        exact: true,
      }),
      fields = dialog.locator(".wizard-field-scroll");
    await expect(dialog.getByRole("checkbox")).toHaveCount(20);
    const geometry = await fields.evaluate((node) => ({
      client: node.clientHeight,
      full: node.scrollHeight,
      overflow: getComputedStyle(node).overflowY,
      wrapper: getComputedStyle(node.parentElement!).overflowY,
    }));
    await writeFile(
      info.outputPath("initial-geometry.json"),
      JSON.stringify(geometry, null, 2),
    );
    await page.screenshot({
      path: info.outputPath(`bounded-modal-initial-${theme}.png`),
    });
    expect(geometry.overflow).toBe("auto");
    expect(geometry.client).toBeLessThan(geometry.full);
    await expect(fields).toHaveAttribute("tabindex", "0");
    await fields.focus();
    await expect(fields).toBeFocused();
    await page.keyboard.press("End");
    await expect
      .poll(() => fields.evaluate((node) => node.scrollTop))
      .toBeGreaterThan(0);
    const settleScroll = () =>
      fields.evaluate(
        (node) =>
          new Promise<void>((resolve, reject) => {
            let previous = node.scrollTop,
              stable = 0,
              frames = 0;
            const frame = () => {
              stable =
                Math.abs(node.scrollTop - previous) < 0.5 ? stable + 1 : 0;
              previous = node.scrollTop;
              if (stable >= 6) resolve();
              else if (++frames > 180)
                reject(new Error("Field scrolling did not settle"));
              else requestAnimationFrame(frame);
            };
            requestAnimationFrame(frame);
          }),
      );
    await settleScroll();
    const endPosition = await fields.evaluate((node) => node.scrollTop);
    await fields.hover();
    await page.mouse.wheel(0, -200);
    await expect
      .poll(() => fields.evaluate((node) => node.scrollTop))
      .toBeLessThan(endPosition);
    await settleScroll();
    const last = dialog.getByRole("checkbox", { name: /Component 20/ });
    await last.focus();
    await expect(last).toBeFocused();
    await settleScroll();
    const lastBox = await last.boundingBox(),
      fieldBox = await fields.boundingBox();
    expect(lastBox!.y).toBeGreaterThanOrEqual(fieldBox!.y);
    expect(lastBox!.y + lastBox!.height).toBeLessThanOrEqual(
      fieldBox!.y + fieldBox!.height + 1,
    );
    const action = dialog.getByRole("button", {
      name: "Inspect configured source",
      exact: true,
    });
    await action.focus();
    await expect(action).toBeFocused();
    const actionBox = await action.boundingBox(),
      dialogBox = await dialog.boundingBox();
    expect(actionBox!.y + actionBox!.height).toBeLessThanOrEqual(
      dialogBox!.y + dialogBox!.height,
    );
    expect(actionBox!.y).toBeGreaterThanOrEqual(dialogBox!.y);
    expect(
      await dialog.evaluate((node) => node.scrollWidth > node.clientWidth + 1),
    ).toBe(false);
    await page.screenshot({
      path: info.outputPath(`bounded-modal-last-control-${theme}.png`),
    });
    expect(writes).toEqual([]);
  });
