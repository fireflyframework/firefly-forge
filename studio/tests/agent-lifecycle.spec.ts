/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { role, catalog } from "./agent-lifecycle-fixture";

test("operator explores lifecycle, retains a draft across phases and saves project defaults", async ({
  page,
}) => {
  let saved: unknown;
  let rejectWrite = true;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/agent-roles/lifecycle"))
      await route.fulfill({ json: catalog });
    else if (
      path.endsWith("/agent-roles") &&
      route.request().method() === "PUT"
    ) {
      saved = route.request().postDataJSON();
      if (rejectWrite) {
        rejectWrite = false;
        await route.fulfill({
          status: 403,
          json: { detail: "Project authority changed; review your draft." },
        });
        return;
      }
      await route.fulfill({ json: { roles: saved } });
    } else if (path.endsWith("/repositories"))
      await route.fulfill({
        json: [
          {
            id: "repo-1",
            name: "Inventory",
            url: "/source",
            provider: "git",
            default_branch: "main",
          },
        ],
      });
    else if (path.endsWith("/policies"))
      await route.fulfill({
        json: { revision: "builtin", source: { kind: "builtin" }, skills: [] },
      });
    else await route.fulfill({ json: path.endsWith("/runs") ? [] : {} });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("operator-token-at-least-24-chars");
  await page.getByRole("button", { name: "Save connection" }).click();
  await navigate(page, "Agents & skills");
  await expect(
    page.getByRole("heading", { name: "Product assistant" }),
  ).toBeVisible();
  await expect(
    page.getByText("Configured, retrieval unverified", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Lifecycle project").selectOption("repo-1");
  await page.getByRole("button", { name: "Implement", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Implementation worker" })
    .click();
  await page.getByLabel("Humanizer", { exact: true }).check();
  await page.getByRole("button", { name: "Monitor", exact: true }).click();
  await page.getByRole("button", { name: "Implement", exact: true }).click();
  await expect(page.getByLabel("Humanizer", { exact: true })).toBeChecked();
  await page.getByRole("button", { name: "Save project role" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Project authority changed",
  );
  await expect(page.getByLabel("Humanizer", { exact: true })).toBeChecked();
  const projectSaved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        "/api/v1/repositories/repo-1/agent-roles" &&
      response.request().method() === "PUT" &&
      response.status() === 200,
  );
  await page.getByRole("button", { name: "Save project role" }).click();
  await projectSaved;
  expect(saved).toEqual({
    enabled: [],
    disabled: [],
    skills: { implementer: ["implementation", "humanizer"] },
    allowed_tools: { implementer: ["read_file"] },
  });
  await expect(
    page.getByRole("status").filter({ hasText: "Role settings saved" }),
  ).toBeVisible();
  await page.getByLabel("Lifecycle project").selectOption("");
  await page.getByRole("button", { name: "Implement", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Implementation worker" })
    .click();
  await page
    .getByLabel("Role instructions")
    .fill("Retain the customer reservation boundaries.");
  const factorySaved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/agent-roles" &&
      response.request().method() === "PUT" &&
      response.status() === 200,
  );
  await page.getByRole("button", { name: "Save factory role" }).click();
  await factorySaved;
  expect(saved).toEqual({
    version: "1.0.0",
    max_requests: 3,
    roles: [
      { ...role, instructions: "Retain the customer reservation boundaries." },
    ],
  });
  await page.setViewportSize({ width: 360, height: 720 });
  await expect(
    page.getByRole("button", { name: "Monitor", exact: true }),
  ).toBeVisible();
  expect(
    await page
      .locator(".agent-lifecycle")
      .evaluate((el) => el.scrollWidth > el.clientWidth),
  ).toBe(false);
});
