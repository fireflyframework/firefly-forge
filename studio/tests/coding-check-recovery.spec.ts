/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import {
  prepare,
  open,
  scope,
  grant,
  review,
} from "./coding-check-recovery-browser-fixture";
import {
  setup,
  openPlan,
  prefix,
  review as legacyReview,
} from "./provider-recovery-allowance-browser-fixture";
import { captureWalkthrough } from "./walkthrough-capture";
test("configured-check modal boundaries cannot exempt another producer or stage", async ({
  page,
}) => {
  for (const [producer, stage] of [
    ["studio/tests/docs-walkthrough.spec.ts", "coding-check-review"],
    ["studio/tests/docs-coding-check-recovery.spec.ts", "run-review"],
    ["studio/tests/docs-provider-allowance.spec.ts", "coding-check-review"],
  ])
    await expect(
      captureWalkthrough(page, stage, "light", "desktop", {
        producer,
        boundary: "dialog",
      }),
    ).rejects.toThrow("Unknown final allowance dialog capture boundary");
  await expect(
    captureWalkthrough(page, "coding-check-review", "light", "desktop", {
      producer: "studio/tests/docs-coding-check-recovery.spec.ts",
      boundary: "dialog",
    }),
  ).rejects.toThrow("Final allowance captures require a visible native modal");
});
async function actionFits(page: Page) {
  const dialog = page.getByRole("dialog"),
    action = dialog.locator(".wizard-actions button").last();
  await action.scrollIntoViewIfNeeded();
  await expect(action).toBeVisible();
  const proof = await action.evaluate((node) => {
    const box = node.getBoundingClientRect(),
      modal = node.closest("dialog")!.getBoundingClientRect();
    return {
      inside:
        box.left >= Math.max(0, modal.left) &&
        box.right <= Math.min(innerWidth, modal.right) &&
        box.top >= Math.max(0, modal.top) &&
        box.bottom <= Math.min(innerHeight, modal.bottom),
      hit: node.contains(
        document.elementFromPoint(
          box.left + box.width / 2,
          box.top + box.height / 2,
        ),
      ),
      height: box.height,
      font: getComputedStyle(node).fontFamily,
      unclipped: node.scrollWidth <= node.clientWidth + 1,
    };
  });
  expect(proof.inside).toBe(true);
  expect(proof.hit).toBe(true);
  expect(proof.height).toBeGreaterThanOrEqual(44);
  expect(proof.font).toContain("Manrope");
  expect(proof.unclipped).toBe(true);
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
  ).toBe(true);
}
async function topFits(page: Page) {
  const dialog = page.getByRole("dialog");
  for (const selector of [
    ".access-dialog-heading",
    ".wizard-steps",
    ".wizard-heading h2",
  ]) {
    await expect
      .poll(
        () =>
          dialog.locator(selector).evaluate((node) => {
            const b = node.getBoundingClientRect(),
              d = node.closest("dialog")!.getBoundingClientRect();
            return (
              b.top >= Math.max(0, d.top) &&
              b.bottom <= Math.min(innerHeight, d.bottom)
            );
          }),
        { message: selector + " is fully visible at step entry" },
      )
      .toBe(true);
  }
}

