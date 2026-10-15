/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import {
  setup,
  selectCampaign,
  reviewSave,
  native,
  verifiedReport,
  projectId,
  campaignId,
  digest,
} from "./campaign-report-history-fixture";
test("saved reports are an explicit read and preserve the original historical actor", async ({
  page,
}) => {
  const state = await setup(page);
  await selectCampaign(page, state);
  expect(state.archiveLists).toBe(0);
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  await expect(
    page.getByText("1 saved report in this page.", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Open saved report 1", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Prepared by the server", { exact: true }),
  ).toBeVisible();
  expect(state.archiveLists).toBe(1);
  expect(state.archiveReads).toBe(1);
  expect(state.archivePosts).toBe(0);
});
test("saving requires a review and retains an unresolved identity without replay", async ({
  page,
}) => {
  const state = await setup(page);
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save this report", exact: true }),
  ).toBeVisible({ timeout: 1000 });
  await page
    .getByRole("button", { name: "Save this report", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Save campaign report",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  expect(state.archivePosts).toBe(0);
  await dialog
    .getByRole("button", { name: "Review save", exact: true })
    .click();
  await dialog
    .getByLabel("I understand that saving does not approve or run work.", {
      exact: true,
    })
    .check();
  await dialog
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
  expect(state.archivePosts).toBe(1);
  const retained = await page.evaluate(() =>
    Object.entries(localStorage).filter(([key]) =>
      key.startsWith("forge.campaign-report-archive.v1:"),
    ),
  );
  expect(retained).toHaveLength(1);
  expect(Object.keys(JSON.parse(retained[0][1])).sort()).toEqual([
    "actor",
    "base",
    "campaign",
    "project",
    "reportDigest",
    "requestId",
    "version",
  ]);
  await page.reload();
  await navigate(page, "Product projects");
  await selectCampaign(page, state);
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
  expect(state.archivePosts).toBe(1);
});
test("POST200 always requires exact receipt and keeps each confirmed identity recoverable", async ({
  page,
}) => {
  const state = await setup(page);
  state.postMode = "success";
  await selectCampaign(page, state);
  await (
    await reviewSave(page)
  )
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toBeVisible();
  expect(state.markerBeforePost).toBe(true);
  expect(state.archivePosts).toBe(1);
  expect(state.archiveReads).toBe(1);
  const first = state.postBodies[0].request_id;
  await page
    .getByRole("button", { name: "Save this report", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Save campaign report",
    exact: true,
  });
  await dialog
    .getByRole("button", { name: "Review save", exact: true })
    .click();
  await dialog
    .getByLabel("I understand that saving does not approve or run work.")
    .check();
  await dialog
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect.poll(() => state.archiveReads).toBe(2);
  expect(state.archivePosts).toBe(2);
  expect(state.postBodies[1].request_id).not.toBe(first);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((k) =>
          k.startsWith("forge.campaign-report-archive.v1:"),
        ).length,
    ),
  ).toBe(2);
});
test("lost delivery survives reload and recovers only the original exact receipt", async ({
  page,
}) => {
  const state = await setup(page);
  state.postMode = "lost";
  await selectCampaign(page, state);
  await (
    await reviewSave(page)
  )
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Saved request unresolved",
      exact: true,
    }),
  ).toBeFocused();
  const original = state.postBodies[0].request_id;
  await page.reload();
  await navigate(page, "Product projects");
  await selectCampaign(page, state);
  await page
    .getByRole("button", { name: "Check saved request 1", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toBeVisible();
  expect(state.archivePosts).toBe(1);
  expect(state.archiveReads).toBe(1);
  expect(
    await page.evaluate(
      (id) =>
        Object.values(localStorage).some((raw) =>
          raw.includes('"requestId":"' + id + '"'),
        ),
      original,
    ),
  ).toBe(true);
});
test("POST200 followed by missing receipt cannot authorize another save", async ({
  page,
}) => {
  const state = await setup(page);
  state.postMode = "success";
  state.archiveReadStatus = 404;
  await selectCampaign(page, state);
  await (
    await reviewSave(page)
  )
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save this report", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Check saved request 1", exact: true })
    .click();
  await expect.poll(() => state.archiveReads).toBe(2);
  expect(state.archivePosts).toBe(1);
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
});
test("a shape-valid changed live body cannot offer save or be posted", async ({
  page,
}) => {
  const state = await setup(page);
  state.report = { ...verifiedReport, state: "blocked" };
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Campaign report snapshot",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save this report", exact: true }),
  ).toHaveCount(0);
  expect(state.archivePosts).toBe(0);
});
test("current read-only members can open another actor archive but have no save action", async ({
  page,
}) => {
  const state = await setup(page, true);
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save this report", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Open saved report 1", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toBeVisible();
  expect(state.archivePosts).toBe(0);
});
test("post-confirmation access refusal clears private views and preserves the held request", async ({
  page,
}) => {
  const state = await setup(page);
  state.postMode = "success";
  state.archiveReadStatus = 403;
  await selectCampaign(page, state);
  await (
    await reviewSave(page)
  )
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText(
      "Access to campaign evidence could not be confirmed. Sign in or ask an authorized project reader.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Campaign report snapshot",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((k) =>
          k.startsWith("forge.campaign-report-archive.v1:"),
        ).length,
    ),
  ).toBe(1);
  expect(state.archivePosts).toBe(1);
  expect(state.archiveReads).toBe(1);
});
test("a re-signed receipt belonging to another operator cannot release the held save", async ({
  page,
}) => {
  const state = await setup(page);
  state.postMode = "success";
  state.wrongReceiptActor = true;
  await selectCampaign(page, state);
  await (
    await reviewSave(page)
  )
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save this report", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toHaveCount(0);
  expect(state.archivePosts).toBe(1);
});
test("failed durable storage prevents all save dispatch", async ({ page }) => {
  const state = await setup(page);
  await selectCampaign(page, state);
  const dialog = await reviewSave(page);
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.campaign-report-archive.v1:"))
        throw new Error("private storage canary");
      return original.call(this, key, value);
    };
  });
  await dialog
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText(
      "Retained request storage cannot be verified. Saving is unavailable; existing server history can still be read.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.archivePosts).toBe(0);
  await expect(
    page.getByText("private storage canary", { exact: true }),
  ).toHaveCount(0);
});
test("late history evidence cannot enter a newly selected campaign", async ({
  page,
}) => {
  const state = await setup(page);
  await selectCampaign(page, state);
  state.campaigns.push({
    id: "a".repeat(32),
    project_id: projectId,
    status: "running",
  });
  await page
    .getByRole("button", { name: "Load campaigns", exact: true })
    .click();
  await page.getByLabel("Campaign", { exact: true }).selectOption(campaignId);
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  let release!: () => void;
  state.archiveGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page
    .getByRole("button", { name: "Open saved report 1", exact: true })
    .click();
  await expect.poll(() => state.archiveReads).toBe(1);
  await page
    .getByLabel("Campaign", { exact: true })
    .selectOption("a".repeat(32));
  release();
  await expect(
    page.getByRole("heading", { name: "Saved campaign report", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Open saved report 1", exact: true }),
  ).toHaveCount(0);
  expect(state.archivePosts).toBe(0);
});
test("history pagination uses the exact returned sequence without reading report bodies", async ({
  page,
}) => {
  const state = await setup(page);
  const summary = native.page.items[0];
  const items = Array.from({ length: 50 }, (_, i) => {
    const row = {
      ...summary,
      sequence: i + 1,
      request_id: (i + 1).toString(16).padStart(32, "0"),
      digest: "",
    };
    row.digest = digest(
      Object.fromEntries(Object.entries(row).filter(([k]) => k !== "digest")),
    );
    return row;
  });
  state.archivePage = { ...native.page, items, next_after: 50 };
  state.archiveNextPage = { ...native.page, items: [], next_after: null };
  await selectCampaign(page, state);
  await page
    .getByRole("button", { name: "Load saved reports", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: /^Open saved report \d+$/ }),
  ).toHaveCount(50);
  await page
    .getByRole("button", { name: "Next saved reports", exact: true })
    .click();
  await expect(
    page.getByText("No saved reports were returned for this page.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(state.archiveLists).toBe(2);
  expect(state.archiveReads).toBe(0);
  expect(state.archivePosts).toBe(0);
});
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1280, height: 800 },
  { width: 390, height: 600 },
  { width: 844, height: 390 },
  { width: 600, height: 390 },
])
  for (const theme of ["light", "dark"])
    test(`archive keyboard and painted controls ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      const state = await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await selectCampaign(page, state);
      const dialog = await reviewSave(page);
      await page.evaluate(() => document.fonts.ready.then(() => undefined));
      const checkbox = dialog.getByRole("checkbox");
      await checkbox.uncheck();
      await checkbox.focus();
      await page.keyboard.press("Space");
      await expect(checkbox).toBeChecked();
      const acknowledgment = await checkbox.evaluate((node) => {
        const b = node.parentElement!.getBoundingClientRect(),
          field = node.closest(".wizard-field-scroll")!.getBoundingClientRect();
        return {
          top: b.top,
          bottom: b.bottom,
          fieldTop: field.top,
          fieldBottom: field.bottom,
          hit:
            node ===
            document.elementFromPoint(
              node.getBoundingClientRect().x + 8,
              node.getBoundingClientRect().y + 8,
            ),
        };
      });
      expect(acknowledgment.top).toBeGreaterThanOrEqual(
        acknowledgment.fieldTop,
      );
      expect(acknowledgment.bottom).toBeLessThanOrEqual(
        acknowledgment.fieldBottom,
      );
      expect(acknowledgment.hit).toBe(true);
      const action = dialog.getByRole("button", {
        name: "Save report",
        exact: true,
      });
      await action.scrollIntoViewIfNeeded();
      await expect(action).toBeEnabled();
      const bounds = await action.evaluate((node) => {
        const b = node.getBoundingClientRect(),
          d = node.closest("dialog")!.getBoundingClientRect(),
          f = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
        return {
          x: b.x,
          right: b.right,
          top: b.top,
          bottom: b.bottom,
          height: b.height,
          dialogTop: d.top,
          dialogBottom: d.bottom,
          footerTop: f.top,
          hit: node.contains(
            document.elementFromPoint(b.x + b.width / 2, b.bottom - 2),
          ),
        };
      });
      expect(bounds.x).toBeGreaterThanOrEqual(0);
      expect(bounds.right).toBeLessThanOrEqual(viewport.width);
      expect(bounds.height).toBeGreaterThanOrEqual(36);
      expect(bounds.top).toBeGreaterThanOrEqual(bounds.dialogTop);
      expect(bounds.bottom).toBeLessThanOrEqual(bounds.dialogBottom);
      expect(bounds.bottom).toBeLessThanOrEqual(viewport.height);
      expect(bounds.hit).toBe(true);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await action.focus();
      await expect(action).toBeFocused();
      const paint = await action.evaluate((node) => {
        const b = node.getBoundingClientRect(),
          clip = node.closest(".wizard-form")!.getBoundingClientRect(),
          style = getComputedStyle(node),
          outset =
            parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset);
        return {
          left: b.left - outset,
          right: b.right + outset,
          bottom: b.bottom + outset,
          clipLeft: clip.left,
          clipRight: clip.right,
          clipBottom: clip.bottom,
        };
      });
      expect(paint.left).toBeGreaterThanOrEqual(paint.clipLeft);
      expect(paint.right).toBeLessThanOrEqual(paint.clipRight);
      expect(paint.bottom).toBeLessThanOrEqual(paint.clipBottom);
      await page.screenshot({
        path: testInfo.outputPath(`review-${viewport.width}-${theme}.png`),
      });
      await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(
        page.getByRole("button", { name: "Save this report", exact: true }),
      ).toBeFocused();
      expect(state.archivePosts).toBe(0);
      await page
        .getByRole("button", { name: "Load saved reports", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Open saved report 1", exact: true })
        .click();
      const heading = page.getByRole("heading", {
        name: "Saved campaign report",
        exact: true,
      });
      await expect(heading).toBeFocused();
      await heading.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath(`history-${viewport.width}-${theme}.png`),
      });
      expect(state.archivePosts).toBe(0);
    });
test("save review remains operable at 200 percent zoom", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const state = await setup(page);
  await selectCampaign(page, state);
  await page.evaluate(() => (document.documentElement.style.zoom = "2"));
  const dialog = await reviewSave(page);
  const action = dialog.getByRole("button", {
    name: "Save report",
    exact: true,
  });
  await action.scrollIntoViewIfNeeded();
  expect(
    await action.evaluate((node) => {
      const b = node.getBoundingClientRect();
      return (
        b.top >= 0 &&
        b.bottom <= innerHeight &&
        node.contains(
          document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2),
        )
      );
    }),
  ).toBe(true);
  expect(state.archivePosts).toBe(0);
});

test("a supported origin-root API base can save with an isolated canonical identity", async ({
  page,
}) => {
  const state = await setup(page, false, true);
  await selectCampaign(page, state);
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save this report", exact: true }),
  ).toBeEnabled();
  await (
    await reviewSave(page)
  )
    .getByRole("button", { name: "Save report", exact: true })
    .click();
  await expect(
    page.getByText("Saved request unresolved", { exact: true }),
  ).toBeVisible();
  expect(state.archivePosts).toBe(1);
  expect(
    await page.evaluate(() =>
      Object.entries(localStorage)
        .filter(([k]) => k.startsWith("forge.campaign-report-archive.v1:"))
        .map(([, v]) => JSON.parse(v).base),
    ),
  ).toEqual([new URL(page.url()).origin]);
});
for (const drift of ["before review", "before dispatch"])
  test(`changed confirmed storage stays held ${drift} while its original receipt remains readable`, async ({
    page,
  }) => {
    const state = await setup(page);
    state.postMode = "success";
    await selectCampaign(page, state);
    await (
      await reviewSave(page)
    )
      .getByRole("button", { name: "Save report", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Saved campaign report", exact: true }),
    ).toBeVisible();
    const original = state.postBodies[0];
    if (drift === "before dispatch") {
      await page
        .getByRole("button", { name: "Save this report", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Save campaign report",
        exact: true,
      });
      await dialog
        .getByRole("button", { name: "Review save", exact: true })
        .click();
      await dialog.getByRole("checkbox").check();
    }
    const changedRaw = await page.evaluate((id) => {
      const [key, raw] = Object.entries(localStorage).find(
        ([key, raw]) =>
          key.startsWith("forge.campaign-report-archive.v1:") &&
          JSON.parse(raw).requestId === id,
      )!;
      const updated = JSON.stringify({
        ...JSON.parse(raw),
        reportDigest: "9".repeat(64),
      });
      localStorage.setItem(key, updated);
      return updated;
    }, original.request_id);
    if (drift === "before review") {
      await page
        .getByRole("button", { name: "Save this report", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Save campaign report", exact: true }),
      ).toHaveCount(0);
    } else {
      await page
        .getByRole("dialog", { name: "Save campaign report", exact: true })
        .getByRole("button", { name: "Save report", exact: true })
        .click();
      await expect.poll(() => state.archivePosts).toBe(1);
      await expect(
        page.getByRole("dialog", { name: "Save campaign report", exact: true }),
      ).toHaveCount(0);
    }
    expect(state.archivePosts).toBe(1);
    expect(
      await page.evaluate(
        () =>
          Object.keys(localStorage).filter((k) =>
            k.startsWith("forge.campaign-report-archive.v1:"),
          ).length,
      ),
    ).toBe(1);
    const receipt = state.archiveReceipt as typeof native.receipt;
    const summary = {
      ...native.page.items[0],
      request_id: receipt.request_id,
      actor_id: receipt.actor_id,
      receipt_digest: receipt.digest,
      digest: "",
    };
    summary.digest = digest(
      Object.fromEntries(
        Object.entries(summary).filter(([k]) => k !== "digest"),
      ),
    );
    state.archivePage = { ...native.page, items: [summary] };
    await page
      .getByRole("button", { name: "Load saved reports", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Open saved report 1", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Saved campaign report", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Saved request unresolved", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Save this report", exact: true }),
    ).toBeDisabled();
    expect(
      await page.evaluate(
        (id) =>
          Object.entries(localStorage).find(
            ([key, raw]) =>
              key.startsWith("forge.campaign-report-archive.v1:") &&
              JSON.parse(raw).requestId === id,
          )![1],
        original.request_id,
      ),
    ).toBe(changedRaw);
    expect(state.archivePosts).toBe(1);
  });
