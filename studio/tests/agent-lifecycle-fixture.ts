/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
export const role = {
  id: "implementer",
  name: "Implementation worker",
  enabled: true,
  execution_mode: "coding_provider",
  provider_binding: "run",
  stages: ["implement"],
  responsibilities: ["Implement customer behavior"],
  skills: ["implementation"],
  allowed_tools: ["read_file"],
  inputs: ["Frozen contract"],
  outputs: ["Source and tests"],
  instructions: "Preserve reviewed behavior.",
  feedback: "Retain evidence.",
  escalation: "Ask the owner.",
};
export const catalog = {
  scope: { repository_id: null },
  registry_revision: "builtin-1",
  effective_digest: "snapshot",
  permissions: { edit_registry: true, edit_project: true },
  document: { version: "1.0.0", max_requests: 3, roles: [role] },
  effective_roles: [role],
  overrides: { enabled: [], disabled: [], skills: {}, allowed_tools: {} },
  knowledge: {
    status: "configured_unverified",
    framework_scope: "configured",
    verification: "Invocation citations establish retrieval.",
  },
  tools: [
    { id: "read_file", availability: "available", reason: "Confined source" },
  ],
  skills: [
    { id: "implementation", name: "Implementation" },
    {
      id: "humanizer",
      name: "Humanizer",
      provenance: {
        license: "MIT",
        source_url: "https://github.com/blader/humanizer",
        revision: "a".repeat(40),
      },
    },
  ],
  phases: [
    {
      id: "product_definition",
      name: "Define the product",
      description: "Capture outcomes.",
      handoff: "Owner-reviewed proposal",
      participants: [
        {
          id: "assistant-product",
          name: "Product assistant",
          execution_kind: "configured_model",
          enabled: true,
          optional: true,
          responsibilities: ["Propose for owner review"],
          inputs: ["Owner request"],
          outputs: ["Dialogue receipt"],
          skills: [],
          tools: [],
          knowledge_scope: "project_and_configured_framework",
          selection: { status: "invocation_selected" },
        },
      ],
    },
    {
      id: "implement",
      name: "Implement",
      description: "One coding provider changes source.",
      handoff: "Source for checks",
      participants: [
        {
          ...role,
          role_id: role.id,
          execution_kind: "run_provider",
          optional: false,
          skills: [
            {
              id: "implementation",
              name: "Implementation",
              application: "injected_instruction",
            },
          ],
          tools: [],
          knowledge_scope: "project_and_configured_framework",
          selection: { status: "invocation_selected" },
        },
      ],
    },
    {
      id: "observe",
      name: "Monitor",
      description: "Record actual probes.",
      handoff: "Observation and incident receipts",
      participants: [],
    },
  ],
  limits: "Current configuration; existing runs keep frozen snapshots.",
};

export async function openRoleEditor(page: Page, theme?: string) {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/agent-roles/lifecycle"))
      return route.fulfill({ json: catalog });
    if (path.endsWith("/repositories")) return route.fulfill({ json: [] });
    if (path.endsWith("/policies"))
      return route.fulfill({ json: { revision: "builtin", skills: [] } });
    return route.fulfill({ json: path.endsWith("/runs") ? [] : {} });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("operator-token-at-least-24-chars");
  await page.getByRole("button", { name: "Save connection" }).click();
  await navigate(page, "Agents & skills");
  if (theme)
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await page.getByRole("button", { name: "Implement", exact: true }).click();
  await page
    .getByRole("button", { name: "Edit Implementation worker" })
    .focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Role instructions")).toBeVisible();
}
