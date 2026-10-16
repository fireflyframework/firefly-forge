/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
const repository = "a".repeat(32);
async function setup(page: Page, committed: boolean) {
  let definition: Record<string, unknown> = {
    customer: "Saved customer",
    problem: "Manage fictional stock",
    owners: ["Owner"],
    journey: ["Read stock"],
    user_stories: ["An operator reads stock"],
    acceptance: ["Stock is visible"],
    security_requirements: [],
    nonfunctional_requirements: [],
    architecture: null,
    design_review: null,
  };
  let revision = 1,
    writes = 0,
    dropNext = true;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: repository,
            name: "Fictional stock",
            url: "https://github.com/example/stock",
            provider: "github",
            default_branch: "main",
          },
        ],
      });
    const value = () => ({
      repository_id: repository,
      revision,
      digest: String(revision).repeat(64),
      created_at: "2026-10-16T00:00:00Z",
      definition,
    });
    if (path.endsWith("/product/conditional")) {
      writes++;
      const input = request.postDataJSON();
      if (
        input.expected_revision !== revision ||
        input.expected_digest !== String(revision).repeat(64)
      )
        return route.fulfill({
          status: 409,
          json: { detail: "The saved product changed" },
        });
      if (committed || !dropNext) {
        definition = input.definition;
        revision++;
      }
      if (dropNext) {
        dropNext = false;
        return route.abort("connectionreset");
      }
      return route.fulfill({ json: value() });
    }
    if (path.endsWith("/product")) return route.fulfill({ json: value() });
    return route.fulfill({ json: path.endsWith("/brand") ? null : [] });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product definition");
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Saved customer");
  await page
    .getByLabel("Customer or user", { exact: true })
    .fill("Submitted customer");
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save draft definition", exact: true })
    .click();
  await expect(
    page.getByText("The save outcome is uncertain.", { exact: false }),
  ).toBeVisible();
  return { writes: () => writes, revision: () => revision };
}

test("committed product save with a lost response is inspected without a second write after navigation", async ({
  page,
}) => {
  const state = await setup(page, true);
  await expect(
    page.getByRole("button", { name: "Save draft definition", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Discard edits", exact: true }),
  ).toBeDisabled();
  await navigate(page, "Connections");
  await navigate(page, "Product definition");
  await expect(
    page.getByRole("button", { name: "Save draft definition", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(1);
  await page
    .getByRole("button", { name: "Inspect saved definition", exact: true })
    .click();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(state.revision()).toBe(2);
});
test("unchanged read keeps the hold; only explicit identical conditional retry can save", async ({
  page,
}) => {
  const state = await setup(page, false);
  await page
    .getByRole("button", { name: "Inspect saved definition", exact: true })
    .click();
  await expect(
    page.getByText("The save outcome is still uncertain.", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save draft definition", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(1);
  await page
    .getByRole("button", {
      name: "Retry original conditional save",
      exact: true,
    })
    .click();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(2);
  expect(state.revision()).toBe(2);
});
