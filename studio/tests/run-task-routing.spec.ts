/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { fixture, open } from "./run-creation-cancellation-fixture";

async function prepareRun(page: Page, provider: string) {
  await page
    .getByLabel("What should this change accomplish?", { exact: true })
    .fill("Preserve the configured implementation route.");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  if (provider)
    await page
      .getByLabel("Execution agent", { exact: true })
      .selectOption(provider);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Commands to run", { exact: true })
    .fill('[["pytest", "-q"]]');
  await page.getByRole("button", { name: "Review run", exact: true }).click();
}
for (const provider of ["", "codex", "claude", "bridge", "agentic"]) {
  test(`new run preserves ${provider || "configured task routing"} in its actual request`, async ({
    page,
    context,
  }) => {
    await fixture(context);
    let envelope: { input: Record<string, unknown> } | undefined;
    await page.route("**/run-creation-requests/*", async (route) => {
      envelope = route.request().postDataJSON();
      await route.fulfill({
        status: 503,
        json: { detail: "Controlled delivery unknown" },
      });
    });
    await open(page, true);
    await prepareRun(page, provider);
    await page
      .getByRole("button", { name: "Start building", exact: true })
      .click();
    await expect(
      page.getByText("Controlled delivery unknown", { exact: true }),
    ).toBeVisible();
    expect(envelope?.input.operation).toBe("build");
    if (provider) expect(envelope?.input.agent).toBe(provider);
    else expect(envelope?.input).not.toHaveProperty("agent");
  });
}

test("an explicit provider conflict remains held with the original request", async ({
  page,
  context,
}) => {
  await fixture(context);
  let writes = 0;
  await page.route("**/run-creation-requests/*", async (route) => {
    writes++;
    expect(route.request().postDataJSON().input.agent).toBe("claude");
    await route.fulfill({
      status: 409,
      json: {
        detail:
          "The explicit provider conflicts with the configured task route.",
      },
    });
  });
  await open(page, true);
  await prepareRun(page, "claude");
  await page
    .getByRole("button", { name: "Start building", exact: true })
    .click();
  await expect(
    page.getByText(
      "The explicit provider conflicts with the configured task route.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Check original result", exact: true }),
  ).toBeVisible();
  expect(writes).toBe(1);
});
