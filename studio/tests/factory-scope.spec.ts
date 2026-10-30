/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
const actorA = "f".repeat(32),
  actorB = "e".repeat(32);
const a = "a".repeat(32),
  b = "b".repeat(32);
function gate() {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { pending, release };
}
async function fixture(page: Page) {
  let allowB = false,
    failRead = false,
    failDetail = false,
    failIdentity = false,
    failAction = false;
  let actor = actorA,
    writes = 0,
    authReads = 0;
  const holds = new Map<string, ReturnType<typeof gate>>();
  const reads = new Map<string, number>(),
    settled = new Map<string, number>();
  const repositories = [a, b].map((id) => ({
    id,
    name: id === a ? "Project A" : "Project B",
    url: `https://github.com/example/${id}`,
    provider: "github",
    default_branch: "main",
  }));
  const runs = [
    ["a-1", a],
    ["b-2", b],
  ].map(([id, repository_id]) => ({
    id,
    repository_id,
    brief: `Recorded ${id}`,
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "succeeded",
    changed_files: [],
    verification: [],
    artifacts: [],
    pr_url: "",
  }));
  const repositoryC = {
    ...repositories[0],
    id: "c".repeat(32),
    name: "Project C",
  };
  const runC = {
    ...runs[0],
    id: "c-1",
    repository_id: repositoryC.id,
    brief: "Recorded c-1",
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/session") && failIdentity) {
      authReads++;
      await holds.get("auth")?.pending;
      await route.fulfill({
        status: 503,
        json: { detail: "Public fixture session read unavailable" },
      });
      return;
    }
    if (path.endsWith("/auth/session") && actor === actorB) {
      authReads++;
      await holds.get("auth")?.pending;
      await route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: actorB,
            display_name: "Other operator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: true,
          memberships: [],
          roles: {},
        },
      });
      return;
    }
    if (await mockIdentity(route)) return;
    if (route.request().method() !== "GET") writes++;
    const scope = path.startsWith("/second/") ? "apiB" : actor;
    if (path.endsWith("/runs/b-2/publish")) {
      reads.set("action", (reads.get("action") ?? 0) + 1);
      await holds.get("action")?.pending;
      await route.fulfill(
        failAction
          ? { status: 403, json: { detail: "Prior-actor public error marker" } }
          : {
              json: {
                ...runs[1],
                pr_url: "https://github.com/example/project/pull/1",
              },
            },
      );
      settled.set("action", (settled.get("action") ?? 0) + 1);
      return;
    }
    let json: unknown = [];
    if (path.endsWith("/repositories"))
      json = scope === actorA ? repositories : allowB ? [repositoryC] : [];
    else if (path.endsWith("/runs"))
      json = scope === actorA ? runs : allowB ? [runC] : [];
    else if (path.endsWith("/plan"))
      json = {
        tasks: [
          {
            id: "private-plan",
            title: scope === actorA ? "Previous actor plan" : "New actor plan",
            status: "done",
            instructions: "Fictional prior actor content",
          },
        ],
      };
    else if (path.endsWith("/health")) json = { execution_ready: true };
    else if (runs.some((run) => path.endsWith(`/runs/${run.id}`)))
      json =
        scope === actorA
          ? runs.find((run) => path.endsWith(`/runs/${run.id}`))
          : null;
    if (scope !== actorA && path.endsWith("/runs/c-1") && allowB) json = runC;
    const snapshot = structuredClone(json);
    if (path.endsWith("/repositories")) {
      reads.set(scope, (reads.get(scope) ?? 0) + 1);
      await holds.get(scope)?.pending;
    }
    if (path.endsWith("/runs/b-2")) {
      reads.set("detail", (reads.get("detail") ?? 0) + 1);
      await holds.get("detail")?.pending;
    }
    if (path.endsWith("/runs/c-1")) {
      reads.set("new-detail", (reads.get("new-detail") ?? 0) + 1);
      await holds.get("new-detail")?.pending;
    }
    if (
      scope === actorA &&
      ((path.endsWith("/repositories") && failRead) ||
        (path.endsWith("/runs/b-2") && failDetail))
    ) {
      await route.fulfill({
        status: 403,
        json: { detail: "Prior-actor public error marker" },
      });
    } else await route.fulfill({ json: snapshot });
    if (path.endsWith("/runs/c-1"))
      settled.set("new-detail", (settled.get("new-detail") ?? 0) + 1);
    if (path.endsWith("/runs/b-2"))
      settled.set("detail", (settled.get("detail") ?? 0) + 1);
    if (path.endsWith("/repositories"))
      settled.set(scope, (settled.get(scope) ?? 0) + 1);
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Factory");
  await page.getByLabel("Project", { exact: true }).selectOption(b);
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Selected run tasks" }),
  ).toContainText("Previous actor plan");
  return {
    hold: (scope: string) => {
      const value = gate();
      holds.set(scope, value);
      return value;
    },
    releaseAll: () => holds.forEach((value) => value.release()),
    reads: (scope: string) => reads.get(scope) ?? 0,
    settled: (scope: string) => settled.get(scope) ?? 0,
    authReads: () => authReads,
    switchActor: () => {
      actor = actorB;
    },
    writes: () => writes,
    grantActorB: () => {
      allowB = true;
    },
    rejectRead: () => {
      failRead = true;
    },
    rejectDetail: () => {
      failDetail = true;
    },
    rejectIdentity: () => {
      failIdentity = true;
    },
    rejectAction: () => {
      failAction = true;
    },
  };
}
async function changeActor(
  page: Page,
  state: Awaited<ReturnType<typeof fixture>>,
) {
  state.switchActor();
  await page.evaluate(() =>
    window.dispatchEvent(new Event("forge-session-expired")),
  );
}
async function emptyFactory(page: Page) {
  await expectAuthenticated(page);
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("");
  await expect(page.locator(".production-floor")).toContainText(
    "No connected projects",
  );
  await expect(page.locator(".production-floor")).not.toContainText(
    "Previous actor plan",
  );
  await expect(page.locator(".runs-section")).not.toContainText("Recorded b-2");
}
test("identity resolution and fresh actor data never expose the previous actor project or plan", async ({
  page,
}) => {
  const state = await fixture(page),
    auth = state.hold("auth"),
    fresh = state.hold(actorB);
  try {
    await changeActor(page, state);
    await expect.poll(state.authReads).toBeGreaterThan(0);
    await expect(page.locator(".production-floor")).toHaveCount(0);
    auth.release();
    await expect.poll(() => state.reads(actorB)).toBeGreaterThan(0);
    await expect(page.locator(".production-floor")).toHaveCount(0);
    fresh.release();
    await emptyFactory(page);
    expect(state.writes()).toBe(0);
  } finally {
    state.releaseAll();
  }
});
for (const mode of ["actor", "API"] as const)
  test(`late prior-scope response cannot replace fresh ${mode} resources`, async ({
    page,
  }) => {
    const state = await fixture(page),
      old = state.hold(actorA);
    const oldCount = state.reads(actorA),
      settled = state.settled(actorA);
    try {
      await page
        .locator(".runs-section")
        .getByRole("button", { name: "Refresh", exact: true })
        .click();
      await expect.poll(() => state.reads(actorA)).toBeGreaterThan(oldCount);
      if (mode === "actor") await changeActor(page, state);
      else {
        await navigate(page, "Connections");
        await page
          .getByRole("button", { name: "Workspace", exact: true })
          .click();
        const base = new URL("/second/api/v1", page.url()).href;
        await page.getByLabel("API address", { exact: true }).fill(base);
        await page
          .getByLabel("API token", { exact: true })
          .fill("test-bootstrap-at-least-24-characters");
        const session = page.waitForResponse(`${base}/auth/session`);
        await page
          .getByRole("button", { name: "Load server", exact: true })
          .click();
        await session;
        await navigate(page, "Factory");
      }
      await expect
        .poll(() => state.reads(mode === "actor" ? actorB : "apiB"))
        .toBeGreaterThan(0);
      await emptyFactory(page);
      old.release();
      await expect.poll(() => state.settled(actorA)).toBeGreaterThan(settled);
      await emptyFactory(page);
      expect(state.writes()).toBe(0);
    } finally {
      state.releaseAll();
    }
  });

