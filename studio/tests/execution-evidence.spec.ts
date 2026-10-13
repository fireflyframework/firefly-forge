/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

// Controlled API fixtures verify presentation only; they are not execution evidence.
const output = "Reservation rejected\n" + "check output\n".repeat(300);
const run = {
  id: "fixture-run",
  repository_id: "fixture-repo",
  brief: "Fixture reservation verification",
  backend: "pyfly",
  frontend: "react",
  agent: "codex",
  status: "needs_reconciliation",
  events: [
    {
      message: "Verification command returned",
      created_at: "2026-10-13T15:00:00Z",
      actor_id: "fixture-operator",
      argv: ["npm", "test"],
      exit_code: 2,
      output,
    },
  ],
  verification: [
    {
      argv: ["npm", "test"],
      exit_code: 2,
      duration_ms: 1530,
      output,
      stderr: "<img src=x onerror=window.injected=true>",
      actor_id: "fixture-operator",
    },
  ],
  changed_files: ["src/check.py"],
  diff: "diff --git a/src/check.py b/src/check.py\n--- a/src/check.py\n+++ b/src/check.py\n@@ -10,2 +10,2 @@ def check():\n-    return False\n+    return True\n     # reservation bound",
  artifacts: [
    {
      name: "changes.zip",
      size: 2048,
      url: "/api/v1/runs/fixture-run/artifacts/changes.zip",
      extension: { retained: true },
    },
  ],
};
async function openRun(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    const json = path.endsWith("/repositories")
      ? [
          {
            id: "fixture-repo",
            name: "Fixture Inventory",
            provider: "git",
            url: "/local/fixture",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [run]
        : path.endsWith("/runs/fixture-run")
          ? run
          : path.endsWith("/plan")
            ? {
                revision: 1,
                tasks: [
                  {
                    id: "verify",
                    title: "Verify reservation behavior",
                    status: "needs_reconciliation",
                    attempt: 1,
                    instructions: "Inspect retained command evidence.",
                    evidence: {
                      checks: run.verification,
                      source_tree: "a".repeat(40),
                    },
                  },
                ],
              }
            : path.endsWith("/journal")
              ? {
                  valid: true,
                  entries: [
                    {
                      sequence: 1,
                      task_id: "verify",
                      kind: "intent",
                      created_at: "2026-10-13T15:00:00Z",
                      digest: "b".repeat(64),
                      previous_hash: "0".repeat(64),
                      idempotency_key: "fixture-intent",
                      payload: {
                        actor_id: "fixture-operator",
                        reason: "unknown effect",
                        argv: ["npm", "test"],
                      },
                    },
                  ],
                }
              : path.endsWith("/health")
                ? { status: "ok", execution_ready: false }
                : path.endsWith("/providers")
                  ? [{ id: "codex", ready: false }]
                  : [];
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Fixture reservation verification/ })
    .click();
}
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 375, height: 667 },
  { width: 812, height: 375 },
]) {
  for (const theme of ["dark", "light"]) {
    test(`execution evidence remains readable and truthful at ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await openRun(page);
      await page.evaluate(
        (value) => (document.documentElement.dataset.theme = value),
        theme,
      );
      const panel = page.getByRole("tabpanel");
      await page
        .getByRole("tab", { name: "Verification", exact: true })
        .click();
      await expect(panel.getByText("Exit 2", { exact: true })).toBeVisible();
      await expect(panel.getByText("Failed", { exact: true })).toBeVisible();
      await expect(panel.getByText("1.53 s", { exact: true })).toBeVisible();
      await expect(panel.locator("img")).toHaveCount(0);
      await expect(panel.locator("details")).not.toHaveAttribute("open", "");
      await panel
        .getByRole("button", { name: "Show more", exact: true })
        .click();
      await expect(
        panel.getByRole("button", { name: "Show less", exact: true }),
      ).toBeVisible();
      await page.screenshot({
        path: test.info().outputPath(`verification-${theme}.png`),
        fullPage: true,
      });
      await page.getByRole("tab", { name: "Changes", exact: true }).click();
      const changes = panel.getByRole("region", {
        name: "Code changes for src/check.py",
      });
      await expect(
        changes.getByRole("cell", { name: "Added", exact: true }),
      ).toBeVisible();
      await expect(
        changes.getByRole("cell", { name: "Removed", exact: true }),
      ).toBeVisible();
      await expect(
        changes.locator(".code-token-keyword").filter({ hasText: "return" }),
      ).toHaveCount(2);
      await changes.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: test.info().outputPath(`changes-${theme}.png`),
      });
      await page
        .getByRole("tab", { name: "Changes", exact: true })
        .press("ArrowRight");
      await expect(
        page.getByRole("tab", { name: "Verification", exact: true }),
      ).toBeFocused();
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      await expect(
        panel.getByText("Chain verified by server", { exact: true }),
      ).toBeVisible();
      await expect(
        panel.getByText("Outcome not recorded", { exact: true }).first(),
      ).toBeVisible();
      await expect(
        panel.getByText("b".repeat(64), { exact: true }),
      ).toBeVisible();
      await expect(
        panel.getByText("0".repeat(64), { exact: true }),
      ).toBeVisible();
      await panel
        .getByText("fixture-intent", { exact: true })
        .scrollIntoViewIfNeeded();
      const keyBounds = await panel
        .getByText("fixture-intent", { exact: true })
        .boundingBox();
      const footerBounds = await page
        .locator(".workspace-main > footer")
        .boundingBox();
      expect(
        keyBounds &&
          footerBounds &&
          keyBounds.y + keyBounds.height <= footerBounds.y,
      ).toBe(true);
      await page.screenshot({
        path: test.info().outputPath(`journal-${theme}.png`),
      });
      await page.getByRole("tab", { name: "Artifacts", exact: true }).click();
      await expect(
        panel.getByText("changes.zip", { exact: true }),
      ).toBeVisible();
      await expect(panel.getByText("2048", { exact: true })).toBeVisible();
      await expect(panel.locator("details")).not.toHaveAttribute("open", "");
      await panel.locator("summary").click();
      await expect(
        panel.getByRole("region", { name: "Raw record" }),
      ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      expect(
        await page.evaluate(
          () => (window as Window & { injected?: boolean }).injected,
        ),
      ).toBeUndefined();
      await page.screenshot({
        path: test.info().outputPath(`evidence-${theme}.png`),
        fullPage: true,
      });
    });
  }
}
test("copy preserves the entire bounded output and patch", async ({ page }) => {
  await openRun(page);
  await page.evaluate(() =>
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async (value: string) => {
          (window as Window & { copied?: string }).copied = value;
        },
      },
      configurable: true,
    }),
  );
  await page.getByRole("tab", { name: "Verification", exact: true }).click();
  await page
    .getByRole("tabpanel")
    .getByRole("button", { name: "Copy full output", exact: true })
    .first()
    .click();
  await expect
    .poll(() =>
      page.evaluate(() => (window as Window & { copied?: string }).copied),
    )
    .toBe(output);
  await page.getByRole("tab", { name: "Changes", exact: true }).click();
  await page.getByRole("button", { name: "Copy patch", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(() => (window as Window & { copied?: string }).copied),
    )
    .toBe(run.diff);
});
