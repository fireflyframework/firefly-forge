/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { test, expect, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
const repository = {
  id: "project-1",
  name: "Inventory",
  url: "https://github.com/example/inventory",
  provider: "github",
  default_branch: "main",
};
async function connect(page: Page) {
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
}
async function fixtures(
  page: Page,
  custom: (
    path: string,
    method: string,
    body: Record<string, unknown> | null,
  ) => unknown,
) {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    const result = custom(
      path,
      req.method(),
      req.postData() ? req.postDataJSON() : null,
    );
    if (result !== undefined) return route.fulfill({ json: result });
    if (path.endsWith("/repositories"))
      return route.fulfill({ json: [repository] });
    if (
      path.endsWith("/runs") ||
      path.endsWith("/providers") ||
      path.endsWith("/deployment-targets")
    )
      return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
}
test("knowledge upload validates files, records admission separately and reconciles without resubmission", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let uploads = 0,
    reconcile: unknown;
  let receipt = {
    id: "upload-1",
    repository_id: "project-1",
    filename: "inventory.md",
    title: "Inventory reference",
    content_sha256: "a".repeat(64),
    content_bytes: 25,
    workspace_id: "workspace-1",
    status: "needs_reconciliation",
    source_status: "",
    created_at: "2026-10-12T00:00:00Z",
    updated_at: "2026-10-12T00:00:00Z",
    history: [{ status: "needs_reconciliation", at: "2026-10-12T00:00:00Z" }],
  };
  await fixtures(page, (path, method, body) => {
    if (path.endsWith("/knowledge/configuration"))
      return {
        enabled: true,
        readiness: { state: "ready", detail: "Configured" },
      };
    if (path.endsWith("/knowledge/explore"))
      return {
        workspace_id: "workspace-1",
        scope: "project",
        sources: [],
        knowledge: [],
        totals: { sources: 0, knowledge: 0 },
        limit: 50,
        offset: 0,
        browsing_available: true,
        warnings: [],
      };
    if (path.endsWith("/knowledge/uploads")) {
      if (method === "POST") {
        uploads++;
        expect(body?.filename).toBe("inventory.md");
        expect(
          Buffer.from(String(body?.content_base64), "base64").toString(),
        ).toContain("inventory reference");
        return receipt;
      }
      return [];
    }
    if (path.endsWith("/reconcile")) {
      reconcile = body;
      receipt = { ...receipt, status: "submitted", source_status: "ingested" };
      return receipt;
    }
    if (path.endsWith("/uploads/upload-1")) return receipt;
  });
  await connect(page);
  await navigate(page, "Knowledge");
  await page
    .getByRole("button", { name: "Upload a file", exact: true })
    .click();
  const picker = page.getByLabel("Choose knowledge file", { exact: true });
  await picker.setInputFiles({
    name: "unsafe.exe",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("text"),
  });
  await expect(page.getByRole("alert")).toContainText("Choose a .txt");
  await picker.setInputFiles({
    name: "invalid.txt",
    mimeType: "text/plain",
    buffer: Buffer.from([0xff, 0xfe]),
  });
  await expect(page.getByRole("alert")).toContainText("valid UTF-8");
  await picker.setInputFiles({
    name: "big.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(1000001, 65),
  });
  await expect(page.getByRole("alert")).toContainText(
    "at most 1 MB (1,000,000 bytes)",
  );
  await picker.setInputFiles({
    name: "inventory.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("Actual inventory reference"),
  });
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Source title").fill("Inventory reference");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("I reviewed this file and confirmed it contains no secrets.")
    .check();
  await page.getByRole("button", { name: "Submit knowledge file" }).click();
  await expect(
    page.getByText("Admission needs reconciliation", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Remote source status: Not yet reported", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Existing remote source ID").fill("source-1");
  await page
    .getByRole("button", { name: "Reconcile existing source", exact: true })
    .click();
  await expect(
    page.getByText("Remote source status: ingested", { exact: true }),
  ).toBeVisible();
  expect(reconcile).toEqual({ source_id: "source-1" });
  expect(uploads).toBe(1);
  await expect(
    page.getByRole("button", { name: "Done", exact: true }),
  ).toBeInViewport();
});
test("application onboarding bounds configuration actors and keeps signing secret write-only", async ({
  page,
}) => {
  let saved: Record<string, unknown> | null = null;
  const defaults = {
    enabled: false,
    trusted_actors: [],
    configuration_actors: [],
    channel_commands: ["project"],
    backend: "pyfly",
    frontend: "react",
    agent: "codex",
    publish: false,
    allowed_commands: ["status"],
    test_commands: [],
    deploy_enabled: false,
    deploy_targets: [],
  };
  const config = {
    provider: "github",
    enabled: false,
    auto_register: false,
    trusted_actors: [],
    configuration_actors: [],
    defaults,
    webhook_secret_configured: false,
    ingress_path: "/api/v1/webhooks/github",
  };
  await fixtures(page, (path, method, body) => {
    if (path.endsWith("/collaboration/bootstrap/github/receipts")) return [];
    if (path.endsWith("/collaboration/bootstrap/github")) {
      if (method === "PUT") {
        saved = body;
        return { ...config, ...body, webhook_secret_configured: true };
      }
      return config;
    }
    if (path.endsWith("/collaboration"))
      return {
        ...defaults,
        repository_id: "project-1",
        webhook_secret_configured: false,
        capabilities: {
          issue_authoring: true,
          issue_comments: true,
          pull_request_comments: true,
        },
      };
    if (path.endsWith("/collaboration/receipts")) return [];
  });
  await connect(page);
  await navigate(page, "Repository workflow");
  await page
    .getByRole("button", { name: "Application onboarding", exact: true })
    .click();
  await page.getByLabel("Enable signed application commands").check();
  await page.getByLabel("Trusted application actor IDs").fill("actor-1");
  await page
    .getByLabel("Configuration actor IDs", { exact: true })
    .fill("unknown");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "must also appear in trusted actors",
  );
  await page
    .getByLabel("Configuration actor IDs", { exact: true })
    .fill("actor-1");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Default verification commands")
    .fill('[["make","check"]]');
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByText("/api/v1/webhooks/github", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Application webhook signing secret")
    .fill("test-signing-secret");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Save application onboarding", exact: true })
    .click();
  expect(saved).toMatchObject({
    enabled: true,
    trusted_actors: ["actor-1"],
    configuration_actors: ["actor-1"],
    webhook_secret: "test-signing-secret",
  });
  await page.getByRole("button", { name: /Webhook/ }).click();
  await expect(
    page.getByLabel("Application webhook signing secret"),
  ).toHaveValue("");
});
