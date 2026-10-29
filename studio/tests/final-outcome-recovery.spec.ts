/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Intercepted Studio fixtures prove presentation and one-use submission, not native CRM recovery.
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: "a".repeat(32),
  brief: "Saved final result fixture",
  status: "failed",
  agent: "bridge",
  publish: false,
  invalid_read_continuity: { request_id: "b".repeat(32) },
  implementation_pending: false,
  usage: {
    runtime_report: {
      summary: "Private report canary",
      feedback: [{ disposition: "blocked" }],
    },
  },
};
const hash = "c".repeat(64);
function candidate() {
  return {
    version: 1,
    kind: "final_outcome_continuation_candidate",
    run_id: run.id,
    request_id: run.invalid_read_continuity.request_id,
    admission: "READY",
    holds: [],
    execution_authorized: false,
    original_admission_seal: hash,
    attempt: 2,
    ...Object.fromEntries(
      [
        "ticket_digest",
        "events_digest",
        "journal_digest",
        "run_digest",
        "task_digest",
        "checkpoint_record_digest",
        "response_record_digest",
        "consume_record_digest",
        "report_digest",
        "final_outcome_digest",
        "source_digest",
        "current_runtime_contract",
        "policy_digest",
        "digest",
      ].map((name) => [name, hash]),
    ),
    current_profile: { id: "fictional-qualified" },
    maximum_window_seconds: 1800,
    admission_window_purpose: "claim_only",
    outcome: "blocked",
    followup_kind: "verification",
    usage: { requests: 17, tool_calls: 11 },
    limits: { request_limit: 100, tool_calls_limit: 300 },
  };
}
async function setup(page: Page, theme: "light" | "dark" = "light") {
  const state = {
    posts: 0,
    action: "",
    lost: false,
    readStatus: 200,
    status: "not_recovered",
    candidate: candidate(),
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.includes("/final-outcome-continuations/")) {
      if (path.endsWith("/candidate"))
        return route.fulfill({ json: state.candidate });
      if (req.method() === "POST") {
        state.posts++;
        state.action = path.split("/").at(-1)!;
        expect(state.action).toMatch(/^[a-f0-9]{32}$/);
        expect(req.postDataJSON()).toEqual({
          version: 1,
          candidate_digest: hash,
          valid_for_seconds: 1800,
          acknowledge_already_consumed_final_output: true,
          acknowledge_no_completed_effect_replay: true,
          acknowledge_same_request_attempt_and_budgets: true,
          acknowledge_finite_ownership_lease: true,
        });
        const saved = await page.evaluate(() =>
          Object.entries(localStorage).filter(([key]) =>
            key.startsWith("forge.final-outcome.v1:"),
          ),
        );
        expect(saved).toHaveLength(1);
        expect(saved[0][1]).toContain(state.action);
        expect(saved[0][1]).not.toContain("fictional-ui-token");
        state.status = "completed";
        if (state.lost) return route.abort("failed");
      } else if (state.readStatus !== 200)
        return route.fulfill({
          status: state.readStatus,
          json: { detail: "private denial canary" },
        });
      return route.fulfill({
        json: {
          version: 1,
          kind: "final_outcome_continuation_status",
          run_id: run.id,
          request_id: run.invalid_read_continuity.request_id,
          status: state.status,
          action_id: state.action || null,
          witness_digest: state.action ? hash : null,
          original_admission_seal: hash,
          lease: state.action ? {} : null,
          run_status: state.action ? "paused" : "failed",
        },
      });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: "Saved final result fixture",
            url: "/fixture",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [run]
        : path.endsWith("/runs/" + run.id)
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
  await open(page);
  return state;
}
async function open(page: Page) {
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Saved final result fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
}
async function review(page: Page) {
  const panel = page.getByRole("region", {
    name: "Saved agent result",
    exact: true,
  });
  await panel
    .getByRole("button", { name: "Check saved result", exact: true })
    .click();
  await expect(
    panel.getByRole("heading", { name: "Review the saved report" }),
  ).toBeVisible();
  return panel;
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`saved final-result wizard fits ${width}px in ${theme} without claiming successful QA`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const state = await setup(page, theme),
        panel = await review(page);
      await panel.evaluate((node) => {
        const main = node.closest("main")!;
        main.scrollTop +=
          node.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
      });
      await expect(
        panel.getByRole("heading", { name: "Saved agent result", exact: true }),
      ).toBeVisible();
      await page.screenshot({
        path: info.outputPath(`saved-final-review-${width}-${theme}.png`),
      });
      const accept = panel.getByRole("button", { name: "Accept saved result" });
      await expect(accept).toBeDisabled();
      await panel.getByRole("checkbox").focus();
      await page.keyboard.press("Space");
      await expect(accept).toBeEnabled();
      await accept.scrollIntoViewIfNeeded();
      const geometry = await accept.evaluate((node) => {
        const box = node.getBoundingClientRect();
        return {
          height: box.height,
          visible: box.top >= 0 && box.bottom <= innerHeight,
          hit: node.contains(
            document.elementFromPoint(
              box.x + box.width / 2,
              box.y + box.height / 2,
            ),
          ),
          unclipped: node.scrollWidth <= node.clientWidth + 1,
        };
      });
      expect(geometry).toMatchObject({
        visible: true,
        hit: true,
        unclipped: true,
      });
      expect(geometry.height).toBeGreaterThanOrEqual(44);
      expect(
        await panel.evaluate(
          (node) => node.scrollWidth <= node.clientWidth + 1,
        ),
      ).toBe(true);
      await expect(panel).not.toContainText("Private report canary");
      expect(state.posts).toBe(0);
      await page.screenshot({
        path: info.outputPath(`saved-final-${width}-${theme}.png`),
      });
      await accept.click();
      await expect(
        panel.getByRole("heading", { name: "Saved result accepted" }),
      ).toBeVisible();
      await expect(panel).toContainText("paused for its recorded follow-up");
      expect(state.posts).toBe(1);
    });
