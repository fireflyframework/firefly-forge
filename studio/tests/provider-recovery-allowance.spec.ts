/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import {
  setup,
  openPlan,
  open,
  mitigation,
  review,
  prefix,
} from "./provider-recovery-allowance-browser-fixture";
import {
  allowanceCandidate,
  allowanceInput,
} from "../src/ProviderRecoveryAllowanceData";
import { candidate, context } from "./provider-recovery-allowance-fixture";
import { captureWalkthrough } from "./walkthrough-capture";
test("native dialog boundaries cannot exempt another producer or an unrelated stage", async ({
  page,
}) => {
  for (const [producer, stage] of [
    ["studio/tests/docs-walkthrough.spec.ts", "provider-allowance-review"],
    ["studio/tests/docs-provider-allowance.spec.ts", "run-review"],
  ])
    await expect(
      captureWalkthrough(page, stage, "light", "desktop", {
        producer,
        boundary: "dialog",
      }),
    ).rejects.toThrow("Unknown final allowance dialog capture boundary");
  await expect(
    captureWalkthrough(page, "provider-allowance-review", "light", "desktop", {
      producer: "studio/tests/docs-provider-allowance.spec.ts",
      boundary: "dialog",
    }),
  ).rejects.toThrow("Final allowance captures require a visible native modal");
});
for (const mode of ["success", "denied", "wrong-actor"]) {
  test(`lost local marker reads only the existing native reservation: ${mode}`, async ({
    page,
  }, info) => {
    const state = await setup(page, mode);
    state.body = allowanceInput(
      allowanceCandidate(candidate, context),
      "a".repeat(32),
      "Reviewed locally.",
      "e".repeat(64),
    );
    state.run.attempt = 5;
    state.run.provider_recovery = {
      request_id: state.body.request_id,
      allowance_id: state.body.request_id,
      allowance_digest: "c".repeat(64),
      attempt: 5,
    };
    if (mode === "denied") state.readStatus = 403;
    const original = JSON.stringify(state.body);
    await page.reload();
    await openPlan(page);
    await expect(
      page.getByRole("alert").filter({
        hasText:
          "Execution is unavailable. Inspect interrupted work before restarting the factory.",
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Review final allowance", exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("button", { name: "Read existing reservation", exact: true })
      .click();
    if (mode === "success") {
      await expect(
        page.getByRole("heading", {
          name: "Final allowance reserved",
          exact: true,
        }),
      ).toBeVisible();
    } else {
      await expect(
        page.getByRole("heading", {
          name: "Final allowance reserved",
          exact: true,
        }),
      ).toHaveCount(0);
      const alert = page
        .getByRole("region", { name: "Final provider allowance", exact: true })
        .getByRole("alert");
      await expect(alert).toBeVisible();
      await expect(alert).toHaveText(
        mode === "denied"
          ? "Current administrator access could not be confirmed. Any saved request remains held for authorized readback."
          : "The reservation is not confirmed. Its exact request is saved. Check that receipt before taking another action.",
      );
    }
    expect(state.reads).toBe(1);
    expect(state.posts).toBe(0);
    expect(state.candidateReads).toBe(0);
    expect(JSON.stringify(state.body)).toBe(original);
    expect(
      await page.evaluate(
        (key) =>
          Object.keys(localStorage).filter((name) => name.startsWith(key)),
        prefix,
      ),
    ).toEqual([]);
    await page.screenshot({ path: info.outputPath(`read-only-${mode}.png`) });
  });
}
test("persisted request precedes one POST; exact GET confirms and later403 clears confirmation", async ({
  page,
}) => {
  const state = await setup(page);
  await review(page);
  await page
    .getByRole("button", { name: "Reserve final allowance", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Final allowance reserved",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  expect(state.reads).toBe(1);
  expect(state.body?.adopt_current_executable).toBe(true);
  state.readStatus = 403;
  await page
    .getByRole("button", { name: "Check saved reservation", exact: true })
    .click();
  await expect(
    page.getByText(/Current administrator access could not be confirmed/),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Final allowance reserved",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toBe(1);
});
test("lost response and404 retain the exact ID across reload with GET only", async ({
  page,
}) => {
  const state = await setup(page, "lost");
  await review(page);
  await page
    .getByRole("button", { name: "Reserve final allowance", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Check saved reservation", exact: true }),
  ).toBeVisible();
  const original = structuredClone(state.body);
  await page.reload();
  await openPlan(page);
  await page
    .getByRole("button", { name: "Check saved reservation", exact: true })
    .click();
  await expect(
    page.getByText(/No matching receipt is available yet/),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  expect(state.body).toEqual(original);
  state.readStatus = 200;
  await page
    .getByRole("button", { name: "Check saved reservation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Final allowance reserved",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
test("changed candidate and failed storage never send a reservation", async ({
  page,
}) => {
  const state = await setup(page);
  await review(page);
  state.candidate.digest = "8".repeat(64);
  await page
    .getByRole("button", { name: "Reserve final allowance", exact: true })
    .click();
  await expect(
    page.getByText(/The reviewed source, executable or failure changed/),
  ).toBeVisible();
  expect(state.posts).toBe(0);
  await page
    .getByRole("button", { name: "Close final allowance review", exact: true })
    .click();
  await review(page);
  await page.evaluate((key) => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (name, value) {
      if (name.startsWith(key)) throw new Error("Controlled storage refusal");
      return original.call(this, name, value);
    };
  }, prefix);
  await page
    .getByRole("button", { name: "Reserve final allowance", exact: true })
    .click();
  await expect(
    page.getByText(/Request storage could not be verified/),
  ).toBeVisible();
  expect(state.posts).toBe(0);
});
for (const mode of ["denied", "wrong-actor"])
  test(`${mode} never confirms the allowance or offers another ID`, async ({
    page,
  }) => {
    const state = await setup(page, mode);
    await review(page);
    await page
      .getByRole("button", { name: "Reserve final allowance", exact: true })
      .click();
    await expect(
      page.getByRole("button", {
        name: "Check saved reservation",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", {
        name: "Final allowance reserved",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Review final allowance", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByText(/private .* canary/)).toHaveCount(0);
    expect(state.posts).toBe(1);
  });
test("cross-tab saved identity closes the review and storage deletion cannot reset it", async ({
  page,
}) => {
  const state = await setup(page);
  await review(page);
  await page
    .getByRole("button", { name: "Reserve final allowance", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Check saved reservation", exact: true }),
  ).toBeVisible();
  await page.evaluate((key) => {
    const name = Object.keys(localStorage).find((name) =>
      name.startsWith(key),
    )!;
    localStorage.removeItem(name);
    dispatchEvent(new StorageEvent("storage", { key: name, newValue: null }));
  }, prefix);
  await expect(
    page.getByRole("button", { name: "Review final allowance", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Check saved reservation", exact: true }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 744 },
  { width: 844, height: 390 },
  { width: 720, height: 450 },
])
  for (const theme of ["light", "dark"])
    test(`wizard grid and keyboard ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      const state = await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await open(page);
      await page.evaluate(() => document.fonts.ready.then(() => undefined));
      await page.screenshot({ path: info.outputPath("failed-attempt.png") });
      await mitigation(page);
      await page.screenshot({ path: info.outputPath("mitigation.png") });
      await page
        .getByRole("button", { name: "Review reservation", exact: true })
        .click();
      await page
        .getByLabel(
          "I authorize this one final reservation and understand it cannot be renewed.",
          { exact: true },
        )
        .check();
      const dialog = page.getByRole("dialog", {
        name: "Reserve a final provider allowance",
        exact: true,
      });
      const action = dialog.getByRole("button", {
        name: "Reserve final allowance",
        exact: true,
      });
      await action.scrollIntoViewIfNeeded();
      await expect(action).toBeEnabled();
      await expect(action).toBeVisible();
      const bounds = await action.boundingBox();
      expect(bounds).not.toBeNull();
      expect(bounds!.height).toBeGreaterThanOrEqual(36);
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
      expect(bounds!.y).toBeGreaterThanOrEqual(0);
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
      expect(
        await action.evaluate((node) => {
          const r = node.getBoundingClientRect();
          return node.contains(
            document.elementFromPoint(
              r.left + r.width / 2,
              r.top + r.height / 2,
            ),
          );
        }),
      ).toBe(true);
      expect(
        await action.evaluate((node) => getComputedStyle(node).fontFamily),
      ).toContain("Manrope");
      expect(
        await dialog.evaluate(
          (node) => node.scrollWidth <= node.clientWidth + 1,
        ),
      ).toBe(true);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({ path: info.outputPath("review.png") });
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByRole("button", {
          name: "Review final allowance",
          exact: true,
        }),
      ).toBeFocused();
      expect(state.posts).toBe(0);
    });

for (const theme of ["light", "dark"])
  test(`actual CSS zoom 200 percent ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await setup(page);
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await page.evaluate(() => {
      document.documentElement.style.zoom = "2";
    });
    await review(page);
    const dialog = page.getByRole("dialog", {
      name: "Reserve a final provider allowance",
      exact: true,
    });
    const action = dialog.getByRole("button", {
      name: "Reserve final allowance",
      exact: true,
    });
    await action.scrollIntoViewIfNeeded();
    const bounds = await action.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(1440);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(900);
    expect(
      await action.evaluate((node) => {
        const r = node.getBoundingClientRect();
        return node.contains(
          document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2),
        );
      }),
    ).toBe(true);
    expect(
      await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
    ).toBe(true);
    await page.screenshot({ path: info.outputPath("review-css-zoom-200.png") });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(state.posts).toBe(0);
  });

test("keyboard confirmation and Escape while POST is pending retain one exact identity", async ({
  page,
}, info) => {
  const state = await setup(page, "pending");
  await open(page);
  await mitigation(page);
  await page
    .getByRole("button", { name: "Review reservation", exact: true })
    .click();
  const ack = page.getByLabel(
    "I authorize this one final reservation and understand it cannot be renewed.",
    { exact: true },
  );
  await page.keyboard.press("Tab");
  await expect(ack).toBeFocused();
  await page.keyboard.press("Space");
  await expect(ack).toBeChecked();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Back", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Reserve final allowance", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect.poll(() => state.posts).toBe(1);
  await expect(
    page.getByRole("heading", {
      name: "Exact reservation request saved",
      exact: true,
    }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("reservation-post-pending.png"),
  });
  await page.keyboard.press("Escape");
  const saved = await page.evaluate(
    (key) =>
      Object.entries(localStorage).find(([name]) => name.startsWith(key))?.[1],
    prefix,
  );
  expect(JSON.parse(saved!).body).toEqual(state.body);
  state.releasePost();
  await expect(
    page.getByRole("button", { name: "Check saved reservation", exact: true }),
  ).toBeEnabled();
  expect(state.posts).toBe(1);
});
test("a real second tab adopts saved readback and closes its competing draft without POST", async ({
  page,
}) => {
  const first = await setup(page, "lost");
  const other = await page.context().newPage();
  const second = await setup(other, "lost", true);
  await review(other);
  await review(page);
  await page
    .getByRole("button", { name: "Reserve final allowance", exact: true })
    .click();
  await expect(other.getByRole("dialog")).toHaveCount(0);
  await expect(
    other.getByRole("button", { name: "Check saved reservation", exact: true }),
  ).toBeVisible();
  expect(first.posts).toBe(1);
  expect(second.posts).toBe(0);
  await other.close();
});
