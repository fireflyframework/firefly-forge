/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { Page } from "@playwright/test";
export async function navigate(page: Page, name: string | RegExp) {
  const menu = page.getByRole("button", {
    name: "Open navigation",
    exact: true,
  });
  if (await menu.isVisible()) await menu.click();
  if (
    typeof name === "string" &&
    [
      "Connections",
      "Project access",
      "Registered users",
      "People",
      "Repository workflow",
      "Agents & skills",
    ].includes(name)
  ) {
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("button", { name: "Administration", exact: true })
      .click();
    await page
      .locator("main")
      .getByRole("button", {
        name: name === "Registered users" ? "People" : name,
        exact: true,
      })
      .click();
    return;
  }
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", {
      name: name === "Repositories" ? /^Repositories(?: \d+)?$/ : name,
      exact: typeof name === "string",
    })
    .click();
}
