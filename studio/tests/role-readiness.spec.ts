/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";

const healthyApi = {
  status: "ok",
  service_role: "api",
  admission_ready: true,
  execution_ready: false,
  detail: null,
  execution: { local_enabled: false, remote_worker_state: "unknown" },
};

for (const [name, health, alert] of [
  ["healthy API admission", healthyApi, ""],
  [
    "degraded API",
    { ...healthyApi, status: "degraded", detail: "Inspect retained API work" },
    "Inspect retained API work",
  ],
  [
    "API with a detail",
    { ...healthyApi, detail: "Inspect retained API work" },
    "Inspect retained API work",
  ],
  [
    "unadmitted API",
    { ...healthyApi, admission_ready: false },
    "Execution is unavailable",
  ],
  [
    "combined execution stopped",
    { ...healthyApi, service_role: "combined" },
    "Execution is unavailable",
  ],
  [
    "legacy execution stopped",
    { execution_ready: false },
    "Execution is unavailable",
  ],
  [
    "degraded API without detail",
    { ...healthyApi, status: "degraded" },
    "Execution is unavailable",
  ],
  [
    "API with local execution enabled",
    { ...healthyApi, execution: { local_enabled: true } },
    "Execution is unavailable",
  ],
  [
    "API missing local execution status",
    { ...healthyApi, execution: {} },
    "Execution is unavailable",
  ],
] as const) {
  test(`${name} preserves the role's readiness boundary`, async ({
    page,
  }, testInfo) => {
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      const path = new URL(route.request().url()).pathname;
      await route.fulfill({
        json: path.endsWith("/health")
          ? health
          : path.endsWith("/profiles")
            ? {}
            : [],
      });
    });
    await page.goto("/");
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("owned-role-readiness-browser-fixture");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await expectAuthenticated(page);
    await navigate(page, "Factory");
    if (alert) await expect(page.getByRole("alert")).toContainText(alert);
    else await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.getByText(/remote worker.*ready/i)).toHaveCount(0);
    if (name === "healthy API admission" || name === "degraded API") {
      if (name === "degraded API") {
        await page.setViewportSize({ width: 390, height: 844 });
        await page
          .getByLabel("Appearance", { exact: true })
          .selectOption("dark");
        const bounds = await page.getByRole("alert").boundingBox();
        expect(bounds!.x).toBeGreaterThanOrEqual(0);
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
      }
      await page.screenshot({
        path: testInfo.outputPath("role-readiness.png"),
      });
    }
  });
}
