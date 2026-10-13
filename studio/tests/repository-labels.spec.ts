/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

test("project selectors distinguish duplicate names across factory and knowledge settings", async ({
  page,
}) => {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
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
  await expect(
    page.getByLabel("Project", { exact: true }).locator("option"),
  ).toHaveText(expected);
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
