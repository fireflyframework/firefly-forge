/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Fictional, fully intercepted API responses exercise Studio only.
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";

test("administrator enables final-result recovery without changing retained policy options", async ({
  page,
}) => {
  const state = await setup(page, "valid", "light");
  await navigate(page, "Connections");
  const settings = page.getByRole("region", {
    name: "Saved-work recovery settings",
    exact: true,
  });
  await settings
    .getByRole("button", { name: "Review continuation settings", exact: true })
    .click();
  const control = settings.getByLabel(
    "Allow reviewed final-result continuations",
    { exact: true },
  );
  await expect(control).not.toBeChecked();
  await control.check();
  await settings
    .getByRole("button", { name: "Save continuation settings", exact: true })
    .click();
  await expect(settings.getByRole("status")).toContainText("Settings saved");
  expect(
    (state.policy.settings as Record<string, unknown>)
      .allow_final_outcome_continuation,
  ).toBe(true);
  expect(state.policy.settings.max_window_seconds).toBe(180);
  expect(state.policyWrites).toBe(1);
  expect(state.posts).toBe(0);
  expect(state.checkpointPosts).toBe(0);
});
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
const checkpointCandidate = {
  version: 1,
  kind: "checkpoint_continuation_candidate",
  run_id: run.id,
  request_id: run.invalid_read_continuity.request_id,
  digest: h,
  admission: "READY",
  holds: [],
  execution_authorized: false,
  original_admission_seal: h,
  attempt: 2,
  ticket_digest: h,
  events_digest: h,
  journal_digest: h,
  run_digest: h,
  task_digest: h,
  checkpoint_record_digest: h,
  response_record_digest: h,
  source_digest: h,
  current_runtime_contract: h,
  current_profile: { id: "fictional-qualified" },
  policy_digest: h,
  frozen_timeout_seconds: 600,
  maximum_window_seconds: 180,
  original_deadline_ms: 1793200000000,
  usage: {
    input_tokens: 8,
    cache_write_tokens: 0,
    cache_read_tokens: 0,
    output_tokens: 12,
    input_audio_tokens: 0,
    cache_audio_read_tokens: 0,
    output_audio_tokens: 0,
    audio_seconds: 0,
    details: {},
    cost: null,
    requests: 2,
    tool_calls: 2,
  },
  limits: {
    cost_limit: null,
    request_limit: 8,
    tool_calls_limit: 20,
    input_tokens_limit: null,
    output_tokens_limit: null,
    total_tokens_limit: null,
    per_request_input_tokens_limit: null,
    count_tokens_before_request: false,
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
    readonly: {
      version: 1,
      kind: "readonly_observation_recovery_status",
      run_id: run.id,
      request_id: run.invalid_read_continuity.request_id,
      recovery_id: null,
      recovery_record_digest: null,
      status: "not_recovered",
      original_admission_seal: h,
      lease: null,
    } as Record<string, unknown>,
    readonlyCode: 200,
    readonlyReads: 0,
    legacyReads: 0,
    admissionSeal: h,
    checkpoint: {
      version: 1,
      kind: "checkpoint_continuation_status",
      run_id: run.id,
      request_id: run.invalid_read_continuity.request_id,
      status: "not_recovered",
      action_id: null,
      witness_digest: null,
      original_admission_seal: h,
      lease: null,
    } as Record<string, unknown>,
    checkpointCandidate: structuredClone(checkpointCandidate) as Record<
      string,
      unknown
    >,
    checkpointCode: 409,
    checkpointStatusCode: 200,
    checkpointPosts: 0,
    checkpointCandidateReads: 0,
    checkpointFollowPolicy: false,
    checkpointMode: "",
    policy: {
      version: 1,
      revision: "f".repeat(32),
      digest: h,
      settings: {
        version: 1,
        enabled: true,
        allow_unavailable_source_observations: true,
        allow_pre_admission_recovery: true,
        allow_readonly_observation_recovery: true,
        allow_checkpoint_continuation: false,
        max_window_seconds: 180,
        max_tickets_per_run: 73,
        max_pending_tickets: 601,
      },
    },
    policyWrites: 0,
    policyLost: false,
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
    if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
      return;
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/invalid-read-continuity/policy")) {
      if (state.deny)
        return route.fulfill({
          status: 403,
          json: { detail: "private-policy-canary" },
        });
      if (req.method() === "PUT") {
        state.policyWrites++;
        const input = req.postDataJSON();
        expect(input.max_tickets_per_run).toBe(73);
        expect(input.max_pending_tickets).toBe(601);
        expect(input.allow_readonly_observation_recovery).toBe(true);
        state.policy = {
          version: 1,
          revision: "d".repeat(32),
          digest: "d".repeat(64),
          settings: input,
        };
        if (state.policyLost) return route.abort("failed");
      }
      return route.fulfill({ json: state.policy });
    }
    if (path.includes("/checkpoint-continuations/")) {
      if (state.deny)
        return route.fulfill({
          status: 403,
          json: { detail: "private-generic-canary" },
        });
      if (path.endsWith("/candidate")) {
        state.checkpointCandidateReads++;
        const value = state.checkpointFollowPolicy
          ? {
              ...state.checkpointCandidate,
              admission:
                state.policy.settings.allow_checkpoint_continuation &&
                state.policy.settings.enabled
                  ? "READY"
                  : "HOLD",
              holds:
                state.policy.settings.allow_checkpoint_continuation &&
                state.policy.settings.enabled
                  ? []
                  : ["checkpoint_continuation_disabled"],
            }
          : state.checkpointCandidate;
        return route.fulfill({
          status: state.checkpointCode,
          json:
            state.checkpointCode === 200
              ? value
              : { detail: "private-not-pending" },
        });
      }
      if (path.includes("/actions/")) {
        state.checkpointPosts++;
        const body = req.postDataJSON();
        expect(Object.keys(body)).toHaveLength(7);
        expect(body).toMatchObject({
          version: 1,
          candidate_digest: h,
          acknowledge_retained_response_consumed_once: true,
          acknowledge_no_completed_effect_replay: true,
          acknowledge_same_request_attempt_and_budgets: true,
          acknowledge_finite_ownership_lease: true,
        });
        const action = path.split("/").at(-1)!;
        const marker = await page.evaluate(
          () =>
            Object.entries(localStorage).find(([key]) =>
              key.startsWith("forge.checkpoint-continuation.v1:"),
            )?.[1],
        );
        expect(marker).toContain(action);
        expect(marker).not.toContain("fictional-ui-token");
        if (state.checkpointMode === "stale")
          return route.fulfill({
            status: 409,
            json: { detail: "private-stale-candidate" },
          });
        Object.assign(state.checkpoint, {
          action_id: action,
          witness_digest: h,
          status: "queued",
          lease: {},
        });
        if (state.checkpointMode === "lost") return route.abort("failed");
      }
      return route.fulfill({
        status: state.checkpointStatusCode,
        json: state.checkpoint,
      });
    }
    if (path.includes("/invalid-read-continuity/")) {
      if (state.deny)
        return route.fulfill({
          status: 403,
          json: { detail: "private-denial-canary" },
        });
      if (path.endsWith("/readonly-observation-recovery")) {
        expect(req.method()).toBe("GET");
        state.readonlyReads++;
        return route.fulfill({
          status: state.readonlyCode,
          json: state.readonly,
        });
      }
      state.legacyReads++;
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
          original_admission_seal: state.admissionSeal,
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
  await expectAuthenticated(page);
  await navigate(page, "Factory");
  await open(page);
  return state;
}
async function open(page: Page) {
  await openFactoryRun(page, /Saved continuity fixture/);
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

const readonlyStates = [
  ["queued", "Continuation queued", "waiting for a worker"],
  ["executing", "Continuation in progress", "worker has claimed"],
  ["completed", "Continuation completed", "worker reported completion"],
  ["failed", "Continuation failed", "worker reported a failure"],
  ["needs_reconciliation", "Continuation needs review", "needs reconciliation"],
] as const;
function savedReadonly(
  state: Awaited<ReturnType<typeof setup>>,
  status: string,
) {
  Object.assign(state.readonly, {
    recovery_id: "e".repeat(32),
    recovery_record_digest: h,
    status,
    lease: {},
  });
}
for (const [status, title, detail] of readonlyStates)
  test(`saved unavailable-read ${status} is read-only and receives keyboard focus`, async ({
    page,
  }) => {
    const state = await setup(page);
    savedReadonly(state, status);
    const check = panel(page).getByRole("button", {
      name: "Check current admission",
      exact: true,
    });
    await check.focus();
    await page.keyboard.press("Enter");
    const heading = panel(page).getByRole("heading", {
      name: title,
      exact: true,
    });
    await expect(heading).toBeFocused();
    await expect(panel(page).getByRole("status")).toContainText(detail);
    await expect(panel(page)).toContainText("Unavailable read recovery");
    await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
    await expect(panel(page).getByRole("button")).toHaveCount(1);
    expect(state.readonlyReads).toBe(1);
    expect(state.legacyReads).toBe(0);
    expect(state.posts).toBe(0);
    for (const value of [h, run.id, "e".repeat(32), "private"])
      expect(await panel(page).innerText()).not.toContain(value);
  });
for (const [width, theme] of [
  [390, "light"],
  [390, "dark"],
  [1440, "light"],
  [1440, "dark"],
] as const)
  test(`unavailable-read status refresh fits ${width}px ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page, "valid", theme);
    savedReadonly(state, "executing");
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(
      panel(page).getByRole("heading", {
        name: "Continuation in progress",
        exact: true,
      }),
    ).toBeFocused();
    expect(
      await panel(page).evaluate(
        (node) => node.scrollWidth > node.clientWidth + 1,
      ),
    ).toBe(false);
    await panel(page).screenshot({
      path: info.outputPath(`readonly-status-${width}-${theme}.png`),
    });
    state.readonly.status = "completed";
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(
      panel(page).getByRole("heading", {
        name: "Continuation completed",
        exact: true,
      }),
    ).toBeFocused();
    expect(state.posts).toBe(0);
    expect(state.legacyReads).toBe(0);
  });
for (const mode of ["denied", "unavailable", "wrong_request", "bad_receipt"])
  test(`unavailable-read ${mode} hides stale details and cannot enter legacy recovery`, async ({
    page,
  }) => {
    const state = await setup(page);
    savedReadonly(state, "executing");
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(
      panel(page).getByRole("heading", {
        name: "Continuation in progress",
        exact: true,
      }),
    ).toBeVisible();
    if (mode === "denied") state.readonlyCode = 403;
    else if (mode === "unavailable") state.readonlyCode = 404;
    else if (mode === "wrong_request")
      state.readonly.request_id = "f".repeat(32);
    else state.readonly.recovery_record_digest = "private-malformed-canary";
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(panel(page).getByRole("alert")).toBeVisible();
    await expect(
      panel(page).getByRole("heading", {
        name: "Continuation in progress",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
    await expect(panel(page).getByRole("button")).toHaveCount(1);
    expect(await panel(page).innerText()).not.toContain(
      "private-malformed-canary",
    );
    expect(state.legacyReads).toBe(0);
    expect(state.posts).toBe(0);
  });

test("different admissions cannot unlock the unclaimed recovery flow", async ({
  page,
}) => {
  const state = await setup(page);
  state.admissionSeal = "d".repeat(64);
  await panel(page)
    .getByRole("button", { name: "Check current admission", exact: true })
    .click();
  await expect(panel(page).getByRole("alert")).toBeVisible();
  await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
  await expect(panel(page).getByRole("button")).toHaveCount(1);
  expect(state.posts).toBe(0);
});

async function checkpointReview(
  page: Page,
  state: Awaited<ReturnType<typeof setup>>,
) {
  state.checkpointCode = 200;
  state.policy.settings.allow_checkpoint_continuation = true;
  await panel(page)
    .getByRole("button", { name: "Check current admission", exact: true })
    .click();
  await panel(page)
    .getByRole("button", { name: "Review saved continuation", exact: true })
    .click();
}
async function acknowledgeCheckpoint(page: Page) {
  for (const box of await panel(page).getByRole("checkbox").all())
    await box.check();
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`guided checkpoint continuation frames at ${width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      const state = await setup(page, "valid", theme);
      state.checkpointCode = 200;
      state.checkpointFollowPolicy = true;
      state.checkpointCandidate.maximum_window_seconds = 120;
      await navigate(page, "Connections");
      const settings = page.getByRole("region", {
        name: "Saved-work recovery settings",
        exact: true,
      });
      await settings
        .getByRole("button", {
          name: "Review continuation settings",
          exact: true,
        })
        .click();
      const toggle = settings.getByLabel(
        "Allow reviewed checkpoint continuations",
        { exact: true },
      );
      await expect(toggle).not.toBeChecked();
      await toggle.check();
      await settings
        .getByLabel("Maximum time window (seconds)", { exact: true })
        .fill("120");
      const capture = (
        stage: string,
        actionSelector: string,
        focusSelector: string,
      ) =>
        captureWalkthrough(
          page,
          stage,
          theme,
          width === 390 ? "mobile" : "desktop",
          {
            producer: "studio/tests/invalid-read-recovery.spec.ts",
            resetFields: false,
            actionSelector,
            focusSelector,
          },
        );
      await capture(
        "checkpoint-continuation-policy",
        ".continuity-policy-settings button.primary",
        ".continuity-policy-settings",
      );
      await settings
        .getByRole("button", {
          name: "Save continuation settings",
          exact: true,
        })
        .click();
      await expect(settings.getByRole("status")).toContainText(
        "Settings saved",
      );
      expect(state.policyWrites).toBe(1);
      expect(state.checkpointPosts).toBe(0);
      await navigate(page, "Factory");
      await open(page);
      await panel(page)
        .getByRole("button", { name: "Check current admission", exact: true })
        .click();
      await panel(page)
        .getByRole("button", { name: "Review saved continuation", exact: true })
        .click();
      await panel(page)
        .getByLabel("Time window (seconds)", { exact: true })
        .fill("60");
      await acknowledgeCheckpoint(page);
      const submit = panel(page).getByRole("button", {
        name: "Continue saved task",
        exact: true,
      });
      await expect(submit).toBeEnabled();
      await capture(
        "checkpoint-continuation-review",
        ".agent-checkpoint-review button.primary",
        ".agent-checkpoint-review",
      );
      await submit.click();
      await expect(
        panel(page).getByRole("heading", {
          name: "Continuation queued",
          exact: true,
        }),
      ).toBeFocused();
      await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
      await expect(panel(page).getByRole("button")).toHaveCount(1);
      await capture(
        "checkpoint-continuation-status",
        'section[aria-label="Saved continuation recovery"] button',
        'section[aria-label="Saved continuation recovery"]',
      );
      expect(state.checkpointPosts).toBe(1);
      expect(state.posts).toBe(0);
      expect(state.readonlyReads).toBe(0);
      expect(state.legacyReads).toBe(0);
    });
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`checkpoint review submits once at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const state = await setup(page, "valid", theme);
      await checkpointReview(page, state);
      await expect(
        panel(page).getByRole("heading", {
          name: "Review saved continuation",
          exact: true,
        }),
      ).toBeFocused();
      await expect(panel(page)).toContainText(
        "Completed changes stay in place",
      );
      await panel(page)
        .getByRole("button", { name: "Back", exact: true })
        .click();
      await expect(
        panel(page).getByRole("button", {
          name: "Review saved continuation",
          exact: true,
        }),
      ).toBeFocused();
      await panel(page)
        .getByRole("button", { name: "Review saved continuation", exact: true })
        .click();
      const window = panel(page).getByLabel("Time window (seconds)", {
        exact: true,
      });
      const submit = panel(page).getByRole("button", {
        name: "Continue saved task",
        exact: true,
      });
      await window.fill("181");
      await acknowledgeCheckpoint(page);
      await expect(submit).toBeDisabled();
      await window.fill("60");
      await expect(panel(page).getByRole("checkbox").last()).not.toBeChecked();
      await acknowledgeCheckpoint(page);
      await expect(submit).toBeEnabled();
      expect(
        await panel(page).evaluate(
          (node) => node.scrollWidth > node.clientWidth + 1,
        ),
      ).toBe(false);
      await panel(page).screenshot({
        path: info.outputPath(`checkpoint-review-${width}-${theme}.png`),
      });
      await submit.evaluate((node) => {
        node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        node.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      });
      await expect(
        panel(page).getByRole("heading", {
          name: "Continuation queued",
          exact: true,
        }),
      ).toBeFocused();
      await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
      await expect(panel(page).getByRole("button")).toHaveCount(1);
      expect(state.checkpointPosts).toBe(1);
      expect(state.posts).toBe(0);
      expect(state.readonlyReads).toBe(0);
      expect(state.legacyReads).toBe(0);
      await panel(page)
        .getByRole("button", { name: "Check saved continuation", exact: true })
        .click();
      expect(state.checkpointPosts).toBe(1);
    });
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`administrator configures checkpoint recovery at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const state = await setup(page, "valid", theme);
      await navigate(page, "Connections");
      const settings = page.getByRole("region", {
        name: "Saved-work recovery settings",
        exact: true,
      });
      await settings
        .getByRole("button", {
          name: "Review continuation settings",
          exact: true,
        })
        .click();
      const toggle = settings.getByLabel(
        "Allow reviewed checkpoint continuations",
        { exact: true },
      );
      await expect(toggle).not.toBeChecked();
      await toggle.check();
      await settings
        .getByLabel("Maximum time window (seconds)", { exact: true })
        .fill("120");
      expect(
        await settings.evaluate(
          (node) => node.scrollWidth > node.clientWidth + 1,
        ),
      ).toBe(false);
      await settings.screenshot({
        path: info.outputPath(`continuity-policy-${width}-${theme}.png`),
      });
      await settings
        .getByRole("button", {
          name: "Save continuation settings",
          exact: true,
        })
        .click();
      await expect(settings.getByRole("status")).toContainText(
        "Settings saved",
      );
      expect(state.policyWrites).toBe(1);
      expect(state.policy.settings.allow_checkpoint_continuation).toBe(true);
      expect(state.policy.settings.max_window_seconds).toBe(120);
      expect(state.checkpointPosts).toBe(0);
    });
