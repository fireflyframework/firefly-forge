/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { setup, selectCampaign } from "./campaign-report-history-fixture";
test("selected campaigns expose explicit reporting-plan reads without starting work", async ({
  page,
}) => {
  const state = await setup(page);
  await selectCampaign(page, state);
  await expect(
    page.getByRole("button", { name: "Read reporting plans", exact: true }),
  ).toBeVisible();
  expect(state.writes).toBe(0);
  expect(state.archivePosts).toBe(0);
});
import { setupDelivery, reviewPlan } from "./campaign-delivery-browser-fixture";
import { navigate } from "./navigation";
test("explicit review saves once, exact-reads and derives progress from a selected saved report", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.markerBefore).toBe(true);
  expect(state.posts).toHaveLength(1);
  await expect.poll(() => state.reads).toBe(1);
  expect(state.base.archivePosts).toBe(0);
  expect(state.progressReads).toBe(0);
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Open saved report 1", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Read delivery progress", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Delivery progress: in progress",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.progressReads).toBe(1);
  expect(state.posts).toHaveLength(1);
  expect(state.base.archivePosts).toBe(0);
  expect(state.base.writes).toBe(0);
});
test("lost delivery and reload recover the same plan without replay", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  state.postMode = "lost";
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Original reporting-plan request retained",
      exact: true,
    }),
  ).toBeVisible();
  await page.reload();
  await navigate(page, "Product projects");
  await selectCampaign(page, state.base);
  await page
    .getByRole("button", { name: "Check original reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
  await expect.poll(() => state.reads).toBe(1);
});
for (const status of [404, 409, 422])
  test(`uncertain ${status} read preserves the original identity and blocks a new save`, async ({
    page,
  }) => {
    const state = await setupDelivery(page);
    state.postMode = "lost";
    state.readStatus = status;
    await (
      await reviewPlan(page)
    )
      .getByRole("button", { name: "Save reporting plan", exact: true })
      .click();
    await page
      .getByRole("button", {
        name: "Check original reporting plan",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("button", {
        name: "Review a reporting plan",
        exact: true,
      }),
    ).toBeDisabled();
    await expect(
      page.getByRole("heading", {
        name: "Reporting plan confirmed",
        exact: true,
      }),
    ).toHaveCount(0);
    expect(state.posts).toHaveLength(1);
    expect(
      await page.evaluate(
        () =>
          Object.keys(localStorage).filter((k) =>
            k.startsWith("forge.campaign-delivery-plan.v1:"),
          ).length,
      ),
    ).toBe(1);
  });
test("readers inspect plan history without receiving execution controls", async ({
  page,
}) => {
  const state = await setupDelivery(page, true);
  state.summaries.push(state.fixture.page.items[0]);
  state.receipts.set(state.fixture.receipt.request_id, state.fixture.receipt);
  await expect(
    page.getByRole("button", { name: "Review a reporting plan", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Read reporting plans", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Open reporting plan 1", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(0);
});
test("POST200 followed by missing exact receipt never confirms or permits another identity", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  state.readStatus = 404;
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Original reporting-plan request retained",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Review a reporting plan", exact: true }),
  ).toBeDisabled();
  expect(state.posts).toHaveLength(1);
  await expect.poll(() => state.reads).toBe(1);
});
test("denied exact read clears private plan data while retaining the durable original marker", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  state.readStatus = 403;
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "Campaign reporting plans", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((k) =>
          k.startsWith("forge.campaign-delivery-plan.v1:"),
        ).length,
    ),
  ).toBe(1);
  expect(state.posts).toHaveLength(1);
});
test("a fresh revision uses a checked CAS reference and preserves both original identities", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Review a reporting plan", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Review reporting plan",
    exact: true,
  });
  await dialog
    .getByRole("button", { name: "Continue to milestones", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Review reporting criteria", exact: true })
    .click();
  await dialog
    .getByRole("checkbox", {
      name: "I understand: reporting only.",
      exact: true,
    })
    .check();
  await dialog
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect.poll(() => state.posts.length).toBe(2);
  expect(state.posts[1].expected_previous_digest).toBe(
    state.summaries[0].receipt_digest,
  );
  expect(state.posts[1].request_id).not.toBe(state.posts[0].request_id);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((k) =>
          k.startsWith("forge.campaign-delivery-plan.v1:"),
        ).length,
    ),
  ).toBe(2);
});
for (const viewport of [
  { width: 320, height: 400 },
  { width: 390, height: 600 },
  { width: 600, height: 390 },
  { width: 1440, height: 900 },
])
  for (const theme of ["light", "dark"])
    test(`reporting review painted controls ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      await setupDelivery(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const dialog = await reviewPlan(page);
      await page.evaluate(() => document.fonts.ready);
      const save = dialog.getByRole("button", {
        name: "Save reporting plan",
        exact: true,
      });
      await save.focus();
      await expect
        .poll(() =>
          dialog.evaluate((node) => {
            const rect = node.getBoundingClientRect(),
              body = node.querySelector(".wizard-body")!,
              actions = node.querySelector(".wizard-actions")!,
              a = actions.getBoundingClientRect();
            const buttons = [...actions.querySelectorAll("button")].map((b) => {
              const r = b.getBoundingClientRect();
              const hit = document.elementFromPoint(
                (r.left + r.right) / 2,
                (r.top + r.bottom) / 2,
              );
              return (
                r.top >= rect.top &&
                r.bottom <= rect.bottom + 1 &&
                r.bottom <= innerHeight &&
                r.left >= rect.left &&
                r.right <= rect.right + 1 &&
                (hit === b || b.contains(hit))
              );
            });
            return {
              inside:
                rect.top >= 0 &&
                rect.bottom <= innerHeight + 1 &&
                rect.left >= 0 &&
                rect.right <= innerWidth + 1,
              overflow: body.scrollWidth <= body.clientWidth + 1,
              actions:
                a.top >= rect.top &&
                a.bottom <= rect.bottom + 1 &&
                buttons.every(Boolean),
            };
          }),
        )
        .toEqual({ inside: true, overflow: true, actions: true });
      await expect(save).toBeFocused();
      const ackLabel = dialog
        .getByRole("checkbox", {
          name: "I understand: reporting only.",
          exact: true,
        })
        .locator("..");
      await ackLabel.scrollIntoViewIfNeeded();
      await expect
        .poll(() =>
          ackLabel.evaluate((node) => {
            const r = node.getBoundingClientRect(),
              field = node
                .closest(".wizard-field-scroll")!
                .getBoundingClientRect();
            return r.top >= field.top - 1 && r.bottom <= field.bottom + 1;
          }),
        )
        .toBe(true);

      await page.screenshot({
        path: info.outputPath(`review-${viewport.width}-${theme}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByRole("button", {
          name: "Review a reporting plan",
          exact: true,
        }),
      ).toBeFocused();
    });
