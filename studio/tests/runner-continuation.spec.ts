/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: null,
  brief: "Owned continuation fixture",
  status: "running",
  agent: "agentic",
  publish: false,
};
const command = {
  execution_id: "4".repeat(32),
  task_id: "verify",
  attempt: 1,
  command_label: "Git command",
  argument_count: 1,
  source_digest: "a".repeat(64),
  invocation_digest: "b".repeat(64),
  wal_digest: "c".repeat(64),
  binding_digest: "d".repeat(64),
};
async function setup(page: Page, mode = "lost") {
  const currentRun = {
    ...run,
    status: mode === "ended" ? "completed" : run.status,
  };
  const state = {
    posts: 0,
    reads: 0,
    request: "",
    candidate: "requires_leader_confirmation",
    commands: [command],
    readStatus: 404,
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/runner-continuations/candidates"))
      return route.fulfill({
        json: {
          version: 1,
          run_id: run.id,
          repository_id: run.repository_id,
          project_id: null,
          actor_id: "f".repeat(32),
          state: state.candidate,
          reason: "Fixture local checks only.",
          commands:
            state.candidate === "requires_leader_confirmation"
              ? state.commands
              : [],
        },
      });
    if (path.endsWith("/runner-continuations")) {
      state.posts++;
      const body = route.request().postDataJSON();
      state.request = body.request_id;
      const markers = await page.evaluate(() =>
        Object.entries(sessionStorage).filter(([key]) =>
          key.startsWith("forge.runner-continuation.v1:"),
        ),
      );
      expect(markers).toHaveLength(1);
      expect(markers[0][1]).toContain(body.request_id);
      expect(markers[0][1]).not.toContain("argv");
      if (mode === "lost") return route.abort("failed");
      if (mode === "denied")
        return route.fulfill({
          status: 403,
          json: { detail: "private denial canary" },
        });
      return route.fulfill({
        json: { unexpected: "POST response is never a receipt" },
      });
    }
    if (path.includes("/runner-continuations/")) {
      state.reads++;
      if (state.readStatus !== 200)
        return route.fulfill({
          status: state.readStatus,
          json: { detail: "private receipt canary" },
        });
      return route.fulfill({
        json: {
          id: state.request,
          run_id: run.id,
          kind: "runner_continue",
          status: "completed",
          request_digest: command.binding_digest,
          request: {
            run_id: run.id,
            execution_id: command.execution_id,
            task_id: command.task_id,
            attempt: 1,
            wal_digest: command.wal_digest,
            invocation_digest: command.invocation_digest,
            actor: { user_id: "f".repeat(32) },
            run_binding: { repository_id: run.repository_id, project_id: null },
          },
          result: { disposition: "accepted" },
        },
      });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: "Owned fixture",
            url: "/fixture",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [currentRun]
        : path.endsWith("/runs/" + run.id)
          ? currentRun
          : path.endsWith("/plan")
            ? { revision: 1, tasks: [] }
            : path.endsWith("/journal")
              ? { valid: true, entries: [] }
              : path.endsWith("/health")
                ? { status: "ok", execution_ready: false }
                : [];
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("owned-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Owned continuation fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
  return state;
}
async function review(page: Page) {
  await page
    .getByRole("button", { name: "Request continuation", exact: true })
    .click();
  await page.getByLabel("Git command · verify · attempt 1").check();
  await page
    .getByRole("button", { name: "Review original command", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review one continuation request",
      exact: true,
    }),
  ).toBeFocused();
  await page
    .getByLabel("I understand the leader must confirm this original command.")
    .check();
}
test("lost response and exact404 retain one identity across reload; only exactGET confirms admission", async ({
  page,
}) => {
  const state = await setup(page);
  await review(page);
  await page
    .getByRole("button", { name: "Send continuation request", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Check original request", exact: true }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  const request = state.request;
  await page.reload();
  await page
    .getByRole("button", { name: /Owned continuation fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
  await page
    .getByRole("button", { name: "Check original request", exact: true })
    .click();
  await expect(
    page.getByText(
      "No matching receipt is currently available. This does not prove the request was not dispatched.",
    ),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  expect(state.request).toBe(request);
  state.readStatus = 200;
  await page
    .getByRole("button", { name: "Check original request", exact: true })
    .click();
  await expect(
    page.getByText(
      "Continuation admission accepted. This does not confirm the command succeeded.",
    ),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
test("pre-admission403 retains exact tracking and never reflects private error or offers another identity", async ({
  page,
}) => {
  const s = await setup(page, "denied");
  await review(page);
  await page
    .getByRole("button", { name: "Send continuation request", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Check original request", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Request continuation", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("private denial canary")).toHaveCount(0);
  expect(s.posts).toBe(1);
});
test("source changes between review and POST cannot dispatch or mint a saved request", async ({
  page,
}) => {
  const s = await setup(page);
  await review(page);
  s.candidate = "source_changed";
  await page
    .getByRole("button", { name: "Send continuation request", exact: true })
    .click();
  await expect(
    page.getByText(
      "The original command changed. Close this review and refresh local checks.",
    ),
  ).toBeVisible();
  expect(s.posts).toBe(0);
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).some((k) =>
        k.startsWith("forge.runner-continuation.v1:"),
      ),
    ),
  ).toBe(false);
});
test("missing shared workspace is explicit and offers no continuation", async ({
  page,
}) => {
  const s = await setup(page);
  s.candidate = "source_unavailable";
  await page
    .getByRole("button", { name: "Refresh local checks", exact: true })
    .click();
  await expect(
    page.getByText(/This API host cannot verify the shared workspace/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Request continuation", exact: true }),
  ).toHaveCount(0);
  expect(s.posts).toBe(0);
});
test("failed marker storage refuses POST and closing review restores surviving context focus", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.runner-continuation.v1:"))
        throw new Error("owned fixture refusal");
      return original.call(this, key, value);
    };
  });
  const s = await setup(page);
  await review(page);
  await page
    .getByRole("button", { name: "Send continuation request", exact: true })
    .click();
  await expect(
    page.getByText(/Request storage could not be verified/),
  ).toBeVisible();
  expect(s.posts).toBe(0);
  await page
    .getByRole("button", { name: "Cancel review", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Original runner command", exact: true }),
  ).toBeFocused();
});
test("200POST still requires exact GET and later403 clears confirmation without another write", async ({
  page,
}) => {
  const s = await setup(page, "success");
  s.readStatus = 200;
  await review(page);
  await page
    .getByRole("button", { name: "Send continuation request", exact: true })
    .click();
  await expect(
    page.getByText(
      "Continuation admission accepted. This does not confirm the command succeeded.",
    ),
  ).toBeVisible();
  expect(s.reads).toBe(1);
  s.readStatus = 403;
  await page
    .getByRole("button", { name: "Check original request", exact: true })
    .click();
  await expect(
    page.getByText(
      "Continuation admission accepted. This does not confirm the command succeeded.",
    ),
  ).toHaveCount(0);
  await expect(
    page.getByText(/Current access could not be confirmed/),
  ).toBeVisible();
  expect(s.posts).toBe(1);
});
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 390, height: 600 },
  { width: 844, height: 390 },
])
  for (const theme of ["light", "dark"]) {
    test(`review keeps painted controls and keyboard context ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      const state = await setup(page);
      state.commands = [
        command,
        {
          ...command,
          execution_id: command.execution_id.slice(0, 12) + "5".repeat(20),
          argument_count: 3,
        },
      ];
      await page
        .getByRole("button", { name: "Refresh local checks", exact: true })
        .click();
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page
        .getByRole("button", { name: "Request continuation", exact: true })
        .click();
      await page
        .getByRole("radio", {
          name: new RegExp("Command 1.*" + command.execution_id),
        })
        .check();
      await page.evaluate(() => document.fonts.ready.then(() => undefined));
      await page.screenshot({
        path: testInfo.outputPath(`selection-${viewport.width}-${theme}.png`),
      });
      await page
        .getByRole("button", { name: "Review original command", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Review one continuation request",
          exact: true,
        }),
      ).toBeFocused();
      await page
        .getByLabel(
          "I understand the leader must confirm this original command.",
        )
        .check();
      const dialog = page.getByRole("dialog", {
          name: "Request original command continuation",
        }),
        action = dialog.getByRole("button", {
          name: "Send continuation request",
          exact: true,
        });
      await action.scrollIntoViewIfNeeded();
      await expect(action).toBeVisible();
      await expect(action).toBeEnabled();
      const box = await action.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.height).toBeGreaterThanOrEqual(36);
      expect(
        await action.evaluate((node) => getComputedStyle(node).fontFamily),
      ).toContain("Manrope");
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.y).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
      expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
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
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(`review-${viewport.width}-${theme}.png`),
      });
      await dialog
        .getByRole("button", { name: "Cancel review", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "Request continuation", exact: true }),
      ).toBeFocused();
    });
  }

test("a known finished run never offers the original command request", async ({
  page,
}) => {
  const s = await setup(page, "ended");
  await expect(
    page.getByRole("button", { name: "Request continuation", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText(
      "This run is not active; no new continuation request is offered.",
    ),
  ).toBeVisible();
  expect(s.posts).toBe(0);
});

test("a changed actor cannot inherit the retained marker or private receipt", async ({
  page,
}) => {
  const s = await setup(page);
  await review(page);
  await page
    .getByRole("button", { name: "Send continuation request", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Check original request", exact: true }),
  ).toBeVisible();
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "e".repeat(32),
          display_name: "Different fixture operator",
          email: null,
          authentication_method: "local_bootstrap",
        },
        global_admin: true,
        memberships: [],
        roles: {},
      },
    }),
  );
  await page.reload();
  await page
    .getByRole("button", { name: /Owned continuation fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
  await expect(
    page.getByText("Original request retained", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Check original request", exact: true }),
  ).toHaveCount(0);
  expect(s.posts).toBe(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(sessionStorage).filter((k) =>
          k.startsWith("forge.runner-continuation.v1:"),
        ).length,
    ),
  ).toBe(1);
});

test("exhausted local inspection budget offers exact saved reads without a fresh request", async ({
  page,
}) => {
  const s = await setup(page);
  s.candidate = "read_budget_exceeded";
  await page
    .getByRole("button", { name: "Refresh local checks", exact: true })
    .click();
  await expect(
    page.getByText(
      "Bounded local inspection is unavailable. No continuation is offered; exact saved requests remain readable.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Request continuation", exact: true }),
  ).toHaveCount(0);
  expect(s.posts).toBe(0);
});

test("same-family commands retain distinct complete identities in a natural-height review", async ({
  page,
}) => {
  const s = await setup(page);
  const second = {
    ...command,
    execution_id: command.execution_id.slice(0, 12) + "5".repeat(20),
    argument_count: 3,
  };
  s.commands = [command, second];
  await page
    .getByRole("button", { name: "Refresh local checks", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Request continuation", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  const first = dialog.getByRole("radio", {
    name: new RegExp("Command 1.*" + command.execution_id),
  });
  const next = dialog.getByRole("radio", {
    name: new RegExp("Command 2.*" + second.execution_id),
  });
  await expect(first).toHaveCount(1);
  await expect(next).toHaveCount(1);
  await next.check();
  await expect(first).not.toBeChecked();
  await dialog
    .getByRole("button", { name: "Review original command", exact: true })
    .click();
  await dialog.getByText("Original command identity", { exact: true }).click();
  await expect(
    dialog.getByText(second.execution_id, { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Argument count: 3", { exact: true }),
  ).toBeVisible();
  expect((await dialog.boundingBox())!.height).toBeLessThan(700);
  expect(s.posts).toBe(0);
});
