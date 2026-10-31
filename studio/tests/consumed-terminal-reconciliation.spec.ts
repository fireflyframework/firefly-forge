/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Synthetic API responses exercise Studio only; no native or Original actions.
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";

const h = "a".repeat(64),
  requestId = "5".repeat(32);
const run = {
  id: "4".repeat(32),
  repository_id: "3".repeat(32),
  project_id: "2".repeat(32),
  brief: "Saved implementation fixture",
  status: "paused",
  attempt: 3,
  agent: "bridge",
  publish: false,
  base_revision: "b".repeat(40),
  consumed_terminal_recovery: {
    state: "claimed",
    request_id: requestId,
    record_digest: h,
    claim_digest: h,
  },
};
const candidate = {
  version: 1,
  run_id: run.id,
  request_id: requestId,
  same_task_attempt: 3,
  ...Object.fromEntries(
    [
      "owner_digest",
      "actor_digest",
      "policy_digest",
      "runtime_contract",
      "profile_digest",
      "run_digest",
      "task_digest",
      "journal_digest",
      "recovery_digest",
      "claim_digest",
      "source_digest",
      "index_digest",
      "digest",
    ].map((key) => [key, h]),
  ),
};
const terminalCandidate = {
  version: 2,
  run_id: run.id,
  actor_digest: h,
  policy_digest: h,
  run_digest: h,
  journal_digest: h,
  digest: h,
  proof: {
    version: 2,
    run_id: run.id,
    task_id: "implement",
    task_attempt: 3,
    task_key: "7".repeat(32),
    completion_count: 1,
    completions: [
      {
        request_id: "fbr1_" + "8".repeat(32),
        intent_digest: h,
        checkpoint_record_digest: h,
        response_record_digest: h,
        consume_record_digest: h,
      },
    ],
    verification: "consumed_terminal_output_known_only",
    ...Object.fromEntries(
      [
        "terminal_report_sha256",
        "final_response_sha256",
        "recorded_runtime_contract",
        "current_runtime_contract",
        "current_profile_digest",
        "source_digest",
        "parent_final_digest",
        "handoff_custody_digest",
        "usage_before_digest",
        "usage_after_digest",
        "digest",
      ].map((key) => [key, h]),
    ),
    recorded_runtime_contract: "4".repeat(64),
    current_runtime_contract: "5".repeat(64),
    current_profile_digest: "6".repeat(64),
  },
};
const terminalInput = {
  version: 2,
  candidate_digest: h,
  policy_digest: h,
  expected_run_digest: h,
  journal_digest: h,
  proof_digest: h,
  acknowledge_known_worker_output: true,
  acknowledge_no_execution_grant: true,
  acknowledge_local_postprocessing: true,
};
async function open(page: Page) {
  await openFactoryRun(page, run.brief);
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
}
async function setup(page: Page, theme?: "light" | "dark") {
  const state = {
    posts: 0,
    reads: 0,
    candidateStatus: 200,
    postStatus: 200,
    readStatus: 404,
    lost: false,
    invalidReceipt: false,
    candidate: structuredClone(candidate),
    settlement: "accepted",
  };
  const body = {
    version: 1,
    candidate_digest: h,
    acknowledge_same_claimed_action: true,
    acknowledge_no_execution_grant: true,
    acknowledge_local_postprocessing: true,
  };
  const receipt = () => ({
    version: 1,
    candidate: state.candidate,
    request: body,
    record_digest: "c".repeat(64),
    settlement: {
      version: 2,
      kind: "consumed_terminal_recovery",
      run_id: run.id,
      request_id: requestId,
      record_digest: h,
      candidate: {
        ...terminalCandidate,
        run_id: state.invalidReceipt ? "9".repeat(32) : run.id,
      },
      request: terminalInput,
      state: state.settlement,
      run_status: "paused",
      same_task_attempt: 3,
      no_execution_grant: true,
    },
  });
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
      return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/reconciliation-candidate"))
      return route.fulfill({
        status: state.candidateStatus,
        json:
          state.candidateStatus === 200
            ? state.candidate
            : { detail: "private refusal canary" },
      });
    if (path.endsWith("/reconcile")) {
      state.posts++;
      expect(path).toContain(
        `/worker-completion-settlements/${requestId}/reconcile`,
      );
      expect(req.postDataJSON()).toEqual(body);
      const markers = await page.evaluate(() =>
        Object.entries(localStorage).filter(([key]) =>
          key.startsWith("forge.consumed-terminal-reconciliation.v1:"),
        ),
      );
      expect(markers).toHaveLength(1);
      expect(markers[0][1]).toContain(requestId);
      expect(markers[0][1]).not.toContain("fictional-ui-token");
      if (state.lost) return route.abort("failed");
      return route.fulfill({
        status: state.postStatus,
        json:
          state.postStatus === 200
            ? receipt()
            : { detail: "private refusal canary" },
      });
    }
    if (path.endsWith("/reconciliation")) {
      state.reads++;
      return route.fulfill({
        status: state.readStatus,
        json:
          state.readStatus === 200
            ? receipt()
            : { detail: "private readback canary" },
      });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: "Saved implementation fixture",
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
  if (theme)
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fictional-ui-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expectAuthenticated(page);
  await navigate(page, "Factory");
  await open(page);
  return state;
}
const panel = (page: Page) =>
  page.getByRole("region", {
    name: "Claimed result reconciliation",
    exact: true,
  });
async function inspect(page: Page) {
  await panel(page)
    .getByRole("button", { name: "Check claimed action", exact: true })
    .click();
}
async function submit(page: Page) {
  await inspect(page);
  await panel(page).getByRole("checkbox").check();
  await panel(page)
    .getByRole("button", { name: "Reconcile saved result", exact: true })
    .click();
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const) {
    test(`claimed result is keyboard reachable and contained at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 1000 });
      const state = await setup(page, theme),
        card = panel(page);
      const check = card.getByRole("button", {
        name: "Check claimed action",
        exact: true,
      });
      await check.focus();
      await page.keyboard.press("Enter");
      await expect(
        card.getByRole("heading", {
          name: "Review the original action",
          exact: true,
        }),
      ).toBeFocused();
      const action = card.getByRole("button", {
        name: "Reconcile saved result",
        exact: true,
      });
      await expect(action).toBeDisabled();
      await card.getByRole("checkbox").focus();
      await page.keyboard.press("Space");
      await expect(action).toBeEnabled();
      await expect(card.locator("details")).not.toHaveAttribute("open");
      expect(
        await page
          .getByRole("button", { name: "Resume run", exact: true })
          .count(),
      ).toBe(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth + 1,
        ),
      ).toBe(false);
      expect(
        await card.evaluate((node) => node.scrollWidth > node.clientWidth + 1),
      ).toBe(false);
      await card.scrollIntoViewIfNeeded();
      const actionBox = await action.boundingBox();
      const footerBox = await page
        .locator("footer")
        .filter({
          has: page.getByRole("link", { name: "Apache 2.0", exact: true }),
        })
        .boundingBox();
      expect(actionBox).not.toBeNull();
      expect(footerBox).not.toBeNull();
      expect(actionBox!.y + actionBox!.height).toBeLessThanOrEqual(
        footerBox!.y,
      );
      await page.screenshot({
        path: info.outputPath(`claimed-result-review-${width}-${theme}.png`),
      });
      await card.screenshot({
        path: info.outputPath(`claimed-result-panel-${width}-${theme}.png`),
      });
      await action.focus();
      await page.keyboard.press("Enter");
      await expect(
        card.getByRole("heading", {
          name: "Saved result accepted",
          exact: true,
        }),
      ).toBeFocused();
      expect(state.posts).toBe(1);
      await page.screenshot({
        path: info.outputPath(`claimed-result-receipt-${width}-${theme}.png`),
      });
    });
  }
test("lost POST outcome preserves one original identity across reload and GET-only404 readback", async ({
  page,
}) => {
  const state = await setup(page);
  state.lost = true;
  await submit(page);
  await expect(panel(page).getByRole("alert")).toContainText(
    "outcome is unconfirmed",
  );
  expect(state.posts).toBe(1);
  await page.reload();
  await open(page);
  await panel(page)
    .getByRole("button", { name: "Read reconciliation receipt", exact: true })
    .click();
  await expect(panel(page).getByRole("alert")).toContainText(
    "outcome is unconfirmed",
  );
  expect(state.posts).toBe(1);
  expect(await panel(page).getByRole("checkbox").count()).toBe(0);
  state.readStatus = 200;
  state.settlement = "claimed";
  await panel(page)
    .getByRole("button", { name: "Read reconciliation receipt", exact: true })
    .click();
  await expect(
    panel(page).getByText(/original action remains claimed/),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
for (const failure of ["stale", "foreign-operator", "changed-claim"] as const)
  test(`${failure} candidate cannot expose or submit another action`, async ({
    page,
  }) => {
    const state = await setup(page);
    if (failure === "changed-claim")
      state.candidate = { ...state.candidate, claim_digest: "d".repeat(64) };
    else state.candidateStatus = failure === "stale" ? 409 : 403;
    await inspect(page);
    await expect(panel(page).getByRole("alert")).toBeVisible();
    expect(await panel(page).getByRole("checkbox").count()).toBe(0);
    expect(state.posts).toBe(0);
    await expect(panel(page)).not.toContainText("private refusal canary");
  });
for (const code of [403, 409])
  test(`POST${code} keeps the original action in readback-only custody`, async ({
    page,
  }) => {
    const state = await setup(page);
    state.postStatus = code;
    await submit(page);
    await expect(panel(page).getByRole("alert")).toBeVisible();
    await panel(page)
      .getByRole("button", { name: "Read reconciliation receipt", exact: true })
      .click();
    expect(state.posts).toBe(1);
    expect(await panel(page).getByRole("checkbox").count()).toBe(0);
    await expect(panel(page)).not.toContainText("private refusal canary");
  });
test("a copied foreign-actor browser marker stays held after reload", async ({
  page,
}) => {
  const state = await setup(page);
  state.lost = true;
  await submit(page);
  await expect(panel(page).getByRole("alert")).toBeVisible();
  await page.evaluate(() => {
    const key = Object.keys(localStorage).find((key) =>
      key.startsWith("forge.consumed-terminal-reconciliation.v1:"),
    )!;
    const marker = JSON.parse(localStorage.getItem(key)!);
    marker.context.actor = "9".repeat(32);
    localStorage.setItem(key, JSON.stringify(marker));
  });
  await page.reload();
  await open(page);
  await expect(panel(page)).toContainText("New submissions remain held");
  await panel(page)
    .getByRole("button", { name: "Read reconciliation receipt", exact: true })
    .click();
  expect(state.posts).toBe(1);
  expect(await panel(page).getByRole("checkbox").count()).toBe(0);
});
test("an accepted response with a foreign nested V2 proof never appears accepted", async ({
  page,
}) => {
  const state = await setup(page);
  state.invalidReceipt = true;
  await submit(page);
  await expect(panel(page).getByRole("alert")).toContainText(
    "outcome is unconfirmed",
  );
  expect(
    await panel(page)
      .getByRole("heading", { name: "Saved result accepted", exact: true })
      .count(),
  ).toBe(0);
  expect(state.posts).toBe(1);
  expect(await panel(page).getByRole("checkbox").count()).toBe(0);
});
