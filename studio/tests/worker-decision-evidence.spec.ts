/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { navigate } from "./navigation";
import { expect, test } from "@playwright/test";
import { setup } from "./worker-decision-evidence-fixture";
import { captureWalkthrough } from "./walkthrough-capture";
test("screenshot modal boundaries apply only to the registered evidence preview stage", async ({
  page,
}) => {
  for (const [producer, stage] of [
    ["studio/tests/docs-provider-allowance.spec.ts", "agent-question-preview"],
    ["studio/tests/docs-walkthrough.spec.ts", "agent-question-retained"],
  ]) {
    await expect(
      captureWalkthrough(page, stage, "light", "desktop", {
        producer,
        boundary: "dialog",
      }),
    ).rejects.toThrow("Unknown final allowance dialog capture boundary");
  }
  await expect(
    captureWalkthrough(page, "agent-question-preview", "light", "desktop", {
      producer: "studio/tests/docs-walkthrough.spec.ts",
      boundary: "dialog",
    }),
  ).rejects.toThrow("require a visible native modal");
  for (const [producer, stage] of [
    ["studio/tests/docs-provider-allowance.spec.ts", "agent-question-retained"],
    ["studio/tests/docs-walkthrough.spec.ts", "agent-question-evidence-review"],
  ]) {
    await expect(
      captureWalkthrough(page, stage, "light", "desktop", {
        producer,
        framing: "initial",
      }),
    ).rejects.toThrow("Unknown configured-check initial frame");
  }
});
test("authenticated question deep link keeps the decision paused and opens only verified retained images", async ({
  page,
}) => {
  const state = await setup(page);
  await expect(
    page.getByRole("heading", { name: "Waiting for your answer", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Resume run", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Hand off run", exact: true }),
  ).toHaveCount(0);
  expect(state.images).toBe(0);
  await page.getByText("Retained screenshots", { exact: true }).click();
  const open = page.getByRole("button", { name: "View image", exact: true });
  await open.click();
  const dialog = page.getByRole("dialog", {
    name: "Retained screenshot",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("img")).toHaveJSProperty("complete", true);
  await expect(dialog.getByRole("img")).toHaveJSProperty("naturalWidth", 1000);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(open).toBeFocused();
  expect(state.images).toBe(1);
  expect(state.writes).toBe(0);
  expect(state.runs).toBeGreaterThan(0);
  await page.reload();
  await expect(
    page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByLabel("Your answer", { exact: true })).toBeVisible();
  expect(state.writes).toBe(0);
});
test("current access denial closes preview without a second image request or an execution", async ({
  page,
}) => {
  const state = await setup(page);
  state.deny = true;
  await page.getByText("Retained screenshots", { exact: true }).click();
  await page.getByRole("button", { name: "View image", exact: true }).click();
  await expect(
    page
      .getByRole("group", { name: "Retained question screenshots" })
      .getByRole("alert"),
  ).toContainText("current project access");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.images).toBe(1);
  expect(state.writes).toBe(0);
});
test("reopening a retained image rechecks current access rather than trusting an earlier blob", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByText("Retained screenshots", { exact: true }).click();
  const open = page.getByRole("button", { name: "View image", exact: true });
  await open.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  state.deny = true;
  await open.click();
  await expect(
    page
      .getByRole("group", { name: "Retained question screenshots" })
      .getByRole("alert"),
  ).toContainText("current project access");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.images).toBe(2);
  expect(state.writes).toBe(0);
});
test("a project viewer can inspect retained evidence but cannot answer or resume", async ({
  page,
}) => {
  const state = await setup(page, true);
  await expect(page.getByLabel("Your answer", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Review answer", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Resume run", exact: true }),
  ).toHaveCount(0);
  await page.getByText("Retained screenshots", { exact: true }).click();
  await page.getByRole("button", { name: "View image", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  expect(state.writes).toBe(0);
});
test("communication selection preserves other tasks and resolves a lost save by readback without retry", async ({
  page,
}) => {
  const state = await setup(page);
  state.lostSave = true;
  await navigate(page, "Connections");
  await expect(
    page.getByRole("heading", { name: "Project updates", exact: true }),
  ).toBeVisible();
  const selection = page.getByLabel("Communication profile", { exact: true });
  expect((await selection.boundingBox())!.height).toBeGreaterThanOrEqual(43);
  await selection.selectOption("updates");
  await page
    .getByRole("button", { name: "Save communication profile", exact: true })
    .click();
  await expect(selection).toHaveValue("updates");
  await expect(page.getByText(/No automatic retry was sent/)).toBeVisible();
  expect(state.saves).toBe(1);
  expect(state.writes).toBe(0);
  expect(state.route.bindings).toEqual({
    implementation: "coding",
    communication: "updates",
  });
  await page.reload();
  await navigate(page, "Connections");
  await expect(
    page.getByLabel("Communication profile", { exact: true }),
  ).toHaveValue("updates");
  expect(state.saves).toBe(1);
});
test("a revoked project read permission holds the linked question without loading screenshots or sending actions", async ({
  page,
}) => {
  const state = await setup(page);
  state.denyRun = true;
  await page.reload();
  await expect(
    page.getByRole("alert").filter({ hasText: "Current project read access" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Resume run", exact: true }),
  ).toHaveCount(0);
  expect(state.images).toBe(0);
  expect(state.writes).toBe(0);
});
test("a declined communication change is reported as a refusal and never retried", async ({
  page,
}) => {
  const state = await setup(page);
  state.refuseSave = true;
  await navigate(page, "Connections");
  await page
    .getByLabel("Communication profile", { exact: true })
    .selectOption("updates");
  await page
    .getByRole("button", { name: "Save communication profile", exact: true })
    .click();
  await expect(
    page.getByRole("alert").filter({ hasText: "server declined this change" }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Communication profile", { exact: true }),
  ).toHaveValue("");
  expect(state.saves).toBe(1);
  expect(state.route.bindings).toEqual({ implementation: "coding" });
  expect(state.writes).toBe(0);
});
const frames = [
  { name: "desktop-light", width: 1440, height: 1000, theme: "light" },
  { name: "desktop-dark", width: 1440, height: 900, theme: "dark" },
  { name: "phone-light", width: 390, height: 844, theme: "light" },
  { name: "phone-dark", width: 390, height: 844, theme: "dark" },
  { name: "narrow-short-dark", width: 320, height: 568, theme: "dark" },
  { name: "short-light", width: 1280, height: 320, theme: "light" },
  {
    name: "zoom-200-light",
    width: 1440,
    height: 900,
    theme: "light",
    zoom: true,
  },
];
for (const frame of frames)
  test(`decision controls and full-size evidence remain usable ${frame.name}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width: frame.width, height: frame.height });
    const state = await setup(page);
    await page
      .getByLabel("Appearance", { exact: true })
      .selectOption(frame.theme);
    if (frame.width <= 390) {
      const rows = await page
        .getByRole("toolbar", { name: "Markdown formatting", exact: true })
        .getByRole("button")
        .evaluateAll((buttons) =>
          buttons.map((button) => button.getBoundingClientRect().top),
        );
      expect(rows).toHaveLength(6);
      expect(new Set(rows).size).toBe(1);
      await page
        .getByRole("button", { name: "Preview description", exact: true })
        .click();
      await expect(
        page.getByRole("region", {
          name: "Formatted description preview",
          exact: true,
        }),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Edit description", exact: true })
        .click();
      await expect(
        page.getByLabel("Your answer", { exact: true }),
      ).toBeVisible();
    }
    if (frame.zoom) {
      await page.evaluate(() => {
        document.documentElement.style.zoom = "2";
      });
      expect(
        await page.evaluate(
          () => getComputedStyle(document.documentElement).zoom,
        ),
      ).toBe("2");
    }
    const question = page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    });
    await question.scrollIntoViewIfNeeded();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    const proof = process.env.FORGE_UI_PROOF_DIR;
    if (proof) {
      mkdirSync(proof, { recursive: true });
      await page.screenshot({
        path: join(proof, frame.name + "-question.png"),
      });
    }
    await page.getByText("Retained screenshots", { exact: true }).click();
    const open = page.getByRole("button", { name: "View image", exact: true });
    await open.click();
    const dialog = page.getByRole("dialog", {
      name: "Retained screenshot",
      exact: true,
    });
    await expect(dialog).toBeVisible();
    const close = dialog.getByRole("button", {
      name: "Close screenshot",
      exact: true,
    });
    await expect(close).toBeFocused();
    const bounds = await dialog.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(frame.width + 1);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(frame.height + 1);
    if (proof)
      await page.screenshot({ path: join(proof, frame.name + "-preview.png") });
    await dialog
      .getByRole("button", { name: "View original size", exact: true })
      .click();
    const canvas = dialog.getByRole("region", {
      name: "Screenshot image",
      exact: true,
    });
    await canvas.focus();
    if (frame.width <= 390 || frame.zoom) {
      const before = await canvas.evaluate((node) => ({
        left: node.scrollLeft,
        width: node.scrollWidth,
        visible: node.clientWidth,
      }));
      expect(before.width).toBeGreaterThan(before.visible);
      await page.keyboard.press("ArrowRight");
      await expect
        .poll(() => canvas.evaluate((node) => node.scrollLeft))
        .toBeGreaterThan(before.left);
    }
    await dialog
      .getByRole("button", { name: "Fit to screen", exact: true })
      .click();
    await page.keyboard.press("Escape");
    await expect(open).toBeFocused();
    await page.getByRole("button", { name: "14 items", exact: true }).click();
    const review = page.getByRole("button", {
      name: "Review answer",
      exact: true,
    });
    await review.scrollIntoViewIfNeeded();
    await review.focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("heading", { name: "Review your answer", exact: true }),
    ).toBeFocused();
    const send = page.getByRole("button", { name: "Send answer", exact: true });
    await send.scrollIntoViewIfNeeded();
    const visible = await send.evaluate((node) => {
      const box = node.getBoundingClientRect(),
        footer = document
          .querySelector(".workspace-main > footer")
          ?.getBoundingClientRect();
      return {
        top: box.top,
        bottom: box.bottom,
        edge: Math.min(innerHeight, footer?.top ?? innerHeight),
        hit: node.contains(
          document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          ),
        ),
      };
    });
    expect(visible.top).toBeGreaterThanOrEqual(0);
    expect(visible.bottom).toBeLessThanOrEqual(visible.edge + 1);
    expect(visible.hit).toBe(true);
    await send.focus();
    await expect(send).toBeFocused();
    if (proof) {
      const path = join(proof, frame.name + "-answer-review.png");
      await page.screenshot({ path });
      await info.attach("Controlled question fixture: review action", { path });
    }
    expect(state.writes).toBe(0);
  });
