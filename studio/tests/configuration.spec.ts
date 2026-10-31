/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import { expect, test } from "@playwright/test";

test("operator configures an AI without reading back its key and selects versioned project policies", async ({
  page,
}) => {
  let saved: unknown;
  let selected: unknown;
  const provider = {
    id: "agentic",
    name: "Firefly Agentic",
    ready: true,
    detail: "Endpoint configured; execution has not been verified",
    auth_mode: "api_key",
    model: "openai:available-model",
    config: { model: "openai:available-model", api_key_configured: true },
    fields: [
      {
        key: "model",
        label: "Model",
        secret: false,
        required: true,
        placeholder: "Your available model",
      },
      {
        key: "api_key",
        label: "API key",
        secret: true,
        required: true,
        placeholder: "Provider key",
      },
    ],
    readiness: {
      state: "configured",
      detail: "Model access requires a real run",
      checked_at: null,
    },
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/configuration")) {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: provider });
    } else if (path.endsWith("/test")) await route.fulfill({ json: provider });
    else if (path.endsWith("/providers"))
      await route.fulfill({ json: [provider] });
    else if (path.endsWith("/repositories"))
      await route.fulfill({
        json: [
          {
            id: "repo-1",
            name: "Inventory",
            url: "/owned/source",
            provider: "git",
            default_branch: "main",
          },
        ],
      });
    else if (path.endsWith("/repositories/repo-1/policies")) {
      if (route.request().method() === "PUT")
        selected = route.request().postDataJSON();
      await route.fulfill({ json: { skills: ["verification"] } });
    } else if (path.endsWith("/policies"))
      await route.fulfill({
        json: {
          revision: "policy-commit",
          digest: "catalog-digest",
          source: { kind: "git" },
          skills: [
            {
              id: "verification",
              name: "Behavior checks",
              description: "Exercise valid and rejected reservations",
              kind: "recommendation",
              default: true,
            },
            {
              id: "accessibility",
              name: "Accessible UI",
              description: "Keyboard navigation and labelled inputs",
              kind: "recommendation",
              default: false,
            },
          ],
        },
      });
    else if (path.endsWith("/agent-roles/lifecycle"))
      await route.fulfill({
        json: {
          permissions: { edit_registry: true, edit_project: true },
          document: { version: "1", max_requests: 3, roles: [] },
          effective_roles: [],
          overrides: {
            enabled: [],
            disabled: [],
            skills: {},
            allowed_tools: {},
          },
          knowledge: {
            status: "not_configured",
            framework_scope: "not_configured",
            verification: "Retained citations prove retrieval.",
          },
          skills: [],
          tools: [],
          limits: "Existing runs retain frozen snapshots.",
          phases: [
            {
              id: "verify",
              name: "Verify",
              description: "Run actual commands",
              handoff: "Retained check outcomes",
              participants: [],
            },
          ],
        },
      });
    else if (path.endsWith("/workflow"))
      await route.fulfill({
        json: {
          stages: [
            {
              id: "validating",
              name: "Verify",
              description: "Run actual commands",
              roles: ["verifier"],
            },
          ],
          agents: [],
          tools: [],
        },
      });
    else await route.fulfill({ json: path.endsWith("/runs") ? [] : {} });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("operator-token-at-least-24-chars");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expectAuthenticated(page);
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  const formWidth = await page
    .locator(".provider-config-form")
    .evaluate((element) => ({
      visible: element.clientWidth,
      scroll: element.scrollWidth,
    }));
  expect(formWidth.scroll).toBeLessThanOrEqual(formWidth.visible);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
  await page.getByLabel("Model", { exact: true }).fill("openai:new-model");
  await page
    .getByLabel("API key", { exact: true })
    .fill("private-provider-key");
  await page
    .getByRole("button", { name: "Save Firefly Agentic settings" })
    .click();
  expect(saved).toEqual({
    values: { model: "openai:new-model", api_key: "private-provider-key" },
    clear_secrets: [],
  });
  expect(
    await page.evaluate(() =>
      JSON.stringify({
        local: { ...localStorage },
        session: { ...sessionStorage },
      }),
    ),
  ).not.toContain("private-provider-key");
  await page
    .getByRole("button", { name: "Check connection", exact: true })
    .click();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Model access requires a real run" }),
  ).toBeVisible();
  await navigate(page, "Agents & skills");
  await page.getByLabel("Project repository").selectOption("repo-1");
  await expect(page.getByLabel(/Behavior checks/)).toBeChecked();
  await page.getByLabel(/Accessible UI/).check();
  await page.getByRole("button", { name: "Save project policies" }).click();
  expect(selected).toEqual({ skills: ["verification", "accessibility"] });
  await expect(
    page.getByText("Run actual commands", { exact: true }),
  ).toBeVisible();
});
