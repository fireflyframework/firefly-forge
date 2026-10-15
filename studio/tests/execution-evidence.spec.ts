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
async function openRun(page: Page, toolEntries?: Record<string, unknown>[]) {
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
                  entries: toolEntries ?? [
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
  { width: 375, height: 600 },
])
  for (const theme of ["light", "dark"])
    test(`typed broker WAL preserves superseded unknown, rejected and abandoned evidence at ${viewport.width}px ${theme}`, async ({
      page,
    }) => {
      const first = "1".repeat(32),
        second = "2".repeat(32),
        third = "3".repeat(32);
      const remote =
        "<img src=x onerror=window.injected=true>\n" +
        "Untrusted read result\n".repeat(300);
      const events = [
        {
          kind: "tool_intent",
          idempotency_key: first,
          payload: {
            correlation_id: first,
            tool: "external_mcp:github:get_file_contents",
            arguments: {
              connector: "github",
              tool: "get_file_contents",
              arguments: {
                owner: "team",
                repo: "product",
                path: "src/check.py",
              },
            },
            source: { head: "a".repeat(40), source_tree: "b".repeat(40) },
            configuration: { digest: "c".repeat(64) },
          },
        },
        {
          kind: "tool_unknown",
          idempotency_key: first,
          payload: {
            correlation_id: first,
            tool: "external_mcp:github:get_file_contents",
            status: "unknown",
            error: { message: "Tool outcome was not admitted" },
          },
        },
        {
          kind: "tool_receipt",
          idempotency_key: first,
          payload: {
            correlation_id: first,
            tool: "external_mcp:github:get_file_contents",
            status: "succeeded",
            result: { untrusted: true, text: [remote] },
          },
        },
        {
          kind: "tool_intent",
          idempotency_key: second,
          payload: {
            correlation_id: second,
            tool: "internet_search",
            arguments: { query: "pyfly DI", max_results: 1 },
          },
        },
        {
          kind: "tool_unknown",
          idempotency_key: second,
          payload: {
            correlation_id: second,
            tool: "internet_search",
            status: "unknown",
            error: { message: "Tool execution was interrupted" },
          },
        },
        {
          kind: "tool_reconciled",
          idempotency_key: "4".repeat(32),
          payload: {
            correlation_id: second,
            decision: "abandoned_without_replay",
            actor: { actor_id: "fixture-operator" },
          },
        },
        {
          kind: "tool_intent",
          idempotency_key: third,
          payload: {
            correlation_id: third,
            tool: "internet_search",
            arguments: { query: "pyfly DI", max_results: 1 },
          },
        },
        {
          kind: "tool_receipt",
          idempotency_key: third,
          payload: {
            correlation_id: third,
            tool: "internet_search",
            status: "rejected",
            error: {
              kind: "quota",
              status_code: 402,
              message: "Provider quota exhausted",
            },
          },
        },
      ].map((event, index) => ({
        ...event,
        sequence: index + 1,
        task_id: "implement",
        created_at: "2026-10-13T15:00:00Z",
        digest: String(index + 1).repeat(64),
        previous_hash: index ? String(index).repeat(64) : "0".repeat(64),
      }));
      await page.setViewportSize(viewport);
      await openRun(page, events);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      const rows = page.getByRole("tabpanel").locator(".journal-entries>li");
      await expect(rows).toHaveCount(8);
      await expect(rows.nth(1)).toContainText(
        "Superseded by a succeeded receipt",
      );
      await expect(rows.nth(1)).not.toContainText("reconciliation required");
      await expect(rows.nth(4)).toContainText("Abandoned without replay");
      await expect(rows.nth(5)).toContainText("fixture-operator");
      await expect(rows.nth(7)).toContainText("Rejected receipt recorded");
      await expect(rows.nth(7)).not.toContainText("Succeeded");
      await expect(rows.locator("img,script")).toHaveCount(0);
      await expect(rows.locator(".record-advanced[open]")).toHaveCount(0);
      await expect(rows.nth(2)).toContainText("3".repeat(64));
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
      const identity = rows.nth(2).locator(".record-tool-identity");
      await expect(identity).not.toHaveAttribute("open", "");
      await identity.locator("summary").focus();
      await page.keyboard.press("Enter");
      const machineId = identity.locator(".record-tool-identity-text");
      await expect(machineId).toHaveText(
        "external_mcp:github:get_file_contents",
      );
      const machineLayout = await machineId.evaluate((element) => {
        const style = getComputedStyle(element);
        const range = document.createRange();
        range.selectNodeContents(
          element.querySelector(".record-bounded-label")!,
        );
        return {
          whiteSpace: style.whiteSpace,
          overflowX: style.overflowX,
          textLines: range.getClientRects().length,
        };
      });
      expect(machineLayout).toEqual({
        whiteSpace: "nowrap",
        overflowX: "auto",
        textLines: 1,
      });
      await identity
        .getByRole("button", { name: "Copy tool identity", exact: true })
        .click();
      expect(
        await page.evaluate(
          () => (window as Window & { copied?: string }).copied,
        ),
      ).toBe("external_mcp:github:get_file_contents");
      await identity.locator("summary").click();
      const copy = rows
        .nth(2)
        .getByRole("button", { name: "Copy full output", exact: true })
        .first();
      await copy.focus();
      const box = await copy.boundingBox(),
        footer = await page.locator(".workspace-main>footer").boundingBox();
      expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
      expect(
        await copy.evaluate((button) => {
          const r = button.getBoundingClientRect();
          return button.contains(
            document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
          );
        }),
      ).toBe(true);
      await page.keyboard.press("Enter");
      expect(
        await page.evaluate(
          () => (window as Window & { copied?: string }).copied,
        ),
      ).toBe(remote);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(
        await page.evaluate(
          () => (window as Window & { injected?: boolean }).injected,
        ),
      ).toBeUndefined();
      await page.screenshot({
        path: test.info().outputPath(`typed-tool-${theme}.png`),
      });
    });
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
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
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