test("uncertain policy save offers readback and never repeats PUT", async ({
  page,
}) => {
  const state = await setup(page);
  state.policyLost = true;
  await navigate(page, "Connections");
  const settings = page.getByRole("region", {
    name: "Saved-work recovery settings",
    exact: true,
  });
  await settings
    .getByRole("button", { name: "Review continuation settings", exact: true })
    .click();
  await settings
    .getByLabel("Allow reviewed checkpoint continuations", { exact: true })
    .check();
  await settings
    .getByRole("button", { name: "Save continuation settings", exact: true })
    .click();
  await expect(settings.getByRole("alert")).toContainText("not confirmed");
  await expect(
    settings.getByRole("button", {
      name: "Save continuation settings",
      exact: true,
    }),
  ).toHaveCount(0);
  await settings
    .getByRole("button", { name: "Check saved settings", exact: true })
    .click();
  await expect(settings.getByRole("status")).toContainText("currently saved");
  expect(state.policyWrites).toBe(1);
});
for (const mode of ["lost", "stale"])
  test(`checkpoint ${mode} response retains one identity across readback and reload`, async ({
    page,
  }) => {
    const state = await setup(page);
    state.checkpointMode = mode;
    await checkpointReview(page, state);
    await acknowledgeCheckpoint(page);
    await panel(page)
      .getByRole("button", { name: "Continue saved task", exact: true })
      .click();
    await expect(panel(page).getByRole("alert")).toContainText(
      "has not been confirmed",
    );
    await page.reload();
    await open(page);
    if (mode === "lost") state.checkpointStatusCode = 404;
    await panel(page)
      .getByRole("button", { name: "Check saved continuation", exact: true })
      .click();
    await expect(panel(page).getByRole("alert")).toBeVisible();
    await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
    await expect(panel(page).getByRole("button")).toHaveCount(1);
    expect(state.checkpointPosts).toBe(1);
    expect(state.posts).toBe(0);
    if (mode === "lost") {
      state.checkpointStatusCode = 200;
      await panel(page)
        .getByRole("button", { name: "Check saved continuation", exact: true })
        .click();
      await expect(
        panel(page).getByRole("heading", {
          name: "Continuation queued",
          exact: true,
        }),
      ).toBeVisible();
    }
  });
