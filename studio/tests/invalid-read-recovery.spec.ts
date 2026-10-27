/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Fictional, fully intercepted API responses exercise Studio only.
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";
const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: "a".repeat(32),
  brief: "Saved continuity fixture",
  status: "failed",
  agent: "bridge",
  publish: false,
  invalid_read_continuity: { request_id: "b".repeat(32) },
};
const h = "c".repeat(64);
const baseCandidate = {
  version: 1,
  kind: "unclaimed_invalid_read_recovery_candidate",
  run_id: run.id,
  request_id: run.invalid_read_continuity.request_id,
  digest: h,
  admission: "READY",
  holds: [],
  execution_authorized: false,
  original_admission_seal: h,
  failed_event_digest: h,
  original_settlement_digest: h,
  original_claim_digest: h,
  checkpoint_digest: h,
  source_digest: h,
  attempt: 2,
  current_profile: { id: "qualified-fixture" },
  current_runtime_contract: h,
  policy_digest: h,
  maximum_window_seconds: 300,
  frozen_timeout_seconds: 600,
  original_lease: {},
  usage: {
    requests: 1,
    request_limit: 8,
    tool_calls: 2,
    tool_calls_limit: 20,
    commands: 11,
    command_limit: 22,
  },
};
async function setup(page: Page, mode = "valid", theme = "dark") {
  const state = {
    posts: 0,
    reads: 0,
    recovery: "",
    readStatus: 200,
    deny: false,
    mode,
    candidate: structuredClone(baseCandidate) as Record<string, unknown>,
    status: "queued",
  };
  const baseURL = test.info().project.use.baseURL;
  expect(baseURL).toBeDefined();
  const origin = new URL(baseURL!).origin;
  expect(new URL(origin).hostname).toBe("127.0.0.1");
  await page.route("**/*", (route) =>
    new URL(route.request().url()).origin === origin
      ? route.continue()
      : route.abort(),
  );
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.includes("/invalid-read-continuity/")) {
      if (state.deny)
        return route.fulfill({
          status: 403,
          json: { detail: "private-denial-canary" },
        });
      if (path.endsWith("/recovery-candidate"))
        return route.fulfill({ json: state.candidate });
      if (path.includes("/recoveries/")) {
        state.posts++;
        state.recovery = path.split("/").at(-1)!;
        const body = req.postDataJSON();
        expect(body).toMatchObject({
          candidate_digest: h,
          expected_admission_seal: h,
          expected_failed_event_digest: h,
          valid_for_seconds: 300,
          acknowledge_never_claimed: true,
          acknowledge_same_request_and_attempt: true,
          acknowledge_preserved_consumption_and_budgets: true,
          acknowledge_renewed_same_attempt_lease: true,
        });
        expect(Object.keys(body)).toHaveLength(9);
        const marker = await page.evaluate(
          () =>
            Object.entries(localStorage).find(([key]) =>
              key.startsWith("forge.invalid-read-recovery.v1:"),
            )?.[1],
        );
        expect(marker).toContain(state.recovery);
        expect(marker).not.toContain("fictional-ui-token");
        if (state.mode === "lost") return route.abort("failed");
        if (state.mode === "source_race")
          return route.fulfill({
            status: 409,
            json: { detail: "private-source-race-canary" },
          });
      } else state.reads++;
      if (state.readStatus !== 200)
        return route.fulfill({
          status: state.readStatus,
          json: { detail: "private-receipt-canary" },
        });
      return route.fulfill({
        json: {
          version: 1,
          kind: "unclaimed_invalid_read_recovery_status",
          run_id: run.id,
          request_id: run.invalid_read_continuity.request_id,
          recovery_id: state.recovery || null,
          recovery_record_digest: state.recovery ? h : null,
          status: state.recovery ? state.status : "not_recovered",
          original_admission_seal: h,
          lease: state.recovery ? {} : null,
          current_profile: { id: "qualified-fixture" },
        },
      });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: run.brief,
            url: "/fixture",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [run]
        : path.endsWith(`/runs/${run.id}`)
          ? run
          : path.endsWith("/plan")
            ? { revision: 1, tasks: [] }
            : path.endsWith("/journal")
              ? { valid: true, entries: [] }
              : path.endsWith("/health")
                ? { status: "ok", execution_ready: false }
                : [];
    return route.fulfill({ json });
  });
  await page.goto("/");
  await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fictional-ui-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await open(page);
  return state;
}
async function open(page: Page) {
  await page.getByRole("button", { name: /Saved continuity fixture/ }).click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
}
const panel = (page: Page) =>
  page.getByRole("region", {
    name: "Saved continuation recovery",
    exact: true,
  });
