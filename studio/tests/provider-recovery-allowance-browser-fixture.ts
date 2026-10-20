/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import type { ForgeRun } from "../src/api";
import {
  candidate,
  context,
  receipt,
} from "./provider-recovery-allowance-fixture";
export const prefix = "forge.provider-recovery-allowance.v1:";
const run = {
  id: context.run,
  repository_id: context.repository,
  project_id: null,
  brief: "Controlled allowance UI fixture (no live CLI)",
  status: "failed",
  active_task: "implement",
  attempt: 4,
  agent: "codex",
  publish: false,
};
export async function setup(
  page: Page,
  mode = "success",
  existingSession = false,
) {
  const state = {
    posts: 0,
    reads: 0,
    candidateReads: 0,
    candidateStatus: 200,
    candidate: structuredClone(candidate),
    readStatus: mode === "lost" ? 404 : 200,
    body: null as Record<string, unknown> | null,
    releasePost: () => {},
    run: structuredClone(run) as ForgeRun,
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("/provider-recoveries/allowances/candidate")) {
      state.candidateReads++;
      expect(
        new URL(request.url()).searchParams.get("adopt_current_executable"),
      ).toBe("true");
      return route.fulfill({
        status: state.candidateStatus,
        json:
          state.candidateStatus === 200
            ? state.candidate
            : { detail: "private candidate canary" },
      });
    }
    if (
      path.endsWith("/provider-recoveries/allowances") &&
      request.method() === "POST"
    ) {
      state.posts++;
      state.body = request.postDataJSON();
      const saved = await page.evaluate(
        (key) =>
          Object.entries(localStorage).filter(([name]) => name.startsWith(key)),
        prefix,
      );
      expect(saved).toHaveLength(1);
      expect(JSON.parse(saved[0][1]).body).toEqual(state.body);
      if (mode === "pending") {
        await new Promise<void>((resolve) => {
          state.releasePost = resolve;
        });
        return route.abort("failed");
      }
      if (mode === "lost") return route.abort("failed");
      if (mode === "denied")
        return route.fulfill({
          status: 403,
          json: { detail: "private denial canary" },
        });
      return route.fulfill({
        status: 201,
        json: { not_a_receipt: "An exact GET must still confirm." },
      });
    }
    if (path.includes("/provider-recoveries/allowances/")) {
      state.reads++;
      if (state.readStatus !== 200 || !state.body)
        return route.fulfill({
          status: state.readStatus,
          json: { detail: "private receipt canary" },
        });
      const value = await receipt(state.body);
      return route.fulfill({
        json:
          mode === "wrong-actor"
            ? { ...value, actor: { ...value.actor, user_id: "0".repeat(32) } }
            : value,
      });
    }
    const body = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: "Owned fixture repository",
            url: "/fixture",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [state.run]
        : path.endsWith("/runs/" + run.id)
          ? state.run
          : path.endsWith("/plan")
            ? { revision: 1, tasks: [] }
            : path.endsWith("/journal")
              ? { valid: true, entries: [] }
              : path.endsWith("/health")
                ? { status: "ok", execution_ready: false }
                : [];
    return route.fulfill({ json: body });
  });
  await page.goto("/");
  if (!existingSession) {
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("owned-fixture-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
  }
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await openPlan(page);
  return state;
}
export async function openPlan(page: Page) {
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Controlled allowance UI fixture/ })
    .click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
}
export async function open(page: Page) {
  await page
    .getByRole("button", { name: "Review final allowance", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review the failed attempt",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Current CLI selected for review", { exact: true }),
  ).toBeVisible();
}
export async function mitigation(page: Page) {
  await page
    .getByRole("button", { name: "Continue to mitigation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Record the mitigation", exact: true }),
  ).toBeFocused();
  await page
    .getByLabel("Mitigation reviewed", { exact: true })
    .fill(
      "Reviewed the finite signal mitigation in controlled local fixtures.",
    );
  await page
    .getByLabel("Mitigation evidence SHA-256", { exact: true })
    .fill("e".repeat(64));
  await expect(
    page.getByRole("button", { name: "Review reservation", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("Use the reviewed current CLI for this attempt.", {
      exact: true,
    })
    .check();
}
export async function review(page: Page) {
  await open(page);
  await mitigation(page);
  await page
    .getByRole("button", { name: "Review reservation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Confirm the final reservation",
      exact: true,
    }),
  ).toBeFocused();
  await page
    .getByLabel(
      "I authorize this one final reservation and understand it cannot be renewed.",
      { exact: true },
    )
    .check();
}
