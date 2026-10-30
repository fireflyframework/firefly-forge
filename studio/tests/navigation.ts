/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
export async function expectAuthenticated(page: Page) {
  const account = page.locator(".sidebar-bottom .identity-trigger");
  await expect(account).toBeAttached();
  await expect(account).toHaveAttribute("aria-label", /^Signed in as /);
}
export async function expectSignedIn(page: Page, name: string) {
  const account = page.getByRole("button", {
    name: `Signed in as ${name}`,
    exact: true,
  });
  let opened = false;
  if (!(await account.isVisible())) {
    const menu = page.getByRole("button", {
      name: "Open navigation",
      exact: true,
    });
    if (await menu.isVisible()) {
      await menu.click();
      opened = true;
    }
  }
  await expect(account).toBeVisible();
  if (opened)
    await page
      .getByRole("button", { name: "Close navigation", exact: true })
      .click();
}
export async function openAccount(page: Page) {
  await expect(page.locator(".app")).toHaveAttribute("aria-busy", "false");
  await expectAuthenticated(page);
  const account = page.locator(".sidebar-bottom .identity-trigger");
  if (await account.isVisible()) await account.click();
  else {
    await page
      .getByRole("button", { name: "Open navigation", exact: true })
      .click();
    await page.locator(".mobile-account .identity-trigger").click();
  }
  await expect(
    page.getByRole("dialog", { name: "Your account", exact: true }),
  ).toBeVisible();
}
export async function navigate(page: Page, name: string | RegExp) {
  await expect(page.locator(".app")).toHaveAttribute("aria-busy", "false");
  if (name === "Project access") {
    await openAccount(page);
    await page
      .getByRole("dialog", { name: "Your account", exact: true })
      .getByRole("button", { name, exact: true })
      .click();
    return;
  }
  if (name === "Connections") {
    const connection = page.getByRole("button", {
      name: "Connect factory",
      exact: true,
    });
    if (!(await page.locator(".sidebar-bottom .identity-trigger").count())) {
      if (!(await connection.isVisible())) await navigate(page, "Factory");
      await connection.click();
      return;
    }
  }
  const menu = page.getByRole("button", {
    name: "Open navigation",
    exact: true,
  });
  if (await menu.isVisible()) await menu.click();
  if (
    typeof name === "string" &&
    [
      "Connections",
      "Registered users",
      "People",
      "Repository workflow",
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