test("a late progress result cannot restore a saved report selection that has been cleared", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Open saved report 1", exact: true })
    .click();
  let release!: () => void;
  state.progressGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page
    .getByRole("button", { name: "Read delivery progress", exact: true })
    .click();
  await expect.poll(() => state.progressReads).toBe(1);
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  release();
  await expect(
    page.getByText(
      "Select an exact saved report in report history before reading delivery progress. No report is archived automatically.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("region", {
      name: "Saved-cut delivery progress",
      exact: true,
    }),
  ).toHaveCount(0);
});
test("a changed confirmed local binding blocks review and sends no replacement request", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  await page.evaluate(() => {
    const key = Object.keys(localStorage).find((k) =>
      k.startsWith("forge.campaign-delivery-plan.v1:"),
    )!;
    const m = JSON.parse(localStorage.getItem(key)!);
    m.inputDigest = "9".repeat(64);
    localStorage.setItem(key, JSON.stringify(m));
  });
  await page
    .getByRole("button", { name: "Review a reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Review reporting plan", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "Original reporting-plan request retained",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
});
test("a late exact response after campaign navigation cannot restore private plan data", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  let release!: () => void;
  state.receiptGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await (
    await reviewPlan(page)
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect.poll(() => state.reads).toBe(1);
  await page.getByLabel("Campaign", { exact: true }).selectOption("");
  release();
  await expect(
    page.getByRole("region", { name: "Campaign reporting plans", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(1);
});
test("review stays keyboard operable at 200 percent zoom", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await setupDelivery(page);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  const dialog = await reviewPlan(page),
    save = dialog.getByRole("button", {
      name: "Save reporting plan",
      exact: true,
    });
  await save.focus();
  await expect(save).toBeFocused();
  await expect
    .poll(() =>
      save.evaluate((node) => {
        const r = node.getBoundingClientRect();
        const hit = document.elementFromPoint(
          (r.left + r.right) / 2,
          (r.top + r.bottom) / 2,
        );
        return (
          r.top >= 0 &&
          r.bottom <= innerHeight &&
          r.left >= 0 &&
          r.right <= innerWidth &&
          (hit === node || node.contains(hit))
        );
      }),
    )
    .toBe(true);
  await page.screenshot({ path: info.outputPath("review-zoom200.png") });
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Review a reporting plan", exact: true }),
  ).toBeFocused();
});
test("test campaigns expose only their frozen task phases and save explicit task conditions", async ({
  page,
}) => {
  const state = await setupDelivery(page);
  await (
    await reviewPlan(page, "prepare")
  )
    .getByRole("button", { name: "Save reporting plan", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Reporting plan confirmed",
      exact: true,
    }),
  ).toBeVisible();
  const body = state.posts[0] as {
    milestones: { conditions: { kind: string; task_id: string }[] }[];
  };
  expect(body.milestones[0].conditions[0]).toMatchObject({
    kind: "task_receipted",
    task_id: "prepare",
  });
  expect(state.posts).toHaveLength(1);
  expect(state.base.writes).toBe(0);
});
