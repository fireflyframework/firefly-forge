/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { readFileSync } from "node:fs";
const launcherFixture = JSON.parse(
  readFileSync(new URL("./php-launcher-fixture.json", import.meta.url), "utf8"),
) as { argv: string[] };

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

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 375, height: 600 },
])
  for (const theme of ["light", "dark"])
    test(`unavailable read recovery remains readable at ${viewport.width}px ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await openRun(page, [
        {
          sequence: 1,
          task_id: "implement",
          kind: "readonly_observation_recovery",
          created_at: "2026-10-28T00:00:00Z",
          digest: "b".repeat(64),
          previous_hash: "0".repeat(64),
          idempotency_key: "2".repeat(32),
          payload: {
            version: 1,
            kind: "reviewed_readonly_observation_continuation",
            run_id: "1".repeat(32),
            request_id: "a".repeat(32),
            recovery_id: "2".repeat(32),
            read_proof_digest: "3".repeat(64),
            request: {
              version: 1,
              candidate_digest: "4".repeat(64),
              valid_for_seconds: 120,
              acknowledge_unavailable_observation: true,
              acknowledge_no_original_callback_replay: true,
              acknowledge_same_request_attempt_and_budgets: true,
              acknowledge_finite_ownership_lease: true,
            },
            candidate: {
              version: 1,
              kind: "readonly_observation_recovery_candidate",
              run_id: "1".repeat(32),
              request_id: "a".repeat(32),
              admission: "READY",
              execution_authorized: false,
              review_basis: "administrator_reviewed_unavailable_read",
              digest: "4".repeat(64),
              read_proof_digest: "3".repeat(64),
            },
            checkpoint: "CONTROLLED_CHECKPOINT_NOT_INITIAL_COPY",
          },
        },
      ]);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      const row = page.getByRole("tabpanel").locator(".journal-entries>li");
      await expect(row).toHaveCount(1);
      await expect(row).toContainText("Unavailable read reviewed");
      await expect(row).toContainText(
        "original unknown result remains unknown",
      );
      await expect(row.locator(".record-advanced[open]")).toHaveCount(0);
      await expect(
        row.getByText("CONTROLLED_CHECKPOINT_NOT_INITIAL_COPY", {
          exact: true,
        }),
      ).not.toBeVisible();
      const summary = row.locator(".record-advanced>summary");
      await summary.focus();
      const box = await summary.boundingBox();
      const footer = await page.locator(".workspace-main>footer").boundingBox();
      expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      await row
        .getByRole("heading", { name: "Unavailable read reviewed", exact: true })
        .scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath("unavailable-read-recovery.png"),
        fullPage: true,
      });
      await summary.focus();
      await page.keyboard.press("Enter");
      await expect(row.locator(".record-advanced")).toHaveAttribute("open", "");
      await expect(row).toContainText("CONTROLLED_CHECKPOINT_NOT_INITIAL_COPY");
    });

async function openRun(
  page: Page,
  toolEntries?: Record<string, unknown>[],
  archived?: { runId: string; events: Record<string, unknown>[] },
) {
  const selectedRun = archived
    ? { ...run, id: archived.runId, events: archived.events }
    : run;
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
        ? [selectedRun]
        : path.endsWith("/runs/" + selectedRun.id)
          ? selectedRun
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

// The argv is exported from a real fictional cached-PHP command. API responses
// here remain controlled presentation fixtures, never native execution proof.
for (const commandCase of [
  {
    label: "actual nested PHP",
    argv: launcherFixture.argv,
    viewports: [
      { width: 320, height: 600 },
      { width: 390, height: 844 },
      { width: 1440, height: 700 },
      { width: 844, height: 390 },
      { width: 320, height: 400 },
    ],
  },
  {
    label: "wide-character nested",
    argv: ["php", "界".repeat(152)],
    viewports: [
      { width: 320, height: 400 },
      { width: 844, height: 390 },
    ],
  },
])
  for (const viewport of commandCase.viewports)
    for (const theme of ["light", "dark"])
      test(`${commandCase.label} command stays compact and fully recoverable at ${viewport.width}x${viewport.height}px ${theme}`, async ({
        page,
        context,
      }) => {
        await page.setViewportSize(viewport);
        await context.grantPermissions(["clipboard-read", "clipboard-write"]);
        await openRun(page, [
          {
            sequence: 1,
            task_id: "implement",
            kind: "sandbox_intent",
            created_at: "2026-10-15T15:00:00Z",
            digest: "a".repeat(64),
            previous_hash: "0".repeat(64),
            idempotency_key: "1".repeat(32) + ":start",
            payload: {
              execution_id: "1".repeat(32),
              effect: "start",
              request: { request: { argv: commandCase.argv }, ordinal: 1 },
            },
          },
        ]);
        await page
          .getByLabel("Appearance", { exact: true })
          .selectOption(theme);
        await expect(
          page.getByRole("img", { name: "Firefly forge", exact: true }).first(),
        ).toHaveAttribute("src", `/assets/forge-lockup-${theme}.svg`);
        await page.getByRole("tab", { name: "Plan", exact: true }).click();
        const command = page
          .locator(".journal-entries .record-command")
          .first();
        const heading = command.locator(".record-command-heading").first();
        if (commandCase.label === "actual nested PHP")
          await expect(heading.locator("code")).toContainText(
            "memory_limit=64M -r $payload =",
          );
        await heading.scrollIntoViewIfNeeded();
        await expect
          .poll(() =>
            heading.evaluate(
              (element) => element.getBoundingClientRect().height,
            ),
          )
          .toBeLessThanOrEqual(240);
        const visible = async (target: typeof heading) => {
          await target.scrollIntoViewIfNeeded();
          await expect
            .poll(() =>
              target.evaluate((element) => {
                const r = element.getBoundingClientRect();
                const top = document
                  .querySelector(".workspace-main > .topbar")!
                  .getBoundingClientRect().bottom;
                const bottom = document
                  .querySelector(".workspace-main > footer")!
                  .getBoundingClientRect().top;
                return (
                  r.top >= top + 3 &&
                  r.bottom <= bottom - 3 &&
                  [
                    [r.left + 2, r.top + 2],
                    [r.right - 2, r.top + 2],
                    [r.left + 2, r.bottom - 2],
                    [r.right - 2, r.bottom - 2],
                  ].every(([x, y]) => {
                    const hit = document.elementFromPoint(x, y);
                    return hit === element || element.contains(hit);
                  })
                );
              }),
            )
            .toBe(true);
        };
        await visible(heading);
        await expect(
          command.getByText("Outcome not recorded", { exact: true }).first(),
        ).toBeVisible();
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
            .outputPath(`command-compact-${viewport.width}-${theme}.png`),
        });
        const summary = command
          .getByText("Full command", { exact: true })
          .first();
        const details = summary.locator("..");
        expect(await details.getAttribute("open")).toBeNull();
        await summary.focus();
        await visible(summary);
        await summary.press("Enter");
        const full = details.getByRole("region", {
          name: "Command",
          exact: true,
        });
        const copy = full.getByRole("button", {
          name: "Copy full command",
          exact: true,
        });
        await copy.focus();
        await visible(copy);
        await copy.press("Enter");
        const expected = commandCase.argv
          .map((arg) => (/\s/.test(arg) ? JSON.stringify(arg) : arg))
          .join(" ");
        await expect
          .poll(() => page.evaluate(() => navigator.clipboard.readText()))
          .toBe(expected);
        const text = full.getByLabel("Command text", { exact: true });
        await text.focus();
        await visible(text);
        expect(
          await text.evaluate(
            (element) => element.getBoundingClientRect().height,
          ),
        ).toBeLessThanOrEqual(320);
        if (expected.length > 10000) {
          expect(
            await text.evaluate(
              (element) => element.scrollHeight > element.clientHeight,
            ),
          ).toBe(true);
          await page.keyboard.press("ArrowDown");
          await expect
            .poll(() => text.evaluate((element) => element.scrollTop))
            .toBeGreaterThan(0);
        }
        if (expected.length > 10000) {
          const more = full.getByRole("button", {
            name: "Show more",
            exact: true,
          });
          await more.focus();
          await visible(more);
          await more.press("Enter");
        }
        await expect(text).toHaveText(expected);
        await text.focus();
        await visible(text);
        await page.screenshot({
          path: test
            .info()
            .outputPath(`command-full-${viewport.width}-${theme}.png`),
        });
        await summary.focus();
        await summary.press("Enter");
        expect(await details.getAttribute("open")).toBeNull();
        await visible(heading);
      });

// These are controlled UI fixtures, not results from a generated CRM or a live provider.
const archivedFixture = JSON.parse(
  readFileSync(
    new URL(
      "../../tests/fixtures/activity-archive-contract-v1.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as {
  run_id: string;
  event: Record<string, unknown>;
  sanitized_message: string;
  chunks: Array<Record<string, unknown>>;
};
async function openArchive(
  page: Page,
  read: (chunk: number) => Record<string, unknown>,
) {
  await openRun(page, undefined, {
    runId: archivedFixture.run_id,
    events: [{ ...archivedFixture.event, created_at: "2026-10-20T15:30:00Z" }],
  });
  await page.route(/\/activity\/[a-f0-9]{32}\/content\?/, async (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("expected_descriptor")).toBe(
      archivedFixture.chunks[0].descriptor_sha256,
    );
    await route.fulfill({ json: read(Number(url.searchParams.get("chunk"))) });
  });
}
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
])
  for (const theme of ["light", "dark"])
    test(`archived provider event renders a verified full read at ${viewport.width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const calls: number[] = [];
      await openArchive(page, (chunk) => {
        calls.push(chunk);
        return archivedFixture.chunks[chunk];
      });
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const panel = page.getByRole("tabpanel");
      await expect(
        panel.getByRole("heading", {
          name: "Provider command finished",
          exact: true,
        }),
      ).toBeVisible();
      await expect(panel.getByText("Exit 0", { exact: true })).toBeVisible();
      expect(calls).toEqual([]);
      await panel.getByText(/Full recorded event ·/).click();
      await panel
        .getByRole("button", { name: "Read full recorded event", exact: true })
        .click();
      await expect(
        panel.getByRole("button", {
          name: "Copy full recorded event",
          exact: true,
        }),
      ).toBeVisible();
      expect(calls).toEqual([0, 1]);
      await expect(
        panel.getByRole("region", {
          name: "Verified recorded event",
          exact: true,
        }),
      ).toContainText("item.completed");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await expect(
        panel.getByLabel("Verified recorded event text", { exact: true }),
      ).toBeFocused();
      const fullCopy = panel.getByRole("button", {
        name: "Copy full recorded event",
        exact: true,
      });
      await fullCopy.scrollIntoViewIfNeeded();
      await expect(fullCopy).toBeInViewport();
      const button = await fullCopy.boundingBox();
      const footer = await page.locator("footer").boundingBox();
      expect(footer).not.toBeNull();
      expect(button).not.toBeNull();
      expect(button!.y + button!.height).toBeLessThanOrEqual(footer!.y);
      expect(button?.width).toBeGreaterThan(0);
      await page.screenshot({
        path: test.info().outputPath(`archive-${viewport.width}-${theme}.png`),
      });
    });