test("late prior-actor run detail cannot reappear after a fresh empty actor load", async ({
  page,
}) => {
  const state = await fixture(page),
    detail = state.hold("detail");
  try {
    await page
      .locator(".flow-card.selected")
      .getByRole("button", { name: "Inspect run", exact: true })
      .click();
    await expect.poll(() => state.reads("detail")).toBeGreaterThan(0);
    await changeActor(page, state);
    await expect.poll(() => state.reads(actorB)).toBeGreaterThan(0);
    await expectAuthenticated(page);
    await expect(page.locator(".run-id")).toHaveCount(0);
    detail.release();
    await expect.poll(() => state.settled("detail")).toBeGreaterThan(0);
    await expect(page.locator(".run-id")).toHaveCount(0);
    expect(state.writes()).toBe(0);
  } finally {
    state.releaseAll();
  }
});

test("late old refresh errors cannot disconnect or disclose errors under the new actor", async ({
  page,
}) => {
  const state = await fixture(page),
    old = state.hold(actorA),
    count = state.reads(actorA),
    settled = state.settled(actorA);
  state.rejectRead();
  try {
    await page
      .locator(".runs-section")
      .getByRole("button", { name: "Refresh", exact: true })
      .click();
    await expect.poll(() => state.reads(actorA)).toBeGreaterThan(count);
    await changeActor(page, state);
    await expect.poll(() => state.reads(actorB)).toBeGreaterThan(0);
    await emptyFactory(page);
    old.release();
    await expect.poll(() => state.settled(actorA)).toBeGreaterThan(settled);
    await emptyFactory(page);
    await expect(page.locator("main")).not.toContainText(
      "Prior-actor public error marker",
    );
  } finally {
    state.releaseAll();
  }
});
test("late old run error and finally cannot alter a newer actor's pending detail", async ({
  page,
}) => {
  const state = await fixture(page),
    old = state.hold("detail"),
    fresh = state.hold("new-detail");
  state.grantActorB();
  state.rejectDetail();
  try {
    await page
      .locator(".flow-card.selected")
      .getByRole("button", { name: "Inspect run", exact: true })
      .click();
    await expect.poll(() => state.reads("detail")).toBeGreaterThan(0);
    await changeActor(page, state);
    await expect.poll(() => state.reads(actorB)).toBeGreaterThan(0);
    await navigate(page, "Factory");
    await expect(page.getByLabel("Project", { exact: true })).toHaveValue(
      "c".repeat(32),
    );
    await page.getByRole("button", { name: "Board", exact: true }).click();
    await page
      .locator(".flow-card.selected")
      .getByRole("button", { name: "Inspect run", exact: true })
      .click();
    await expect.poll(() => state.reads("new-detail")).toBeGreaterThan(0);
    const publish = page.getByRole("button", {
      name: "Publish pull request",
      exact: true,
    });
    await expect(publish).toBeDisabled();
    old.release();
    await expect.poll(() => state.settled("detail")).toBeGreaterThan(0);
    await expect(page.locator("main")).not.toContainText(
      "Prior-actor public error marker",
    );
    await expect(publish).toBeDisabled();
    fresh.release();
    await expect.poll(() => state.settled("new-detail")).toBeGreaterThan(0);
    await expect(page.locator(".run-id")).toHaveText("Run c-1");
    await expect(publish).toBeEnabled();
    expect(state.writes()).toBe(0);
  } finally {
    state.releaseAll();
  }
});

