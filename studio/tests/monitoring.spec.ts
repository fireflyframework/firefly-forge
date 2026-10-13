/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";

test("operator saves monitoring settings, records checks and queues verified incident maintenance", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 900 });
  let app: Record<string, unknown> | null = null;
  let configured: Record<string, unknown> | undefined;
  let maintenance: Record<string, unknown> | undefined;
  const observed = {
    id: "check-1",
    application_id: "stockroom",
    status: "degraded",
    latency_ms: 140,
    metrics: { queue_depth: 9 },
    reasons: ["queue_depth exceeds the configured limit"],
    created_at: "2026-10-12T17:00:00Z",
  };
  const incident = {
    id: "incident-1",
    application_id: "stockroom",
    repository_id: "repo-1",
    target_id: "local",
    status: "open",
    summary: "Queue depth exceeded",
    created_at: observed.created_at,
    updated_at: observed.created_at,
    latest_observation_id: observed.id,
    maintenance_run_ids: [],
  };
  const repair = {
    id: "repair-1",
    repository_id: "repo-1",
    brief: "Reduce queue growth and verify behavior",
    status: "queued",
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/repositories"))
      await route.fulfill({
        json: [
          {
            id: "repo-1",
            name: "Stockroom",
            url: "/owned/source",
            provider: "git",
            default_branch: "main",
          },
        ],
      });
    else if (path.endsWith("/providers"))
      await route.fulfill({
        json: [{ id: "codex", name: "Codex", ready: true }],
      });
    else if (path.endsWith("/deployment-targets"))
      await route.fulfill({
        json: [{ id: "local", name: "Local target", repository_id: "repo-1" }],
      });
    else if (path.endsWith("/applications/stockroom/check")) {
      app = {
        ...app,
        latest: observed,
        stale: false,
        incidents: [incident],
        observations: [observed],
      };
      await route.fulfill({ json: observed });
    } else if (path.endsWith("/applications/stockroom")) {
      if (route.request().method() === "PUT") {
        configured = route.request().postDataJSON();
        app = {
          ...configured,
          id: "stockroom",
          latest: null,
          stale: true,
          incidents: [],
          observations: [],
        };
      }
      await route.fulfill({ json: app });
    } else if (path.endsWith("/applications"))
      await route.fulfill({ json: app ? [app] : [] });
    else if (path.endsWith("/incidents/incident-1/maintenance")) {
      maintenance = route.request().postDataJSON();
      await route.fulfill({ json: repair });
    } else if (path.endsWith("/runs/repair-1"))
      await route.fulfill({ json: repair });
    else if (path.endsWith("/runs/repair-1/plan"))
      await route.fulfill({ json: { tasks: [] } });
    else if (path.endsWith("/runs")) await route.fulfill({ json: [] });
    else await route.fulfill({ json: {} });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("operator-token-at-least-24-chars");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Applications");
  await page
    .getByRole("button", { name: "Monitor an application", exact: true })
    .click();
  await page.getByLabel("Application name", { exact: true }).fill("Stockroom");
  await page.getByLabel("Application ID", { exact: true }).fill("stockroom");
  await page
    .getByLabel("Application project", { exact: true })
    .selectOption("repo-1");
  await page
    .getByLabel("Application target", { exact: true })
    .selectOption("local");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Health endpoint", { exact: true })
    .fill("http://127.0.0.1:8080/health");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Metrics endpoint", { exact: true })
    .fill("http://127.0.0.1:8080/metrics");
  await page
    .getByRole("button", { name: "Add metric limit", exact: true })
    .click();
  await page.getByLabel("Metric name", { exact: true }).fill("queue_depth");
  await page.getByLabel("Limit", { exact: true }).fill("5");
  await page
    .getByRole("button", { name: "Review application", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save application", exact: true })
    .click();
  await expect(
    page.getByText("No recorded check", { exact: true }),
  ).toBeVisible();
  expect(configured).toMatchObject({
    repository_id: "repo-1",
    target_id: "local",
    health_url: "http://127.0.0.1:8080/health",
    metric_thresholds: [{ name: "queue_depth", operator: "gt", value: 5 }],
  });
  await page.getByRole("button", { name: "Check now", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Queue depth exceeded", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Resolve incident", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Start maintenance", exact: true })
    .click();
  await page
    .getByLabel("Maintenance instructions", { exact: true })
    .fill("Reduce queue growth and verify behavior");
  await page
    .getByLabel("Maintenance checks", { exact: true })
    .fill('[["make","check"]]');
  await page
    .getByRole("button", { name: "Queue maintenance run", exact: true })
    .click();
  expect(maintenance).toMatchObject({
    agent: "codex",
    brief: "Reduce queue growth and verify behavior",
    commands: [{ argv: ["make", "check"], timeout: 120 }],
  });
});
