/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Controlled API fixtures exercise presentation; no model or customer run is executed.
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";

const runId = "2".repeat(32),
  repositoryId = "3".repeat(32);
const checks = Array.from({ length: 7 }, (_, index) => ({
  argv: ["python", "-B", "-m", "pytest", `tests/test_check_${index + 1}.py`],
  exit_code: 0,
  output: `Recorded source check ${index + 1} passed`,
  created_at: "2026-10-29T04:00:00Z",
  attempt: 2,
  execution: "reused",
  source_execution_id: String(index + 1).repeat(32),
  source_record_digest: String(index + 1).repeat(64),
  verification_reuse_intent_digest: "c".repeat(64),
}));
const payload = {
  version: 1,
  run_id: runId,
  task_key: "e".repeat(32),
  source_digest: "f".repeat(64),
  retained_source_receipt_digest: "1".repeat(64),
  handoff_intent_digest: "2".repeat(64),
  scope_digest: "3".repeat(64),
  checks,
  new_execution_count: 0,
  intent_digest: "c".repeat(64),
  verify_task_receipt_digest: "4".repeat(64),
};
const run = {
  id: runId,
  repository_id: repositoryId,
  brief: "Fixture verified source",
  status: "succeeded",
  backend: "pyfly",
  frontend: "none",
  agent: "api",
  verification: checks,
};

for (const width of [1440, 390])
  for (const theme of ["light", "dark"]) {
    test(`reused verification fits ${width}px ${theme} without claiming fresh executions`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const writes: string[] = [];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
          return;
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        if (request.method() !== "GET") writes.push(path);
        const json = path.endsWith("/repositories")
          ? [
              {
                id: repositoryId,
                name: "Fixture verified source",
                url: "/fixture",
                provider: "git",
                default_branch: "main",
              },
            ]
          : path.endsWith("/runs")
            ? [run]
            : path.endsWith("/runs/" + runId)
              ? run
              : path.endsWith("/plan")
                ? { revision: 1, tasks: [] }
                : path.endsWith("/journal")
                  ? {
                      valid: true,
                      entries: [
                        {
                          sequence: 1,
                          task_id: "verify",
                          kind: "verification_reuse_receipt",
                          created_at: "2026-10-29T04:05:00Z",
                          digest: "9".repeat(64),
                          payload,
                        },
                      ],
                    }
                  : [];
        return route.fulfill({ json });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fictional-ui-token-at-least-24-characters");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await navigate(page, "Factory");
      await openFactoryRun(page, /Fixture verified source/);
      await page
        .getByRole("tab", { name: "Verification", exact: true })
        .click();
      const notices = page.getByRole("region", {
        name: "Reused prior check",
        exact: true,
      });
      await expect(notices).toHaveCount(7);
      await expect(notices.first()).toContainText(
        "No new command was executed",
      );
      await expect(notices.first()).toContainText(checks[0].output);
      await notices.first().evaluate((node) => {
        const main = node.closest("main")!;
        main.scrollTop +=
          node.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
      });
      expect(
        await notices
          .first()
          .evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
      ).toBe(true);
      await captureWalkthrough(
        page,
        "verification-reuse-checks",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/verification-reuse.spec.ts",
          observationOnly: true,
        },
      );
      await page.screenshot({
        path: info.outputPath(`reused-checks-${width}-${theme}.png`),
      });
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      const receipt = page.getByRole("region", {
        name: "Verification reuse evidence",
        exact: true,
      });
      await expect(receipt).toContainText("Prior verification results reused");
      await expect(receipt).toContainText(
        "New executions for referenced checks",
      );
      await expect(receipt.locator("pre")).toHaveCount(0);
      await receipt.evaluate((node) => {
        const main = node.closest("main")!;
        main.scrollTop +=
          node.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
      });
      const bounds = await receipt.evaluate((node) => {
        const box = node.getBoundingClientRect();
        const footer = document
          .querySelector(".workspace-main > footer")!
          .getBoundingClientRect();
        return {
          aboveFooter: box.bottom <= footer.top,
          noOverflow: node.scrollWidth <= node.clientWidth + 1,
        };
      });
      expect(bounds).toEqual({ aboveFooter: true, noOverflow: true });
      await captureWalkthrough(
        page,
        "verification-reuse-journal",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/verification-reuse.spec.ts",
          observationOnly: true,
        },
      );
      await page.screenshot({
        path: info.outputPath(`reused-journal-${width}-${theme}.png`),
      });
      expect(writes).toEqual([]);
    });
  }