test("failed identity validation keeps retained actor data hidden", async ({
  page,
}) => {
  const state = await fixture(page),
    auth = state.hold("auth");
  state.rejectIdentity();
  try {
    await page.evaluate(() =>
      window.dispatchEvent(new Event("forge-session-expired")),
    );
    await expect.poll(state.authReads).toBeGreaterThan(0);
    await expect(page.locator(".production-floor")).toHaveCount(0);
    auth.release();
    await navigate(page, "Connections");
    const workspace = page.getByRole("button", {
      name: "Workspace",
      exact: true,
    });
    if (await workspace.isVisible()) await workspace.click();
    await expect(
      page.getByText("Public fixture session read unavailable", {
        exact: true,
      }),
    ).toBeVisible();
    await navigate(page, "Factory");
    await expect(page.locator(".production-floor")).toHaveCount(0);
    expect(state.writes()).toBe(0);
  } finally {
    state.releaseAll();
  }
});
for (const outcome of ["success", "error"] as const)
  test(`late prior-actor action ${outcome} cannot alter current feedback or pending detail`, async ({
    page,
  }) => {
    const state = await fixture(page),
      action = state.hold("action"),
      fresh = state.hold("new-detail");
    state.grantActorB();
    if (outcome === "error") state.rejectAction();
    try {
      await page
        .locator(".flow-card.selected")
        .getByRole("button", { name: "Inspect run", exact: true })
        .click();
      const publishA = page.getByRole("button", {
        name: "Publish pull request",
        exact: true,
      });
      await expect(publishA).toBeEnabled();
      await publishA.click();
      await expect.poll(() => state.reads("action")).toBeGreaterThan(0);
      await changeActor(page, state);
      await expect.poll(() => state.reads(actorB)).toBeGreaterThan(0);
      await navigate(page, "Factory");
      await expect(page.getByLabel("Project", { exact: true })).toHaveValue(
        "c".repeat(32),
      );
      await page.getByRole("button", { name: "Board", exact: true }).click();
      await page
        .locator(".flow-card.selected")
        .getByRole("button", { name: "Inspect run", exact: true })
        .click();
      await expect.poll(() => state.reads("new-detail")).toBeGreaterThan(0);
      const publishB = page.getByRole("button", {
        name: "Publish pull request",
        exact: true,
      });
      await expect(publishB).toBeDisabled();
      const before = state.reads(actorB);
      const response = page.waitForResponse((value) =>
        value.url().endsWith("/runs/b-2/publish"),
      );
      action.release();
      await (await response).finished();
      await expect.poll(() => state.settled("action")).toBeGreaterThan(0);
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      );
      await expect(page.locator("main")).not.toContainText(
        "Pull request publication completed.",
      );
      await expect(page.locator("main")).not.toContainText(
        "Prior-actor public error marker",
      );
      await expect(publishB).toBeDisabled();
      fresh.release();
      await expect.poll(() => state.settled("new-detail")).toBeGreaterThan(0);
      await expect(publishB).toBeEnabled();
      expect(state.reads(actorB)).toBe(before);
      expect(state.writes()).toBe(1);
    } finally {
      state.releaseAll();
    }
  });
