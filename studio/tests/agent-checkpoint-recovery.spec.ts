/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// PREPARED ONLY. Controlled API fixtures exercise the real UI, not native recovery.
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: "a".repeat(32),
  brief: "Owned original result fixture",
  status: "needs_reconciliation",
  agent: "bridge",
  publish: false,
  bridge_completion_hold: { reason: "graph_checkpoint_unavailable" },
};
const candidate = {
  completion_id: "fbr1_" + "4".repeat(32),
  checkpoint_digest: "b".repeat(64),
  intent_digest: "c".repeat(64),
  task: "implement",
  attempt: 1,
  ordinal: 2,
  deadline_ms: 1893456000000,
  created_at: "2026-10-17T00:00:00Z",
  state: "ready",
  result: {
    kind: "http_response",
    record_digest: "d".repeat(64),
    observation_id: null,
    terminal_digest: null,
  },
  observation: null,
  ticket: null,
};
async function setup(
  page: Page,
  kind: "restore" | "observation" = "restore",
  outcome: "lost" | "denied" | "valid" = "lost",
) {
  const state = {
    posts: 0,
    reads: 0,
    request: "",
    input: null as Record<string, unknown> | null,
    readStatus: 404,
    candidate: structuredClone(candidate),
    receiptStatus: "completed",
  };
  if (kind === "observation")
    Object.assign(state.candidate, { state: "awaiting_result", result: null });
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/agent-checkpoints/candidates"))
      return route.fulfill({
        json: {
          version: 1,
          actor_id: "f".repeat(32),
          run_id: run.id,
          repository_id: run.repository_id,
          project_id: run.project_id,
          state: "available",
          items: [state.candidate],
        },
      });
    if (
      (path.endsWith("/restores") || path.endsWith("/observations")) &&
      req.method() === "POST"
    ) {
      state.posts++;
      state.input = req.postDataJSON();
      state.request = String(
        state.input![kind === "restore" ? "request_id" : "observation_id"],
      );
      const markers = await page.evaluate(() =>
        Object.entries(sessionStorage).filter(([key]) =>
          key.startsWith("forge.agent-checkpoint.v1:"),
        ),
      );
      expect(markers).toHaveLength(1);
      expect(markers[0][1]).toContain(state.request);
      expect(markers[0][1]).not.toContain("fictional-ui-token");
      if (outcome === "denied")
        return route.fulfill({
          status: 403,
          json: { detail: "private denial canary" },
        });
      if (outcome === "lost") return route.abort("failed");
    }
    if (
      path.includes("/restores/") ||
      path.includes("/observations/") ||
      ((path.endsWith("/restores") || path.endsWith("/observations")) &&
        req.method() === "POST")
    ) {
      if (req.method() === "GET") {
        state.reads++;
        expect(
          path.endsWith(state.request) ||
            path.endsWith(
              String((state.candidate.ticket as { id: string } | null)?.id),
            ),
        ).toBe(true);
        if (state.readStatus !== 200)
          return route.fulfill({
            status: state.readStatus,
            json: { detail: "private exact receipt canary" },
          });
      }
      if (kind === "restore")
        return route.fulfill({
          json: {
            id: path.includes("/restores/")
              ? path.split("/").at(-1)
              : state.request,
            run_id: run.id,
            kind: "agent_restore",
            status: state.receiptStatus,
            request: {
              run_id: run.id,
              completion_request_id: candidate.completion_id,
              actor: { user_id: "f".repeat(32) },
              input: path.includes("/restores/")
                ? { ...state.input, request_id: path.split("/").at(-1) }
                : state.input,
            },
          },
        });
      return route.fulfill({
        json: {
          run_id: run.id,
          kind: "bridge_request_observation",
          task_id: "implement",
          idempotency_key: state.request,
          digest: "e".repeat(64),
          payload: {
            request_id: candidate.completion_id,
            intent_digest: candidate.intent_digest,
            observed_by: "f".repeat(32),
            input: state.input,
            snapshot: { state: "pending" },
          },
        },
      });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: "Owned original result fixture",
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
  await page
    .getByRole("button", { name: /Owned original result fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
}
async function continueOriginal(page: Page) {
  await page
    .getByRole("button", { name: "Review original result", exact: true })
    .click();
  await page
    .getByLabel(
      "I understand this result is consumed once and a later failure may remain held.",
    )
    .check();
  await page
    .getByRole("button", { name: "Continue from original result", exact: true })
    .click();
}
for (const width of [1440, 390])
  test(`one-use original result review is readable and keyboard reachable at ${width}px`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page, "restore", "valid");
    const panel = page.getByRole("region", {
      name: "Original model result",
      exact: true,
    });
    await expect(
      panel.getByLabel("Original implementation attempt"),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Review original result", exact: true })
      .focus();
    await page.keyboard.press("Enter");
    await expect(
      panel.getByRole("heading", { name: "Review continuation", exact: true }),
    ).toBeFocused();
    const action = page.getByRole("button", {
      name: "Continue from original result",
      exact: true,
    });
    await expect(action).toBeDisabled();
    await panel.getByRole("checkbox").focus();
    await page.keyboard.press("Space");
    await expect(action).toBeEnabled();
    expect(state.posts).toBe(0);
    expect(
      await panel.evaluate((node) => node.scrollWidth > node.clientWidth + 1),
    ).toBe(false);
    await panel.screenshot({
      path: info.outputPath(`original-result-${width}.png`),
    });
  });
for (const kind of ["restore", "observation"] as const)
  test(`lost ${kind} response and exact404 keep one original identity across reload`, async ({
    page,
  }) => {
    const state = await setup(page, kind);
    if (kind === "restore") await continueOriginal(page);
    else
      await page
        .getByRole("button", { name: "Check original result", exact: true })
        .click();
    await expect(
      page.getByText(/Existing request identities stay held/),
    ).toBeVisible();
    const original = state.request;
    expect(state.posts).toBe(1);
    await page.reload();
    await open(page);
    await page
      .getByRole("button", { name: "Read exact saved request", exact: true })
      .click();
    await expect(
      page.getByText(/This does not prove the request was not accepted/),
    ).toBeVisible();
    expect(state.posts).toBe(1);
    expect(state.request).toBe(original);
    expect(
      await page
        .getByRole("button", {
          name: "Continue from original result",
          exact: true,
        })
        .count(),
    ).toBe(0);
    state.readStatus = 200;
    await page
      .getByRole("button", { name: "Read exact saved request", exact: true })
      .click();
    await expect(
      page.getByText(
        kind === "restore"
          ? /This confirms the original implementation receipt/
          : /Model result pending/,
      ),
    ).toBeVisible();
    expect(state.posts).toBe(1);
  });
test("failed marker persistence sends no observation or restore POST", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.agent-checkpoint.v1:"))
        throw new Error("Owned storage refusal");
      original.call(this, key, value);
    };
  });
  const state = await setup(page);
  await continueOriginal(page);
  await expect(
    page.getByText(/Request storage or access could not be verified/),
  ).toBeVisible();
  expect(state.posts).toBe(0);
});
test("late403 clears private review and keeps exact reconciliation without exposing server text", async ({
  page,
}) => {
  const state = await setup(page, "restore", "denied");
  await continueOriginal(page);
  await expect(
    page.getByText(/Private result details are cleared/),
  ).toBeVisible();
  await expect(page.getByLabel("Original implementation attempt")).toHaveCount(
    0,
  );
  await expect(page.getByText("private denial canary")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Read exact saved request", exact: true }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
test("Bridge choices preserve unset values and require recovery before checkpoints without a connection probe", async ({
  page,
}) => {
  const saved: Record<string, unknown>[] = [];
  let probes = 0;
  const provider = {
    id: "bridge",
    name: "Bridge",
    ready: true,
    detail: "Owned configuration fixture",
    config: { model: "bridge/default" } as Record<string, string>,
    fields: [
      {
        key: "model",
        label: "Model",
        secret: false,
        required: false,
        placeholder: "",
      },
      {
        key: "completion_recovery",
        label: "Completion Recovery",
        secret: false,
        required: false,
        placeholder: "",
      },
      {
        key: "checkpoints",
        label: "Checkpoints",
        secret: false,
        required: false,
        placeholder: "",
      },
    ],
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/providers/bridge/configuration")) {
      const body = route.request().postDataJSON();
      saved.push(body.values);
      Object.assign(provider.config, body.values);
      return route.fulfill({ json: provider });
    }
    if (path.endsWith("/providers/bridge/test")) probes++;
    return route.fulfill({
      json: path.endsWith("/providers")
        ? [provider]
        : path.endsWith("/health")
          ? { status: "ok", execution_ready: false }
          : [],
    });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fictional-ui-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(
    page.getByLabel("Original model result recovery", { exact: true }),
  ).toHaveValue("");
  await page.getByLabel("Model", { exact: true }).fill("bridge/other");
  await page
    .getByRole("button", { name: "Save Bridge settings", exact: true })
    .click();
  expect(saved).toEqual([{ model: "bridge/other" }]);
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await page
    .getByLabel("Implementation checkpoints", { exact: true })
    .selectOption("required");
  await page
    .getByRole("button", { name: "Save Bridge settings", exact: true })
    .click();
  expect(saved).toHaveLength(1);
  await expect(
    page.getByText(
      "Required implementation checkpoints need required original result recovery.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByLabel("Original model result recovery", { exact: true })
    .selectOption("required");
  await page
    .getByRole("button", { name: "Save Bridge settings", exact: true })
    .click();
  expect(saved[1]).toEqual({
    model: "bridge/other",
    completion_recovery: "required",
    checkpoints: "required",
  });
  expect(probes).toBe(0);
});

test("malformed refreshed evidence closes an acknowledged review and offers no stale action", async ({
  page,
}) => {
  const state = await setup(page, "restore", "valid");
  await page
    .getByRole("button", { name: "Review original result", exact: true })
    .click();
  await page
    .getByLabel(
      "I understand this result is consumed once and a later failure may remain held.",
    )
    .check();
  Object.assign(state.candidate, { response: "private response canary" });
  await page
    .getByRole("button", { name: "Refresh saved result", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Continue from original result",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(page.getByText("private response canary")).toHaveCount(0);
  expect(state.posts).toBe(0);
});

test("a different server-discovered ticket remains readable while the local uncertain identity stays held", async ({
  page,
}) => {
  const state = await setup(page);
  await continueOriginal(page);
  await expect(
    page.getByText(/Existing request identities stay held/),
  ).toBeVisible();
  const original = state.request;
  Object.assign(state.candidate, {
    state: "ticket_recorded",
    ticket: { id: "9".repeat(32), status: "queued" },
  });
  await page
    .getByRole("button", { name: "Refresh saved result", exact: true })
    .click();
  state.readStatus = 200;
  await page
    .getByRole("button", { name: "Read recorded continuation", exact: true })
    .click();
  await expect(
    page.getByText(/This confirms the original implementation receipt/),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  const saved = await page.evaluate(
    () =>
      Object.entries(sessionStorage).find(([key]) =>
        key.startsWith("forge.agent-checkpoint.v1:"),
      )?.[1],
  );
  expect(saved).toContain(original);
  await expect(
    page.getByRole("button", {
      name: "Continue from original result",
      exact: true,
    }),
  ).toHaveCount(0);
});
