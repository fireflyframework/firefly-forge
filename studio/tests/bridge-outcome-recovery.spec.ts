/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

// Controlled API responses verify presentation, not a live Bridge execution.
for (const width of [1440, 390]) {
  for (const theme of ["light", "dark"]) {
    test(`unknown Bridge outcome holds replay actions at ${width}px in ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      const writes: string[] = [];
      const run = {
        id: "bridge-unknown-fixture",
        repository_id: "bridge-repository-fixture",
        brief: "Inspect an unconfirmed Bridge result",
        status: "needs_reconciliation",
        agent: "bridge",
        operation: "build",
        pause: {
          kind: "provider_outcome_unknown",
          automatic: false,
          message: "The original request has no confirmed result.",
        },
      };
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const req = route.request();
        const path = new URL(req.url()).pathname;
        if (req.method() !== "GET") {
          writes.push(path);
          return route.fulfill({ status: 409, json: { detail: "Held" } });
        }
        const body = path.endsWith("/repositories")
          ? [
              {
                id: run.repository_id,
                name: "Bridge outcome fixture",
                url: "/fictional/bridge-outcome",
                provider: "git",
                default_branch: "main",
              },
            ]
          : path.endsWith("/providers") || path.endsWith("/deployments")
            ? []
            : path.endsWith("/runs")
              ? [run]
              : path.endsWith("/plan")
                ? { revision: 1, tasks: [] }
                : path.endsWith("/journal")
                  ? { valid: true, entries: [] }
                  : run;
        return route.fulfill({ json: body });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fictional-ui-only-token");
      await page.getByRole("button", { name: "Save connection" }).click();
      await expect(
        page.getByRole("button", { name: "API connected" }),
      ).toBeVisible();
      await navigate(page, "Factory");
      await page.getByRole("button", { name: "Inspect run" }).click();
      const panel = page.getByRole("region", {
        name: "Review the original Bridge execution",
      });
      await expect(panel).toBeVisible();
      await panel.scrollIntoViewIfNeeded();
      await expect(panel.getByRole("heading")).toHaveText(
        "Execution result needs review",
      );
      await expect(panel.getByRole("button")).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Resume run" }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Hand off run" }),
      ).toHaveCount(0);
      const overflow = await panel.evaluate(
        (node) => node.scrollWidth > node.clientWidth + 1,
      );
      expect(overflow).toBe(false);
      expect(writes).toEqual([]);
    });
  }
}
