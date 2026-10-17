/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Controlled API evidence for browser presentation; no native recovery or model runs.
import { expect, type Page } from "@playwright/test";
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
export async function setup(
  page: Page,
  kind: "restore" | "observation" = "restore",
  outcome: "lost" | "denied" | "valid" = "lost",
  theme?: "light" | "dark",
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
  if (theme) {
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  }
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
export async function open(page: Page) {
  await page
    .getByRole("button", { name: /Owned original result fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
}
export async function continueOriginal(page: Page) {
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