test("step transitions reset the owned dialog and keep the title and progress fully visible", async ({
  page,
}) => {
  await prepare(page);
  await open(page);
  await topFits(page);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await topFits(page);
});
test("explicit grant then linked reservation retains both exact identities and never auto-posts", async ({
  page,
}) => {
  const state = await prepare(page);
  await review(page);
  expect(state.grantPosts).toBe(1);
  expect(state.allowancePosts).toBe(0);
  await page
    .getByRole("button", { name: "Reserve final attempt", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByRole("heading", { name: "Final recovery reserved", exact: true }),
  ).toBeVisible();
  expect(state.allowancePosts).toBe(1);
  expect(state.grantReads).toBe(2);
  expect(state.currentReads).toBe(3);
});
for (const mode of ["grant-lost", "wrong-actor"])
  test(`${mode} holds without a second grant or CLI-only fallback`, async ({
    page,
  }, info) => {
    const state = await prepare(page, mode);
    await grant(page);
    await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
    await actionFits(page);
    await page.screenshot({
      path: info.outputPath("unknown-grant-response.png"),
    });
    expect(state.grantPosts).toBe(1);
    expect(state.allowancePosts).toBe(0);
    await page
      .getByRole("button", {
        name: "Close configured-check recovery",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", { name: "Check saved grant", exact: true })
      .click();
    if (mode === "grant-lost")
      await expect(
        page.getByRole("heading", {
          name: "Review the recovery mitigation",
          exact: true,
        }),
      ).toBeVisible();
    else
      await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
    expect(state.grantPosts).toBe(1);
    expect(state.allowancePosts).toBe(0);
  });
test("revoked or mismatched active seal refuses final reserve with no extra POST", async ({
  page,
}) => {
  const state = await prepare(page);
  await review(page);
  state.currentStatus = "revoked";
  await page
    .getByRole("button", { name: "Reserve final attempt", exact: true })
    .click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  expect(state.allowancePosts).toBe(0);
  expect(state.grantPosts).toBe(1);
});
test("Escape while the grant POST is pending preserves its exact marker and GET-only recovery", async ({
  page,
}) => {
  const state = await prepare(page, "grant-pending");
  await grant(page);
  await expect.poll(() => state.grantPosts).toBe(1);
  await page.keyboard.press("Escape");
  state.release();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Check saved grant", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review the recovery mitigation",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.grantPosts).toBe(1);
  expect(state.allowancePosts).toBe(0);
});
for (const theme of ["light", "dark"])
  for (const viewport of [
    { width: 1440, height: 844 },
    { width: 1280, height: 720 },
    { width: 390, height: 844 },
  ])
    test(`aligned four steps ${theme} ${viewport.width}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (t) => localStorage.setItem("forge.appearance", t),
        theme,
      );
      const state = await prepare(page);
      await open(page);
      for (const stage of [0, 1, 2, 3]) {
        const dialog = page.getByRole("dialog");
        await expect(dialog).toBeVisible();
        if (stage === 0) await topFits(page);
        await actionFits(page);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: info.outputPath(`step-${stage}-action.png`),
        });
        await dialog.evaluate((node) => {
          node.scrollTop = 0;
        });
        await topFits(page);
        await page.screenshot({
          path: info.outputPath(`step-${stage}-initial.png`),
        });
        if (stage === 0) {
          await page
            .getByRole("button", { name: "Continue", exact: true })
            .click();
          await topFits(page);
          await page
            .getByLabel("Reason for granting check access")
            .fill("Reviewed frozen checks and runner limits.");
        }
        if (stage === 1) {
          await page
            .getByRole("button", { name: "Grant selected checks", exact: true })
            .click();
          await expect(
            page.getByRole("heading", {
              name: "Review the recovery mitigation",
              exact: true,
            }),
          ).toBeVisible();
          await topFits(page);
          await page
            .getByLabel("Mitigation reviewed")
            .fill("Reviewed provider mitigation.");
          await page
            .getByLabel("Mitigation evidence SHA-256")
            .fill("e".repeat(64));
          await page
            .getByLabel(
              "Adopt the reviewed current CLI for this final recovery.",
              { exact: true },
            )
            .check();
        }
        if (stage === 2) {
          await page
            .getByRole("button", { name: "Review final recovery", exact: true })
            .click();
          await topFits(page);
        }
      }
      expect(state.grantPosts).toBe(1);
      expect(state.allowancePosts).toBe(0);
    });
for (const theme of ["light", "dark"])
  test(`actual CSS200 keyboard final review ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const state = await prepare(page);
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await page.evaluate(() => {
      document.documentElement.style.zoom = "2";
    });
    await review(page);
    await actionFits(page);
    const action = page.getByRole("button", {
      name: "Reserve final attempt",
      exact: true,
    });
    await action.focus();
    await expect(action).toBeFocused();
    await page.screenshot({ path: info.outputPath("review-css200.png") });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(state.allowancePosts).toBe(0);
  });
test("simultaneous legacy and configured-check tabs retain one lifetime reservation identity", async ({
  page,
}) => {
  const first = await prepare(page);
  const other = await page.context().newPage();
  const second = await setup(other, "lost", true);
  await legacyReview(other);
  await scope(page);
  await Promise.allSettled([
    page
      .getByRole("button", { name: "Grant selected checks", exact: true })
      .click(),
    other
      .getByRole("button", { name: "Reserve final allowance", exact: true })
      .click(),
  ]);
  await expect.poll(() => first.grantPosts + second.posts).toBe(1);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  expect(first.allowancePosts).toBe(0);
  expect(first.grantPosts + second.posts).toBe(1);
  expect(
    await page.evaluate(
      (key) =>
        Object.keys(localStorage).filter((k) => k.startsWith(key)).length,
      prefix,
    ),
  ).toBe(1);
  await other.close();
});
test("saved ambiguous final reservation survives reload using exact GET only", async ({
  page,
}) => {
  const state = await prepare(page, "final-lost");
  await review(page);
  await page
    .getByRole("button", { name: "Reserve final attempt", exact: true })
    .click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  await page.reload();
  await openPlan(page);
  await page
    .getByRole("button", { name: "Check saved final reservation", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByRole("heading", { name: "Final recovery reserved", exact: true }),
  ).toBeVisible();
  expect(state.grantPosts).toBe(1);
  expect(state.allowancePosts).toBe(1);
});
test("cross-tab deletion cannot erase a retained grant or start another identity", async ({
  page,
}) => {
  const state = await prepare(page);
  await grant(page);
  await expect(
    page.getByRole("heading", {
      name: "Review the recovery mitigation",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", {
      name: "Close configured-check recovery",
      exact: true,
    })
    .click();
  await page.evaluate((key) => {
    const k = Object.keys(localStorage).find((k) => k.startsWith(key))!;
    localStorage.removeItem(k);
    window.dispatchEvent(
      new StorageEvent("storage", { key: k, newValue: null }),
    );
  }, prefix);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(
    page.getByRole("button", {
      name: "Prepare recovery with configured checks",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.grantPosts).toBe(1);
  expect(state.allowancePosts).toBe(0);
});

for (const stateName of ["active", "revoked", "expired", "unknown", "denied"])
  test(`unconfirmed current ${stateName} blocks preparation without a new identity`, async ({
    page,
  }) => {
    const state = await prepare(page, "current-" + stateName);
    await page
      .getByRole("button", {
        name: "Prepare recovery with configured checks",
        exact: true,
      })
      .click();
    await expect(
      page
        .getByRole("region", {
          name: "Recovery with configured checks",
          exact: true,
        })
        .getByText(/An existing or unconfirmed configured-check grant/),
    ).toBeVisible();
    expect(state.grantPosts).toBe(0);
    expect(state.allowancePosts).toBe(0);
    expect(state.candidateReads).toBe(0);
    expect(
      await page.evaluate(
        (key) =>
          Object.keys(localStorage).filter((k) => k.startsWith(key)).length,
        prefix,
      ),
    ).toBe(0);
  });
test("cleared storage and reload hold the existing server grant with no replacement candidate or POST", async ({
  page,
}) => {
  const state = await prepare(page);
  await grant(page);
  await expect(
    page.getByRole("heading", {
      name: "Review the recovery mitigation",
      exact: true,
    }),
  ).toBeVisible();
  await page.evaluate(
    (key) =>
      Object.keys(localStorage)
        .filter((k) => k.startsWith(key))
        .forEach((k) => localStorage.removeItem(k)),
    prefix,
  );
  state.grantPosts = 0;
  state.candidateReads = 0;
  await page.reload();
  await openPlan(page);
  await page
    .getByRole("button", {
      name: "Prepare recovery with configured checks",
      exact: true,
    })
    .click();
  await expect(
    page
      .getByRole("region", {
        name: "Recovery with configured checks",
        exact: true,
      })
      .getByText(/An existing or unconfirmed configured-check grant/),
  ).toBeVisible();
  expect(state.grantPosts).toBe(0);
  expect(state.allowancePosts).toBe(0);
  expect(state.candidateReads).toBe(0);
});
