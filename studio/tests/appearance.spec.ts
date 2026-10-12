/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";

test("appearance follows the system, persists an explicit choice, and keeps both headers aligned", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  const appearance = page.getByLabel("Appearance", { exact: true });
  await appearance.selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.reload();
  await expect(appearance).toHaveValue("dark");
  await expect(
    page.getByRole("img", { name: "Firefly Forge", exact: true }),
  ).toHaveAttribute("src", /dark/);
  for (const theme of ["light", "dark"]) {
    await appearance.selectOption(theme);
    const brand = await page.locator(".brand").boundingBox();
    const header = await page.locator(".topbar").boundingBox();
    expect(brand && header).toBeTruthy();
    expect(Math.abs(brand!.height - header!.height)).toBeLessThanOrEqual(1);
    expect(Math.abs(brand!.y - header!.y)).toBeLessThanOrEqual(1);
    await expect(
      page.getByRole("figure", { name: "Operational factory" }),
    ).toHaveAttribute("data-theme", theme);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await appearance.selectOption("system");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("theme controls and all navigation screens fit a phone in both modes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    for (const name of [
      "Factory",
      "Connections",
      "New product",
      "Repositories",
    ]) {
      await page
        .getByRole("navigation", { name: "Main navigation" })
        .getByRole("button", { name, exact: false })
        .click();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    }
  }
});