test("archived provider event refuses a changed full record without exposing partial content", async ({
  page,
}) => {
  await openArchive(page, (chunk) => ({
    ...archivedFixture.chunks[chunk],
    descriptor_sha256: "f".repeat(64),
  }));
  const panel = page.getByRole("tabpanel");
  await panel.getByText(/Full recorded event ·/).click();
  await panel
    .getByRole("button", { name: "Read full recorded event", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toContainText(
    "could not be read and verified",
  );
  await expect(
    panel.getByRole("button", {
      name: "Copy full recorded event",
      exact: true,
    }),
  ).toHaveCount(0);
});

const claudeArchiveFixture = JSON.parse(
  readFileSync(
    new URL(
      "../../tests/fixtures/activity-claude-contract-v1.json",
      import.meta.url,
    ),
    "utf8",
  ),
) as {
  run_id: string;
  records: Array<{
    event: Record<string, unknown>;
    raw_message: string;
    chunk: Record<string, unknown>;
  }>;
};
for (const width of [390, 1440])
  for (const theme of ["light", "dark"])
    test(`Claude activity projections remain honest and readable at ${width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 844 });
      await openRun(page, undefined, {
        runId: claudeArchiveFixture.run_id,
        events: claudeArchiveFixture.records.map((item) => item.event),
      });
      let reads = 0;
      await page.route(/\/activity\/[a-f0-9]{32}\/content\?/, async (route) => {
        reads++;
        const item = claudeArchiveFixture.records.find((record) =>
          route.request().url().includes(String(record.event.id)),
        )!;
        await route.fulfill({ json: item.chunk });
      });
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const panel = page.getByRole("tabpanel");
      await expect(
        panel.getByRole("heading", {
          name: "Provider message recorded",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        panel.getByRole("region", {
          name: "Observed tool request",
          exact: true,
        }),
      ).toHaveCount(3);
      await expect(
        panel.getByText(
          "3 additional content blocks are available in the full recorded event.",
          { exact: true },
        ),
      ).toHaveCount(1);
      await expect(
        panel.getByText("Provider reported an error", { exact: true }),
      ).toHaveCount(1);
      await expect(
        panel.getByRole("heading", {
          name: "Provider result reported",
          exact: true,
        }),
      ).toHaveCount(1);
      await expect(
        panel.getByRole("heading", {
          name: "Structured provider output recorded",
          exact: true,
        }),
      ).toHaveCount(1);
      expect(await panel.textContent()).not.toContain("OPAQUE_PROVIDER_FIELD");
      await expect(panel.getByText(/^Exit /)).toHaveCount(0);
      expect(reads).toBe(0);
      const card = panel
        .getByRole("region", { name: "Archived provider event", exact: true })
        .first();
      const longName = card.getByText("mcp__" + "x".repeat(59), {
        exact: true,
      });
      await expect(
        card.getByText("tool-" + "a".repeat(123), { exact: true }),
      ).not.toBeVisible();
      await longName.scrollIntoViewIfNeeded();
      expect((await longName.boundingBox())!.width).toBeLessThanOrEqual(
        (await card.boundingBox())!.width - 28,
      );
      await page.screenshot({
        path: test
          .info()
          .outputPath(`claude-activity-summary-${width}-${theme}.png`),
      });
      await card.getByText(/Full recorded event ·/).click();
      const read = card.getByRole("button", {
        name: "Read full recorded event",
        exact: true,
      });
      await read.scrollIntoViewIfNeeded();
      const bounds = await read.boundingBox(),
        footer = await page.locator(".workspace-main > footer").boundingBox();
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(footer!.y);
      await read.click();
      await expect(
        card.getByLabel("Verified recorded event text", { exact: true }),
      ).toBeFocused();
      expect(reads).toBe(1);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: test.info().outputPath(`claude-activity-${width}-${theme}.png`),
      });
    });

const codexMCPFixture = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/activity-codex-mcp-v1.json", import.meta.url),
    "utf8",
  ),
) as {
  run_id: string;
  records: Array<{
    event: Record<string, unknown>;
    chunk: Record<string, unknown>;
  }>;
};
// Real redacted finite-client messages in controlled API wrappers; no product QA or native WAL export claim.
for (const viewport of [
  { width: 1440, height: 900, zoom: 1 },
  { width: 390, height: 844, zoom: 1 },
  { width: 1280, height: 600, zoom: 1 },
  { width: 1440, height: 900, zoom: 2 },
])
  for (const theme of ["light", "dark"]) {
    test(`Codex MCP activity is readable at ${viewport.width}px ${viewport.height}px ${viewport.zoom}x ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await openRun(page, undefined, {
        runId: codexMCPFixture.run_id,
        events: codexMCPFixture.records.map((record) => record.event),
      });
      let reads = 0;
      await page.route(/\/activity\/[a-f0-9]{32}\/content\?/, async (route) => {
        reads++;
        const record = codexMCPFixture.records.find((record) =>
          route.request().url().includes(String(record.event.id)),
        )!;
        await route.fulfill({ json: record.chunk });
      });
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      if (viewport.zoom === 2)
        await page.evaluate(() => {
          document.documentElement.style.zoom = "2";
        });
      const cards = page.getByRole("region", {
        name: "Archived provider event",
        exact: true,
      });
      await expect(cards).toHaveCount(2);
      const card = cards.last();
      await expect(
        card.getByRole("heading", {
          name: "Provider tool activity",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        card.getByText("Run configured project check", { exact: true }),
      ).toBeVisible();
      const identity = card.getByText("forge_coding · run_configured_check", {
        exact: true,
      });
      await expect(identity).not.toBeVisible();
      await card.getByText("Tool identity", { exact: true }).click();
      await expect(identity).toBeVisible();
      await card.getByText("Tool identity", { exact: true }).click();
      await expect(
        card.getByText(/Provider reported completion/),
      ).toContainText("Reported exit 0");
      await expect(
        card.getByLabel("Provider-reported output preview text", {
          exact: true,
        }),
      ).toContainText("NATIVE_PREREQUISITE_SAFE_RESULT");
      await expect(
        card.getByText(
          "Native runner receipts and final verification are tracked separately.",
          { exact: true },
        ),
      ).toBeVisible();
      expect(reads).toBe(0);
      await card.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: test
          .info()
          .outputPath(
            `codex-mcp-summary-${viewport.width}-${viewport.height}-${viewport.zoom}-${theme}.png`,
          ),
      });
      await card.getByText(/Full recorded event ·/).click();
      const button = card.getByRole("button", {
        name: "Read full recorded event",
        exact: true,
      });
      await button.scrollIntoViewIfNeeded();
      const box = await button.boundingBox();
      const footer = await page
        .locator(".workspace-main > footer")
        .boundingBox();
      expect(box!.y).toBeGreaterThanOrEqual(0);
      expect(box!.y + box!.height).toBeLessThanOrEqual(
        Math.min(footer!.y, viewport.height),
      );
      await button.focus();
      await page.keyboard.press("Enter");
      await expect(
        card.getByLabel("Verified recorded event text", { exact: true }),
      ).toBeFocused();
      expect(reads).toBe(1);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: test
          .info()
          .outputPath(
            `codex-mcp-full-${viewport.width}-${viewport.height}-${viewport.zoom}-${theme}.png`,
          ),
      });
    });
  }

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 700 },
])
  for (const theme of ["light", "dark"])
    test(`agent tool custody is readable at ${viewport.width}px ${theme}`, async ({
      page,
    }, testInfo) => {
      const shared = {
        version: 1,
        custody_id: "a".repeat(64),
        tool_call_id: "call_source_7",
        tool: "read_file",
        effect: "source_read",
        invocation_id: "request-3",
        task_key: "implement",
        task_attempt: 2,
        request_step: 7,
        arguments_sha256: "b".repeat(64),
        binding_sha256: "c".repeat(64),
      };
      const entries = [
        {
          kind: "agentic_tool_intent",
          digest: "d".repeat(64),
          payload: { ...shared, arguments: { path: "src/customer.py" } },
        },
        {
          kind: "agentic_tool_result",
          digest: "e".repeat(64),
          payload: {
            ...shared,
            intent_digest: "d".repeat(64),
            status: "retained",
            result: {
              content:
                "class Customer:\n    pass\n<img src=x onerror=window.injected=true>",
            },
            result_sha256: "f".repeat(64),
          },
        },
      ].map((entry, index) => ({
        ...entry,
        sequence: index + 1,
        task_id: "implement",
        idempotency_key: shared.custody_id,
        created_at: "2026-10-27T14:00:00Z",
      }));
      await page.setViewportSize(viewport);
      await openRun(page, entries);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      const rows = page.getByRole("tabpanel").locator(".journal-entries>li");
      await expect(rows).toHaveCount(2);
      await expect(rows.nth(0)).toContainText("Result retained");
      await expect(rows.nth(1).getByLabel("Agent tool evidence")).toContainText(
        "Read a source file",
      );
      await expect(rows.nth(1)).toContainText("class Customer");
      await expect(rows.locator("img,script")).toHaveCount(0);
      await expect(rows.locator(".record-advanced[open]")).toHaveCount(0);
      const copy = rows
        .nth(1)
        .getByRole("button", { name: "Copy full output", exact: true })
        .first();
      await copy.focus();
      const box = await copy.boundingBox();
      const footer = await page.locator(".workspace-main>footer").boundingBox();
      expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(
          `tool-custody-${viewport.width}-${theme}.png`,
        ),
      });
    });
