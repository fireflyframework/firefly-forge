/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { factoryDashboardFixture } from "./factory-dashboard-fixture";

test("repository selectors distinguish duplicate names while Factory lists explicit product projects", async ({
  page,
}) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const url = new URL(route.request().url()),
      path = url.pathname;
    const dashboard = factoryDashboardFixture(url, {
      projects: [
        {
          id: "c".repeat(32),
          name: "Inventory product",
          component_count: 3,
          repository_count: 2,
        },
        {
          id: "d".repeat(32),
          name: "Inventory audit",
          component_count: 1,
          repository_count: 1,
        },
      ],
      runs: [],
    });
    if (dashboard !== undefined) return route.fulfill({ json: dashboard });
    await route.fulfill({
      json: path.endsWith("/repositories")
        ? [
            {
              id: "a".repeat(32),
              name: "Inventory",
              url: "https://github.com/example/inventory",
              provider: "github",
              default_branch: "main",
            },
            {
              id: "b".repeat(32),
              name: "Inventory",
              url: "https://github.com/example/inventory",
              provider: "github",
              default_branch: "main",
            },
          ]
        : path.endsWith("/knowledge/configuration")
          ? {
              enabled: true,
              top_k: 5,
              readiness: { state: "disabled", detail: "Not configured" },
            }
          : path.endsWith("/knowledge/explore")
            ? {
                sources: [],
                knowledge: [],
                warnings: [],
                totals: { sources: 0, knowledge: 0 },
                browsing_available: true,
              }
            : path.endsWith("/product") || path.endsWith("/brand")
              ? null
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
    page.getByRole("heading", {
      name: "Choose your coding provider",
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Grant repository access", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Continue to knowledge", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Add project knowledge", exact: true }),
  ).toBeVisible();
  const expected = [
    "Inventory · github.com/example/inventory · github · main · aaaaaaaa",
    "Inventory · github.com/example/inventory · github · main · bbbbbbbb",
  ];
  await expect(
    page
      .getByLabel("Search project", { exact: true })
      .locator("option")
      .filter({ hasText: "Inventory" }),
  ).toHaveText(expected);
  await navigate(page, "Factory");
  await page.getByRole("button", { name: "Browse work", exact: true }).click();
  const drawer = page.getByRole("dialog", {
    name: "Factory work",
    exact: true,
  });
  await expect(drawer.locator(".factory-project-row")).toHaveText([
    "Inventory product3 components · 2 repositories",
    "Inventory audit1 component · 1 repository",
  ]);
  await drawer.getByRole("button", { name: /^Inventory audit / }).click();
  await expect(page.locator(".factory-scope-caption h2")).toHaveText(
    "Inventory audit",
  );
  await navigate(page, "Knowledge");
  await expect(
    page
      .getByLabel("Knowledge project", { exact: true })
      .locator("option")
      .filter({ hasText: "Inventory" }),
  ).toHaveText(expected);
  await navigate(page, "Product definition");
  await page.setViewportSize({ width: 320, height: 568 });
  await expect(
    page.getByLabel("Selected project source", { exact: true }),
  ).toContainText("aaaaaaaa");
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption("b".repeat(32));
  await expect(
    page.getByLabel("Selected project source", { exact: true }),
  ).toContainText("bbbbbbbb");
});