async function review(page: Page) {
  await panel(page)
    .getByRole("button", { name: "Check current admission", exact: true })
    .click();
  await panel(page)
    .getByRole("button", { name: "Review failed admission", exact: true })
    .click();
}
async function submit(page: Page) {
  await review(page);
  for (const box of await panel(page).getByRole("checkbox").all())
    await box.check();
  await panel(page)
    .getByRole("button", { name: "Requeue existing task", exact: true })
    .click();
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`review uses four acknowledgements at ${width}px in ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const state = await setup(page, "valid", theme);
      await expect(
        page.getByRole("region", {
          name: "Original model result",
          exact: true,
        }),
      ).toHaveCount(0);
      await panel(page)
        .getByRole("button", { name: "Check current admission", exact: true })
        .click();
      await panel(page)
        .getByRole("heading", { name: "Saved continuation", exact: true })
        .evaluate((node) => node.scrollIntoView({ block: "center" }));
      await page.screenshot({
        path: info.outputPath(`recovery-intro-${width}-${theme}.png`),
      });
      await captureWalkthrough(
        page,
        "continuity-recovery-intro",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/invalid-read-recovery.spec.ts",
          resetFields: false,
          actionSelector:
            'section[aria-label="Saved continuation recovery"] button',
          focusSelector: 'section[aria-label="Saved continuation recovery"]',
        },
      );
      await panel(page)
        .getByRole("button", { name: "Review failed admission", exact: true })
        .click();
      await expect(
        panel(page).getByRole("heading", {
          name: "Requeue the existing task",
          exact: true,
        }),
      ).toBeFocused();
      await panel(page)
        .getByRole("button", { name: "Back", exact: true })
        .click();
      const reviewOpener = panel(page).getByRole("button", {
        name: "Review failed admission",
        exact: true,
      });
      await expect(reviewOpener).toBeFocused();
      await reviewOpener.click();
      for (const hidden of [h, run.id, run.invalid_read_continuity.request_id])
        expect(await panel(page).innerText()).not.toContain(hidden);
      await panel(page)
        .getByRole("heading", {
          name: "Requeue the existing task",
          exact: true,
        })
        .evaluate((node) => node.scrollIntoView({ block: "center" }));
      await page.screenshot({
        path: info.outputPath(`recovery-review-${width}-${theme}.png`),
      });
      const action = panel(page).getByRole("button", {
        name: "Requeue existing task",
        exact: true,
      });
      for (const box of await panel(page).getByRole("checkbox").all()) {
        await expect(action).toBeDisabled();
        await box.focus();
        await page.keyboard.press("Space");
      }
      await expect(action).toBeEnabled();
      await captureWalkthrough(
        page,
        "continuity-recovery-review",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/invalid-read-recovery.spec.ts",
          resetFields: false,
          actionSelector: ".agent-checkpoint-review button.primary",
          focusSelector: ".agent-checkpoint-review",
        },
      );
      expect(state.posts).toBe(0);
      expect(
        await panel(page).evaluate(
          (node) => node.scrollWidth > node.clientWidth + 1,
        ),
      ).toBe(false);
      await action.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`recovery-${width}-${theme}.png`),
      });
      if (width >= 640)
        await panel(page).screenshot({
          path: info.outputPath(`recovery-panel-${width}-${theme}.png`),
        });
      await action.click();
      await expect(
        panel(page).getByRole("heading", {
          name: "Continuation queued",
          exact: true,
        }),
      ).toBeFocused();
      await panel(page)
        .getByRole("heading", { name: "Continuation queued", exact: true })
        .evaluate((node) => node.scrollIntoView({ block: "center" }));
      await page.screenshot({
        path: info.outputPath(`recovery-confirmation-${width}-${theme}.png`),
      });
      await captureWalkthrough(
        page,
        "continuity-recovery-queued",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/invalid-read-recovery.spec.ts",
          resetFields: false,
          actionSelector:
            'section[aria-label="Saved continuation recovery"] button',
          focusSelector: 'section[aria-label="Saved continuation recovery"]',
        },
      );
      expect(state.posts).toBe(1);
      await panel(page)
        .getByRole("button", {
          name: "Check saved recovery receipt",
          exact: true,
        })
        .click();
      expect(state.posts).toBe(1);
    });
test("lost response, exact404 and reload keep the same one-use identity", async ({
  page,
}) => {
  const state = await setup(page, "lost");
  await submit(page);
  await expect(panel(page).getByRole("alert")).toContainText(
    "has not been confirmed",
  );
  const original = state.recovery;
  state.readStatus = 404;
  await page.reload();
  await open(page);
  await panel(page)
    .getByRole("button", { name: "Check saved recovery receipt", exact: true })
    .click();
  await expect(panel(page).getByRole("alert")).toContainText(
    "has not been confirmed",
  );
  expect(state.posts).toBe(1);
  expect(state.recovery).toBe(original);
  await expect(
    panel(page).getByRole("button", {
      name: "Requeue existing task",
      exact: true,
    }),
  ).toHaveCount(0);
  state.readStatus = 200;
  await panel(page)
    .getByRole("button", { name: "Check saved recovery receipt", exact: true })
    .click();
  await expect(
    panel(page).getByRole("heading", {
      name: "Continuation queued",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
test("source race preserves one action and never refreshes into another POST", async ({
  page,
}) => {
  const state = await setup(page, "source_race");
  await submit(page);
  await expect(panel(page).getByRole("alert")).toContainText(
    "has not been confirmed",
  );
  const original = state.recovery;
  state.recovery = "";
  await panel(page)
    .getByRole("button", { name: "Check saved recovery receipt", exact: true })
    .click();
  await expect(panel(page).getByRole("alert")).toContainText(
    "has not been confirmed",
  );
  await expect(
    panel(page).getByRole("button", {
      name: "Review failed admission",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toBe(1);
  expect(original).toMatch(/^[a-f0-9]{32}$/);
  await expect(panel(page).getByText("private-source-race-canary")).toHaveCount(
    0,
  );
});
for (const status of ["executing", "needs_reconciliation", "failed"])
  test(`existing ${status} recovery offers readback only`, async ({ page }) => {
    const state = await setup(page);
    state.recovery = "e".repeat(32);
    state.status = status;
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(
      panel(page).getByRole("button", {
        name: "Review failed admission",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
    expect(state.posts).toBe(0);
  });
test("revocation hides review details and never submits", async ({ page }) => {
  const state = await setup(page);
  await review(page);
  state.deny = true;
  await panel(page)
    .getByRole("button", { name: "Check current admission", exact: true })
    .click();
  await expect(panel(page).getByRole("alert")).toContainText(
    "Private recovery details are hidden",
  );
  await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
  await expect(panel(page).getByText("private-denial-canary")).toHaveCount(0);
  expect(state.posts).toBe(0);
});
for (const hold of [
  "current_profile_unqualified",
  "lease_policy_disabled",
  "unavailable_observation_policy_disabled",
  "active_execution_budget_expired",
])
  test(`withholds requeue for ${hold}`, async ({ page }) => {
    const state = await setup(page);
    Object.assign(state.candidate, {
      admission: "HOLD",
      holds: [hold],
      maximum_window_seconds: 0,
    });
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(
      panel(page).getByRole("heading", { name: "Recovery held", exact: true }),
    ).toBeVisible();
    await expect(
      panel(page).getByRole("button", {
        name: "Review failed admission",
        exact: true,
      }),
    ).toHaveCount(0);
    expect(state.posts).toBe(0);
  });