for (const [status, title] of readonlyStates)
  test(`saved generic ${status} exposes only current readback`, async ({
    page,
  }) => {
    const state = await setup(page);
    Object.assign(state.checkpoint, {
      status,
      action_id: "e".repeat(32),
      witness_digest: h,
      lease: {},
    });
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(
      panel(page).getByRole("heading", { name: title, exact: true }),
    ).toBeFocused();
    await expect(panel(page).getByRole("button")).toHaveCount(1);
    await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
    expect(state.checkpointCandidateReads).toBe(0);
    expect(state.readonlyReads).toBe(0);
  });
for (const mode of ["denied", "wrong_request", "malformed_status"])
  test(`generic ${mode} cannot fall back or expose prior review`, async ({
    page,
  }) => {
    const state = await setup(page);
    await checkpointReview(page, state);
    if (mode === "denied") state.deny = true;
    else if (mode === "wrong_request")
      state.checkpointCandidate.request_id = "e".repeat(32);
    else state.checkpoint.action_id = "private-incomplete-action";
    await panel(page)
      .getByRole("button", { name: "Check current admission", exact: true })
      .click();
    await expect(panel(page).getByRole("alert")).toBeVisible();
    await expect(panel(page).getByRole("checkbox")).toHaveCount(0);
    expect(await panel(page).innerText()).not.toContain("private-");
    expect(state.checkpointPosts).toBe(0);
    expect(state.readonlyReads).toBe(0);
    expect(state.legacyReads).toBe(0);
  });
