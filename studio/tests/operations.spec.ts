/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate, expectAuthenticated } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { expectProgressFits } from "./wizard-progress";

const repository = {
  id: "project-1",
  name: "Stockroom",
  url: "https://github.com/team/stockroom.git",
  provider: "github",
  default_branch: "main",
};
async function connect(page: import("@playwright/test").Page) {
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("operator-token-for-ui-verification");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expectAuthenticated(page);
}

test("application grant discovery and optional knowledge setup use real API contracts without storing credentials", async ({
  page,
}) => {
  let registered = false;
  let savedApp: unknown;
  let savedKnowledge: unknown;
  let indexedConnection: unknown;
  const scm = {
    id: "github",
    name: "GitHub",
    auth_type: "github_app",
    status: "configured",
    detail: "Install the application in your organization",
    setup_url: "https://github.com/settings/apps/new",
    permissions: ["contents", "pull_requests"],
    config: { app_id: "42", private_key_configured: true },
    fields: [
      {
        key: "app_id",
        label: "Application ID",
        secret: false,
        required: true,
        placeholder: "42",
      },
      {
        key: "private_key",
        label: "Private key",
        secret: true,
        required: true,
        placeholder: "PEM private key",
      },
    ],
  };
  const knowledge = {
    enabled: false,
    endpoint: "",
    tenant_id: "",
    framework_workspace: "",
    required: false,
    auto_memory: false,
    api_key_configured: false,
    agent_token_configured: false,
    top_k: 5,
    readiness: { state: "configured", detail: "Knowledge settings saved" },
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path.endsWith("/scm/connections/github/repositories/connect")) {
      indexedConnection = route.request().postDataJSON();
      registered = true;
      body = repository;
    } else if (path.endsWith("/scm/connections/github/repositories"))
      body = [{ ...repository, id: "remote-42" }];
    else if (path.endsWith("/scm/connections/github")) {
      savedApp = route.request().postDataJSON();
      body = scm;
    } else if (path.endsWith("/scm/connections")) body = [scm];
    else if (path.endsWith("/knowledge/configuration")) {
      if (route.request().method() === "PUT") {
        savedKnowledge = route.request().postDataJSON();
        knowledge.enabled = true;
        knowledge.api_key_configured = true;
      }
      body = knowledge;
    } else if (path.endsWith("/repositories"))
      body = registered ? [repository] : [];
    else if (path.endsWith("/providers"))
      body = [
        {
          id: "codex",
          name: "Codex",
          ready: true,
          detail: "Official CLI signed in",
        },
      ];
    else if (path.endsWith("/runs")) body = [];
    await route.fulfill({ json: body });
  });
  await connect(page);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Create application" }),
  ).toHaveAttribute("href", scm.setup_url);
  await page
    .getByLabel("Private key", { exact: true })
    .fill("private-pem-value");
  await page.getByRole("button", { name: "Save application settings" }).click();
  expect(savedApp).toEqual({
    values: { private_key: "private-pem-value" },
    clear_secrets: [],
  });
  await expect(page.getByLabel("Private key", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "Find granted repositories" }).click();
  await page.getByRole("button", { name: "Connect Stockroom" }).click();
  expect(indexedConnection).toEqual({ repository_id: "remote-42" });
  await page.getByRole("button", { name: "Continue to knowledge" }).click();
  await page.getByLabel("Enable knowledge retrieval").check();
  await page
    .getByLabel("flycanon address")
    .fill("https://knowledge.example.com");
  await page.getByLabel("Tenant", { exact: true }).fill("engineering");
  await page.getByLabel("Platform API key").fill("private-knowledge-key");
  await page.getByRole("button", { name: "Save knowledge settings" }).click();
  expect(savedKnowledge).toMatchObject({
    values: {
      enabled: true,
      endpoint: "https://knowledge.example.com",
      tenant_id: "engineering",
      api_key: "private-knowledge-key",
    },
  });
  await expect(page.getByLabel("Platform API key")).toHaveValue("");
  expect(
    await page.evaluate(() =>
      JSON.stringify({
        local: { ...localStorage },
        session: { ...sessionStorage },
      }),
    ),
  ).not.toContain("private-");
});

test("deployment wizard requires health checks and saves reviewed project-bound commands", async ({
  page,
}) => {
  let saved: unknown;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    let body: unknown = path.endsWith("/repositories")
      ? [repository]
      : path.endsWith("/providers") ||
          path.endsWith("/runs") ||
          path.endsWith("/deployment-targets")
        ? []
        : {};
    if (path.endsWith("/deployment-targets/staging")) {
      saved = route.request().postDataJSON();
      body = { id: "staging", ...(saved as object) };
    }
    await route.fulfill({ json: body });
  });
  await connect(page);
  await navigate(page, "Deployment");
  await page.getByLabel("Target name").fill("Staging");
  await page.getByLabel("Target ID").fill("staging");
  await page.getByLabel("Target project").selectOption(repository.id);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Deployment commands", { exact: true })
    .fill('[["make","deploy"]]');
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByLabel("Deployment health checks")).toBeVisible();
  await page.setViewportSize({ width: 320, height: 568 });
  await expectProgressFits(
    page.getByRole("list", { name: "Setup progress" }),
    "Health checks",
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByLabel("Deployment health checks").fill('[["make","health"]]');
  await page.getByRole("button", { name: "Review target" }).click();
  await expect(
    page.getByRole("heading", { name: "Review the deployment target" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save deployment target" }).click();
  expect(saved).toEqual({
    name: "Staging",
    repository_id: repository.id,
    commands: [{ argv: ["make", "deploy"], timeout: 120 }],
    verification: [{ argv: ["make", "health"], timeout: 120 }],
    rollback_commands: [],
    rollback_verification: [],
  });
});

test("paused production stops and explicit handoff retains the run identifier and plan evidence", async ({
  page,
}) => {
  const run = {
    id: "paused-1",
    repository_id: repository.id,
    brief: "Add stock alerts",
    status: "paused",
    agent: "codex",
    operation: "build",
    pause: { message: "Credits exhausted", kind: "quota", automatic: false },
  };
  let handoff: unknown;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    let body: unknown = run;
    if (path.endsWith("/repositories")) body = [repository];
    else if (path.endsWith("/providers") || path.endsWith("/deployments"))
      body = [];
    else if (path.endsWith("/runs")) body = [run];
    else if (path.endsWith("/plan"))
      body = {
        revision: 1,
        tasks: [
          {
            id: "prepare",
            title: "Prepare",
            status: "done",
            instructions: "Inspect source",
            attempt: 1,
          },
          {
            id: "implement",
            title: "Develop",
            status: "paused",
            instructions: "Implement alerts",
            attempt: 1,
            error: { message: "Credits exhausted" },
          },
        ],
      };
    else if (path.endsWith("/journal")) body = { valid: true, entries: [] };
    else if (path.endsWith("/handoff")) {
      handoff = route.request().postDataJSON();
      run.agent = "claude";
      run.status = "queued";
    }
    await route.fulfill({ json: body });
  });
  await connect(page);
  await page.getByRole("button", { name: "Factory", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Develop paused" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Inspect run" }).click();
  await page.getByRole("tab", { name: "Plan", exact: true }).click();
  await expect(
    page.getByText("Chain verified by server", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Continue with another provider")
    .selectOption("claude");
  await page.getByRole("button", { name: "Hand off run" }).click();
  expect(handoff).toEqual({ agent: "claude" });
  await expect(page.getByText("Run paused-1", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel run" })).toBeVisible();
});
