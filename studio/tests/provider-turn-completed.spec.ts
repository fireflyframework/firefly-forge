/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import fixture from "./provider-turn-completed-fixture.json" with { type: "json" };
import { mockIdentity } from "./auth-fixture";

for (const width of [1440, 390])
  for (const theme of ["light", "dark"])
    test(`completed provider turn is readable at ${width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      let contentReads = 0;
      const writes: string[] = [];
      const run = {
        id: fixture.run_id,
        repository_id: "presentation-repository",
        brief: "Review a completed coding turn",
        backend: "pyfly",
        frontend: "react",
        agent: "codex",
        status: "failed",
        events: [
          fixture.worker_report.event,
          fixture.unknown_report.event,
          fixture.event,
          {
            stage: "failed",
            created_at: "2026-10-23T22:33:03Z",
            message:
              "The implementation worker reported failure: Independent browser, SQL and restart acceptance remain outstanding. Existing records must be preserved.",
          },
        ],
        verification: [],
        changed_files: [],
        artifacts: [],
        diff: "",
      };
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (request.method() !== "GET") writes.push(path);
        if (path.includes("/activity/")) contentReads++;
        const json = path.endsWith("/repositories")
          ? [
              {
                id: run.repository_id,
                name: "Presentation fixture",
                provider: "git",
                url: "/local/fixture",
                default_branch: "main",
              },
            ]
          : path.endsWith("/runs")
            ? [run]
            : path.endsWith("/runs/" + run.id)
              ? run
              : path.endsWith("/health")
                ? { status: "ok", execution_ready: false }
                : path.endsWith("/providers")
                  ? [{ id: "codex", ready: false }]
                  : [];
        await route.fulfill({ json });
      });
      await page.goto("/?run=" + fixture.run_id);
      await page
        .getByRole("button", { name: "Connect factory", exact: true })
        .click();
      await page
        .getByLabel("API token", { exact: true })
        .fill("public-presentation-fixture-only");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const card = page
        .getByRole("region", { name: "Archived provider event", exact: true })
        .filter({
          has: page.getByRole("heading", {
            name: "Provider task completed",
            exact: true,
          }),
        });
      await expect(
        card.getByRole("heading", {
          name: "Provider task completed",
          exact: true,
        }),
      ).toBeVisible();
      await card.scrollIntoViewIfNeeded();
      const usage = card.getByLabel("Provider-reported token usage", {
        exact: true,
      });
      await expect(usage).toBeVisible();
      await expect(usage).toContainText("Input tokens");
      await expect(usage).toContainText("1,234");
      await expect(usage).toContainText("Output tokens");
      await expect(usage).toContainText("56");
      await expect(
        card.getByText("Factory verification is tracked separately.", {
          exact: false,
        }),
      ).toBeVisible();
      await expect(card.locator("pre")).not.toBeVisible();
      const advanced = card
        .locator("details")
        .filter({ has: page.getByText("Advanced details", { exact: true }) });
      await expect(advanced).not.toHaveAttribute("open");
      await advanced.locator("summary").focus();
      await page.keyboard.press("Enter");
      await expect(advanced).toHaveAttribute("open");
      await expect(
        advanced.getByRole("region", { name: "Recorded preview", exact: true }),
      ).toContainText('"input_tokens":1234');
      await page.keyboard.press("Enter");
      await expect(advanced).not.toHaveAttribute("open");
      await expect(card.locator("pre")).not.toBeVisible();
      expect(contentReads).toBe(0);
      expect(writes).toEqual([]);
      const geometry = await page.evaluate(() => {
        const card = document.querySelector(
          '[aria-label="Archived provider event"]',
        )!;
        const usage = card.querySelector("dl")!;
        const rect = card.getBoundingClientRect();
        return {
          viewport: innerWidth,
          documentWidth: document.documentElement.scrollWidth,
          card: { left: rect.left, right: rect.right },
          visibleJSON: [...card.querySelectorAll("pre")].filter(
            (el) => !el.closest("details:not([open])"),
          ).length,
          usageWidth: usage.getBoundingClientRect().width,
          footerVisible: !!document.querySelector("footer"),
        };
      });
      expect(geometry.documentWidth).toBeLessThanOrEqual(width + 1);
      expect(geometry.card.left).toBeGreaterThanOrEqual(0);
      expect(geometry.card.right).toBeLessThanOrEqual(width + 1);
      expect(geometry.visibleJSON).toBe(0);
      await page.screenshot({
        path: test.info().outputPath(`completed-turn-${width}-${theme}.png`),
      });
      const worker = page
        .getByRole("region", { name: "Archived provider event", exact: true })
        .filter({
          has: page.getByText(
            "Migration preserves existing records. Final verification remains outstanding.",
            { exact: true },
          ),
        });
      await expect(
        worker.getByLabel("Provider-reported worker result", { exact: true }),
      ).toContainText("Failed");
      await expect(worker.getByText("6", { exact: true })).toBeVisible();
      await expect(
        worker.getByText("src/crm/migrations.py", { exact: true }),
      ).toBeVisible();
      await expect(
        worker
          .getByRole("list", { name: "Reported evidence paths", exact: true })
          .getByRole("listitem"),
      ).toHaveCount(2);
      await expect(worker.locator("pre")).not.toBeVisible();
      const unsupported = page
        .getByRole("region", { name: "Archived provider event", exact: true })
        .filter({
          has: page.getByText(/A structured recorded message is retained/),
        });
      await expect(unsupported.locator("pre")).not.toBeVisible();
      await worker.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: test.info().outputPath(`worker-report-${width}-${theme}.png`),
      });
      await writeFile(
        test.info().outputPath("geometry.json"),
        JSON.stringify(geometry, null, 2),
      );
    });