for (const width of [320, 1440])
  for (const theme of ["light", "dark"])
    for (const recipe of ["runtime", "xdebug"])
      test(`PHP profile observations ${recipe} remain readable at ${width}px ${theme}`, async ({
        page,
      }) => {
        await page.setViewportSize({ width, height: 700 });
        const result = {
          recipe: `forge-php-${recipe}-v1`,
          observation_only: true,
          best_effort: true,
          script: "bin/larafly-workload.php",
          source_sha256: "a".repeat(64),
          image: "example@sha256:" + "b".repeat(64),
          argv: ["php", "-r", "TRUSTED_LAUNCHER"],
          exit_code: 7,
          output_truncated: false,
          report_marker_observed: true,
          receipt: {
            execution_id: "1".repeat(32),
            request_digest: "2".repeat(64),
            snapshot_digest: "3".repeat(64),
            workspace_digest: "4".repeat(64),
            start_key: "1".repeat(32) + ":start",
            cleanup_key: "1".repeat(32) + ":cleanup",
          },
          output:
            "Workload output\nFORGE_PHP_PROFILE_V1\n" +
            JSON.stringify({
              recipe: `forge-php-${recipe}-v1`,
              observation_only: true,
              best_effort: true,
              complete: true,
              incomplete_reason: null,
              workload_exit_code: 7,
              metrics: {
                wall_ns: 1500000,
                cpu_user_us: 750,
                cpu_system_us: 250,
                peak_memory_bytes: 2097152,
              },
              callgraph:
                recipe === "xdebug"
                  ? {
                      events: ["Time_(10ns)", "Memory_(bytes)"],
                      rows_truncated: false,
                      rows: [
                        {
                          file: "src/Workload.php",
                          function: "measureWorkload",
                          self_time: 100000,
                          self_memory: 32,
                          calls: 2,
                        },
                      ],
                    }
                  : null,
            }) +
            "\n",
        };
        await openRun(page, [
          {
            sequence: 1,
            task_id: "implement",
            kind: "diagnostic_observation",
            created_at: "2026-10-15T15:00:00Z",
            digest: "b".repeat(64),
            previous_hash: "0".repeat(64),
            idempotency_key: "1".repeat(32) + ":php_profile",
            payload: result,
          },
        ]);
        await page
          .getByLabel("Appearance", { exact: true })
          .selectOption(theme);
        await page.getByRole("tab", { name: "Plan", exact: true }).click();
        const profile = page.getByRole("region", {
          name: "PHP profiling observation",
          exact: true,
        });
        await expect(
          profile.getByText("Report complete", { exact: true }),
        ).toBeVisible();
        await expect(
          profile.getByText("1.5 ms", { exact: true }),
        ).toBeVisible();
        await expect(profile.getByText("2 MiB", { exact: true })).toBeVisible();
        await expect(profile.getByText("7", { exact: true })).toBeVisible();
        await expect(
          page.getByText("TRUSTED_LAUNCHER", { exact: true }),
        ).not.toBeVisible();
        const heading = profile.getByRole("heading", {
          name:
            recipe === "xdebug" ? "PHP Xdebug profile" : "PHP runtime profile",
          exact: true,
        });
        await heading.scrollIntoViewIfNeeded();
        await page.screenshot({
          path: test
            .info()
            .outputPath(`php-profile-heading-${recipe}-${width}-${theme}.png`),
        });
        const summary = page.getByText("Original workload output and report", {
          exact: true,
        });
        await summary.focus();
        const bounds = await summary.boundingBox(),
          footer = await page.locator(".workspace-main > footer").boundingBox();
        expect(bounds && footer && bounds.y + bounds.height <= footer.y).toBe(
          true,
        );
        await expect
          .poll(() =>
            page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth,
            ),
          )
          .toBe(true);
        await page.screenshot({
          path: test
            .info()
            .outputPath(`php-profile-${recipe}-${width}-${theme}.png`),
        });
        if (recipe === "xdebug") {
          const costs = profile.getByRole("region", {
            name: "Function costs",
            exact: true,
          });
          await costs.focus();
          await expect(costs.getByText("1 ms", { exact: true })).toBeVisible();
          if (width === 320) {
            await costs.evaluate((element) => {
              element.scrollLeft = 0;
            });
            await page.keyboard.press("ArrowRight");
            await expect
              .poll(() => costs.evaluate((element) => element.scrollLeft))
              .toBeGreaterThan(0);
          }
          await page.screenshot({
            path: test
              .info()
              .outputPath(`php-profile-functions-${width}-${theme}.png`),
          });
        }
        await summary.focus();
        await page.keyboard.press("Enter");
        await expect(
          summary
            .locator("..")
            .getByRole("region", { name: "Output", exact: true }),
        ).toBeVisible();
      });