test("late generic candidate cannot reappear after leaving the run", async ({
  page,
}) => {
  const state = await setup(page);
  state.checkpointCode = 200;
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/checkpoint-continuations/**/candidate",
    async (route) => {
      await held;
      await route.fulfill({ json: state.checkpointCandidate }).catch(() => {});
    },
  );
  await panel(page)
    .getByRole("button", { name: "Check current admission", exact: true })
    .click();
  await navigate(page, "Connections");
  release();
  await expect(
    page.getByRole("heading", {
      name: "Review saved continuation",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.checkpointPosts).toBe(0);
});
test("a changed policy is refreshed before save and cannot be overwritten from an old form", async ({
  page,
}) => {
  const state = await setup(page);
  await navigate(page, "Connections");
  const settings = page.getByRole("region", {
    name: "Saved-work recovery settings",
    exact: true,
  });
  await settings
    .getByRole("button", { name: "Review continuation settings", exact: true })
    .click();
  await settings
    .getByLabel("Allow reviewed checkpoint continuations", { exact: true })
    .check();
  state.policy.digest = "e".repeat(64);
  state.policy.settings.max_window_seconds = 90;
  await settings
    .getByRole("button", { name: "Save continuation settings", exact: true })
    .click();
  await expect(settings.getByRole("alert")).toContainText(
    "changed in another session",
  );
  await expect(
    settings.getByLabel("Maximum time window (seconds)", { exact: true }),
  ).toHaveValue("90");
  expect(state.policyWrites).toBe(0);
});
test("policy revocation clears the editable form and private advanced record", async ({
  page,
}) => {
  const state = await setup(page);
  await navigate(page, "Connections");
  const settings = page.getByRole("region", {
    name: "Saved-work recovery settings",
    exact: true,
  });
  await settings
    .getByRole("button", { name: "Review continuation settings", exact: true })
    .click();
  state.deny = true;
  await settings
    .getByRole("button", { name: "Refresh saved settings", exact: true })
    .click();
  await expect(settings.getByRole("alert")).toContainText(
    "Private settings are hidden",
  );
  await expect(settings.getByRole("checkbox")).toHaveCount(0);
  await expect(
    settings.getByText("Advanced record", { exact: true }),
  ).toHaveCount(0);
  expect(state.policyWrites).toBe(0);
});

test("an administrator enables the held policy then explicitly continues the same saved task", async ({
  page,
}) => {
  const state = await setup(page);
  state.checkpointCode = 200;
  state.checkpointFollowPolicy = true;
  await panel(page)
    .getByRole("button", { name: "Check current admission", exact: true })
    .click();
  await expect(
    panel(page).getByRole("heading", {
      name: "Continuation held",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel(page).getByRole("button", {
      name: "Review saved continuation",
      exact: true,
    }),
  ).toHaveCount(0);
  await navigate(page, "Connections");
  const settings = page.getByRole("region", {
    name: "Saved-work recovery settings",
    exact: true,
  });
  await settings
    .getByRole("button", { name: "Review continuation settings", exact: true })
    .click();
  await settings
    .getByLabel("Allow reviewed checkpoint continuations", { exact: true })
    .check();
  await settings
    .getByRole("button", { name: "Save continuation settings", exact: true })
    .click();
  await expect(settings.getByRole("status")).toContainText("Settings saved");
  expect(state.checkpointPosts).toBe(0);
  await navigate(page, "Factory");
  await open(page);
  await checkpointReview(page, state);
  await acknowledgeCheckpoint(page);
  await panel(page)
    .getByRole("button", { name: "Continue saved task", exact: true })
    .click();
  await expect(
    panel(page).getByRole("heading", {
      name: "Continuation queued",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.policyWrites).toBe(1);
  expect(state.checkpointPosts).toBe(1);
  expect(state.posts).toBe(0);
});