test("a lost POST keeps the same saved action across reload and exact readback", async ({
  page,
}) => {
  const state = await setup(page),
    panel = await review(page);
  state.lost = true;
  await panel.getByRole("checkbox").check();
  await panel.getByRole("button", { name: "Accept saved result" }).click();
  await expect(panel.getByRole("alert")).toContainText("no replacement action");
  const original = state.action;
  state.readStatus = 404;
  await page.reload();
  await open(page);
  const current = page.getByRole("region", {
    name: "Saved agent result",
    exact: true,
  });
  await current.getByRole("button", { name: "Read saved action" }).click();
  await expect(current.getByRole("alert")).toContainText(
    "no replacement action",
  );
  expect(state.posts).toBe(1);
  expect(state.action).toBe(original);
  state.readStatus = 200;
  await current.getByRole("button", { name: "Read saved action" }).click();
  await expect(
    current.getByRole("heading", { name: "Saved result accepted" }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
test("changed admission and denied status clear the proposed action without a POST", async ({
  page,
}) => {
  const state = await setup(page);
  state.candidate.original_admission_seal = "d".repeat(64);
  const panel = page.getByRole("region", {
    name: "Saved agent result",
    exact: true,
  });
  await panel.getByRole("button", { name: "Check saved result" }).click();
  await expect(panel.getByRole("alert")).toContainText(
    "could not be confirmed",
  );
  expect(state.posts).toBe(0);
  state.readStatus = 403;
  await panel.getByRole("button", { name: "Check saved result" }).click();
  await expect(panel).toContainText("Current access could not be verified");
  await expect(panel).not.toContainText("private denial canary");
  await expect(panel.getByRole("checkbox")).toHaveCount(0);
  expect(state.posts).toBe(0);
});
