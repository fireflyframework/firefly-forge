/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
test("queued publication is not reported complete, cannot be duplicated, and follows its recorded outcome", async ({
  page,
}) => {
  const run = {
    id: "a".repeat(32),
    repository_id: "b".repeat(32),
    brief: "Reservation API",
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "succeeded",
    verification: [],
    changed_files: ["src/reservations.py"],
    pr_url: null as string | null,
    publication_request: undefined as
      { id: string; status: string } | undefined,
  };
  let writes = 0,
    completed = false;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/publish")) {
      writes++;
      run.publication_request = { id: "c".repeat(32), status: "queued" };
    }
    if (completed) {
      run.pr_url = "https://github.com/example/inventory/pull/7";
      run.publication_request = { id: "c".repeat(32), status: "completed" };
    }
    await route.fulfill({
      json: path.endsWith("/repositories")
        ? [
            {
              id: run.repository_id,
              name: "Inventory",
              provider: "github",
              url: "https://github.com/example/inventory",
              default_branch: "main",
            },
          ]
        : path.endsWith("/runs")
          ? [run]
          : path.endsWith(`/runs/${run.id}`) || path.endsWith("/publish")
            ? run
            : path.endsWith("/health")
              ? { status: "ok", execution_ready: false }
              : [],
    });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await page.getByRole("button", { name: /Reservation API/ }).click();
  await page
    .getByRole("button", { name: "Publish pull request", exact: true })
    .click();
  await expect(
    page.getByText("Pull request publication completed.", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Publication queued", exact: true }),
  ).toBeDisabled();
  expect(writes).toBe(1);
  completed = true;
  await expect(
    page.getByRole("link", { name: "Open pull request", exact: true }),
  ).toHaveAttribute("href", "https://github.com/example/inventory/pull/7");
  expect(writes).toBe(1);
});

test("a cancellation request waits for worker acknowledgement without offering a duplicate or premature retry", async ({
  page,
}) => {
  const run = {
    id: "a".repeat(32),
    repository_id: "b".repeat(32),
    brief: "Reservation cancellation",
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "running",
    cancel_request: undefined as { id: string; status: string } | undefined,
  };
  let writes = 0,
    acknowledged = false;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/cancel")) {
      writes++;
      run.cancel_request = { id: "c".repeat(32), status: "requested" };
    }
    if (acknowledged) {
      run.status = "cancelled";
      run.cancel_request = { id: "c".repeat(32), status: "acknowledged" };
    }
    await route.fulfill({
      json: path.endsWith("/repositories")
        ? [
            {
              id: run.repository_id,
              name: "Inventory",
              provider: "git",
              url: "/owned/source",
              default_branch: "main",
            },
          ]
        : path.endsWith("/runs")
          ? [run]
          : path.endsWith(`/runs/${run.id}`) || path.endsWith("/cancel")
            ? run
            : path.endsWith("/health")
              ? { status: "ok", execution_ready: true }
              : [],
    });
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
  await page.getByRole("button", { name: /Reservation cancellation/ }).click();
  await page.getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Cancellation requested", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Retry run", exact: true }),
  ).toHaveCount(0);
  acknowledged = true;
  await expect(
    page.getByRole("button", { name: "Retry run", exact: true }),
  ).toBeVisible();
  expect(writes).toBe(1);
});
