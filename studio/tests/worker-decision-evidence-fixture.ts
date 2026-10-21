/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
import raster from "./worker-evidence-raster-fixture.json" with { type: "json" };
import { mockIdentity } from "./auth-fixture";
import contract from "./worker-dialogue-contract-fixture.json" with { type: "json" };
// Controlled API responses and fictional raster bytes; no model or native backend runs.
export const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: "4".repeat(32),
  brief: "Controlled decision and screenshot fixture",
  status: "paused",
  publish: false,
  agent: "codex",
  worker_request: {
    id: "reservation-limit",
    kind: "product_definition",
    status: "pending",
  },
  pause: {
    kind: "worker_request",
    automatic: false,
    message: "A product decision is needed.",
  },
};
const png = Buffer.from(raster.content_base64, "base64");
export const image = raster.image;
export async function setup(page: Page, viewer = false) {
  const state = {
    images: 0,
    writes: 0,
    deny: false,
    runs: 0,
    saves: 0,
    lostSave: false,
    denyRun: false,
    refuseSave: false,
    route: {
      revision: 1,
      digest: "a".repeat(64),
      bindings: { implementation: "coding" } as Record<string, string>,
    },
  };
  let signedIn = false;
  await page.context().route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/login")) signedIn = true;
    if (viewer && signedIn && path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name: "Controlled project viewer",
            authentication_method: "oidc_bearer",
          },
          global_admin: false,
          memberships: [
            {
              project_id: run.project_id,
              role: "viewer",
              permissions: ["read"],
            },
          ],
          roles: { viewer: ["read"] },
        },
      });
    if (await mockIdentity(route)) return;
    if (path.endsWith("/task-routing")) {
      if (route.request().method() === "PUT") {
        state.saves++;
        if (state.refuseSave)
          return route.fulfill({
            status: 403,
            json: { detail: "Current administrator access is required." },
          });
        const body = route.request().postDataJSON();
        expect(body.expected_digest).toBe(state.route.digest);
        expect(body.bindings.implementation).toBe("coding");
        expect(Object.keys(body).sort()).toEqual([
          "bindings",
          "expected_digest",
        ]);
        state.route = {
          revision: state.route.revision + 1,
          digest: "b".repeat(64),
          bindings: body.bindings,
        };
        if (state.lostSave) return route.abort("failed");
      }
      return route.fulfill({ json: state.route });
    }
    if (path.endsWith("/model-profiles"))
      return route.fulfill({
        json: [
          {
            id: "updates",
            name: "Project updates",
            provider: "bridge",
            tasks: ["communication"],
          },
          {
            id: "coding",
            name: "Source builder",
            provider: "codex",
            tasks: ["implementation", "review"],
          },
        ],
      });
    if (path.endsWith("/providers"))
      return route.fulfill({
        json: [
          {
            id: "codex",
            name: "Codex fixture",
            ready: false,
            detail: "Controlled provider fixture; no login or model call.",
          },
        ],
      });
    if (state.denyRun && path.endsWith(`/runs/${run.id}`))
      return route.fulfill({
        status: 403,
        json: { detail: "Current project read access is required." },
      });
    if (route.request().method() !== "GET") {
      state.writes++;
      return route.fulfill({
        status: 403,
        json: { detail: "No execution is authorized by this visual fixture." },
      });
    }
    if (path.endsWith(`/worker-evidence/${image.id}`)) {
      state.images++;
      return state.deny
        ? route.fulfill({
            status: 403,
            json: { detail: "Current project access was removed." },
          })
        : route.fulfill({
            body: png,
            headers: {
              "Content-Type": image.mime_type,
              "X-Content-SHA256": image.sha256,
              "Cache-Control": "private, no-store",
            },
          });
    }
    const json = path.endsWith("/worker-evidence")
      ? [image]
      : path.endsWith("/worker-messages")
        ? [contract.feedback]
        : path.endsWith(`/runs/${run.id}`)
          ? (state.runs++, run)
          : path.endsWith("/runs")
            ? [run]
            : path.endsWith("/repositories")
              ? [
                  {
                    id: run.repository_id,
                    name: "Controlled design fixture",
                    url: "https://example.test/fixture.git",
                    provider: "git",
                    default_branch: "main",
                  },
                ]
              : path.endsWith("/health")
                ? { status: "ok", execution_ready: false }
                : [];
    return route.fulfill({ json });
  });
  await page.goto(`/?run=${run.id}&view=questions`);
  expect(state.runs).toBe(0);
  await page.evaluate(async () => {
    await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: "controlled-fixture-token" }),
    });
  });
  await page.reload();
  await expect(
    page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    }),
  ).toBeVisible();
  return state;
}
