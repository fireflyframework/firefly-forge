/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { record } from "./learning-record-fixture";
const actor = "9".repeat(32),
  reason = "Dependency evidence changed after this observation.";
async function fixture(
  page: Page,
  options: {
    lost?: boolean;
    receipt?: string;
    viewer?: boolean;
    storage?: boolean;
  } = {},
) {
  let currentActor = actor;
  let writes = 0,
    reads = 0,
    denied = false,
    body: { request_id: string; reason: string } | null = null;
  if (options.storage)
    await page.addInitScript(() => {
      const set = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith("forge.learning-invalidation"))
          throw new Error("Storage blocked");
        return set.call(this, key, value);
      };
    });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({ json: { mode: "local_bootstrap" } });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: currentActor,
            display_name: "Owned reviewer",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: false,
          memberships: [
            {
              project_id: record.envelope.context.project_id,
              role: options.viewer ? "viewer" : "reviewer",
              revision: 1,
              permissions: options.viewer ? ["read"] : ["read", "review"],
            },
          ],
          roles: {},
        },
      });
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: record.envelope.context.repository_id,
            name: "Owned inventory",
            url: "/owned/inventory",
            provider: "local",
            default_branch: "main",
          },
        ],
      });
    if (path.endsWith("/learning") && path.includes("/repositories/"))
      return route.fulfill({
        json: [{ ...record, state: writes ? "invalidated" : "verified" }],
      });
    if (path.endsWith("/projection"))
      return route.fulfill({
        json: {
          id: "1".repeat(32),
          record_id: record.id,
          state: "pending",
          citations: [],
        },
      });
    if (path.endsWith("/invalidate")) {
      writes++;
      body = req.postDataJSON();
      if (options.lost) return route.abort("failed");
      return route.fulfill({ json: { ...record, state: "invalidated" } });
    }
    if (path.includes("/invalidations/")) {
      reads++;
      if (denied)
        return route.fulfill({
          status: 403,
          json: { detail: "Review permission revoked" },
        });
      if (options.receipt === "absent")
        return route.fulfill({
          status: 404,
          json: { detail: "Learning invalidation request not observed" },
        });
      const receipt = {
        event_id: "2".repeat(32),
        record_id: record.id,
        repository_id: record.envelope.context.repository_id,
        run_id: record.envelope.run_id,
        request_id: body!.request_id,
        kind: "invalidation",
        event_state: "invalidated",
        current_record_state: "invalidated",
        reason: body!.reason,
        actor_id: currentActor,
        created_at: "2026-10-15T01:30:00Z",
        sequence: 2,
        digest: "3".repeat(64),
      };
      if (options.receipt === "foreign") receipt.request_id = "8".repeat(32);
      if (options.receipt === "reason") receipt.reason = "Another decision";
      if (options.receipt === "actor") receipt.actor_id = "4".repeat(32);
      return route.fulfill({
        json:
          options.receipt === "legacy"
            ? { ...receipt, actor_id: null }
            : receipt,
      });
    }
    return route.fulfill({
      json: [
        "/runs",
        "/projects",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((s) => path.endsWith(s))
        ? []
        : {},
    });
  });
  return {
    writes: () => writes,
    reads: () => reads,
    body: () => body,
    deny: () => {
      denied = true;
    },
    changeActor: () => {
      currentActor = "7".repeat(32);
    },
  };
}
async function select(page: Page, reload = true) {
  if (reload) await page.goto("/");
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Learning records", exact: true })
    .click();
  await page
    .getByRole("button", { name: record.envelope.question, exact: true })
    .click();
}
async function review(page: Page) {
  await page
    .getByRole("button", { name: "Invalidate evidence", exact: true })
    .click();
  await page.getByLabel("Reason for invalidation").fill(reason);
  await page
    .getByRole("button", { name: "Review invalidation", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Confirm invalidation", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "I understand this excludes the evidence from reuse and keeps its history.",
    )
    .check();
}
test("one submitted decision is confirmed only by its exact receipt", async ({
  page,
}) => {
  const state = await fixture(page);
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Confirm invalidation", exact: true })
    .click();
  await expect(
    page.getByText("Original invalidation confirmed", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(state.reads()).toBe(1);
  await expect(page.getByText(reason, { exact: true })).toBeVisible();
});
for (const receipt of ["absent", "foreign", "reason", "actor", "legacy"]) {
  test(`lost response and ${receipt} receipt never authorizes replay after reload`, async ({
    page,
  }) => {
    const state = await fixture(page, { lost: true, receipt });
    await select(page);
    await review(page);
    await page
      .getByRole("button", { name: "Confirm invalidation", exact: true })
      .click();
    await expect(
      page.getByText("Original request unresolved", { exact: true }),
    ).toBeVisible();
    const original = state.body();
    await select(page);
    await expect(
      page.getByRole("button", { name: "Check original request", exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Check original request", exact: true })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(
      page.getByText("Original request unresolved", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Invalidate evidence", exact: true }),
    ).toHaveCount(0);
    expect(state.writes()).toBe(1);
    expect(state.body()).toEqual(original);
    await select(page);
    expect(state.writes()).toBe(1);
  });
}
test("lost response recovers the exact event after navigation without another POST", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Confirm invalidation", exact: true })
    .click();
  await expect(
    page.getByText("Original request unresolved", { exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await select(page, false);
  await page
    .getByRole("button", { name: "Check original request", exact: true })
    .click();
  await expect(
    page.getByText("Original invalidation confirmed", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(state.reads()).toBe(1);
});
test("unavailable pending storage prevents any invalidation request", async ({
  page,
}) => {
  const state = await fixture(page, { storage: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Confirm invalidation", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(state.writes()).toBe(0);
  await expect(
    page.getByRole("button", { name: "Confirm invalidation", exact: true }),
  ).toBeDisabled();
});
test("viewer can inspect evidence but receives no invalidation control", async ({
  page,
}) => {
  await fixture(page, { viewer: true });
  await select(page);
  await expect(
    page.getByRole("button", { name: "Invalidate evidence", exact: true }),
  ).toHaveCount(0);
});
test("current receipt denial clears private evidence and keeps the pending identity", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Confirm invalidation", exact: true })
    .click();
  await expect(
    page.getByText("Original request unresolved", { exact: true }),
  ).toBeVisible();
  state.deny();
  await page
    .getByRole("button", { name: "Check original request", exact: true })
    .click();
  await expect(
    page.getByText(record.envelope.statement, { exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).some((k) =>
        k.startsWith("forge.learning-invalidation"),
      ),
    ),
  ).toBe(true);
  expect(state.writes()).toBe(1);
});

test("an actor change does not expose or reconcile the previous actor's pending reason", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Confirm invalidation", exact: true })
    .click();
  await expect(
    page.getByText("Original request unresolved", { exact: true }),
  ).toBeVisible();
  state.changeActor();
  await select(page);
  await expect(page.getByText(reason, { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Check original request", exact: true }),
  ).toHaveCount(0);
  expect(state.writes()).toBe(1);
  expect(state.reads()).toBe(0);
});

test("corrupt retained storage blocks dispatch without discarding its original bytes", async ({
  page,
}) => {
  await page.addInitScript(
    ({ actor, repository, record }) => {
      const key = `forge.learning-invalidation.v1:${encodeURIComponent(location.origin + "/api/v1")}:${actor}:${repository}:${record}`;
      sessionStorage.setItem(key, "owned-corrupt-pending-marker");
    },
    {
      actor,
      repository: record.envelope.context.repository_id,
      record: record.id,
    },
  );
  const state = await fixture(page);
  await select(page);
  await expect(
    page.getByRole("button", { name: "Invalidate evidence", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(0);
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage)
        .filter((k) => k.startsWith("forge.learning-invalidation"))
        .map((k) => sessionStorage.getItem(k)),
    ),
  ).toEqual(["owned-corrupt-pending-marker"]);
});

for (const [width, height] of [
  [844, 390],
  [390, 480],
]) {
  test(`short invalidation review keeps complete controls inside clipping ancestors ${width}x${height}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    await fixture(page);
    await select(page);
    await review(page);
    const button = page.getByRole("button", {
      name: "Confirm invalidation",
      exact: true,
    });
    await button.scrollIntoViewIfNeeded();
    const clipping = await button.evaluate((element) => {
      const box = element.getBoundingClientRect();
      let top = 0,
        bottom = innerHeight;
      for (let node = element.parentElement; node; node = node.parentElement) {
        if (/hidden|auto|scroll|clip/.test(getComputedStyle(node).overflowY)) {
          const rect = node.getBoundingClientRect();
          top = Math.max(top, rect.top + node.clientTop);
          bottom = Math.min(
            bottom,
            rect.top + node.clientTop + node.clientHeight,
          );
        }
        // A modal is in the top layer; page ancestors do not clip it.
        if (node instanceof HTMLDialogElement && node.open) break;
      }
      return {
        top: box.top,
        bottom: box.bottom,
        visibleTop: top,
        visibleBottom: bottom,
      };
    });
    expect(clipping.top).toBeGreaterThanOrEqual(clipping.visibleTop);
    expect(clipping.bottom).toBeLessThanOrEqual(clipping.visibleBottom);
    await page
      .getByLabel(
        "I understand this excludes the evidence from reuse and keeps its history.",
      )
      .scrollIntoViewIfNeeded();
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("button", { name: "Invalidate evidence", exact: true }),
    ).toBeFocused();
  });
}

for (const [theme, width, height] of [
  ["light", 1440, 1000],
  ["dark", 390, 844],
] as const) {
  test(`confirmed invalidation remains visible outside the verified filter: ${theme}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ colorScheme: theme });
    const state = await fixture(page);
    await select(page);
    await page
      .getByText("Filter saved records", { exact: true })
      .first()
      .click();
    await page
      .getByLabel("Saved state", { exact: true })
      .selectOption("verified");
    await review(page);
    await page
      .getByRole("button", { name: "Confirm invalidation", exact: true })
      .click();
    await expect(
      page.getByText("Original invalidation confirmed", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("0 of 1 saved records shown", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("No saved records match this filter.", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "The selected record no longer matches this filter. Its decision remains visible until you close it or select another record.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: record.envelope.question, exact: true }),
    ).toHaveCount(0);
    await expect(page.getByText(reason, { exact: true })).toBeVisible();
    expect(state.writes()).toBe(1);
    expect(state.reads()).toBe(1);
    const original = state.body();
    await page.screenshot({
      path: testInfo.outputPath(`confirmed-filter-${theme}.png`),
      fullPage: true,
    });
    const confirmation = page.getByText("Original invalidation confirmed", {
      exact: true,
    });
    await confirmation.scrollIntoViewIfNeeded();
    await expect(confirmation).toBeInViewport();
    await page.screenshot({
      path: testInfo.outputPath(`confirmed-receipt-${theme}.png`),
      fullPage: true,
    });
    await page
      .getByRole("button", { name: "Close selected record", exact: true })
      .click();
    await expect(
      page.locator(".learning-records-filter-toggle > summary"),
    ).toBeFocused();
    await expect(
      page.getByText("Original invalidation confirmed", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("No saved records match this filter.", { exact: true }),
    ).toBeVisible();
    await page.getByLabel("Saved state", { exact: true }).selectOption("all");
    await page
      .getByRole("button", { name: record.envelope.question, exact: true })
      .click();
    await page
      .getByRole("button", { name: "Check original request", exact: true })
      .click();
    await expect(
      page.getByText("Original invalidation confirmed", { exact: true }),
    ).toBeVisible();
    expect(state.writes()).toBe(1);
    expect(state.reads()).toBe(2);
    expect(state.body()).toEqual(original);
  });
}
