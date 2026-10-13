/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

test("collapsing the assistant releases editor space without losing the chat draft and restores keyboard focus", async ({
  page,
}) => {
  const repository = "a".repeat(32),
    conversation = "b".repeat(32);
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    let json: unknown = [];
    if (path.endsWith("/repositories"))
      json = [
        {
          id: repository,
          name: "Inventory",
          url: "https://github.com/example/inventory",
          provider: "github",
          default_branch: "main",
        },
      ];
    if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    if (path.endsWith("/assistant/conversations"))
      json = [
        {
          id: conversation,
          task: "product",
          agent: "bridge",
          created_at: "2026-10-12T12:00:00Z",
        },
      ];
    if (path.endsWith(`/assistant/conversations/${conversation}`))
      json = {
        id: conversation,
        task: "product",
        agent: "bridge",
        turns: [],
        journal: { verified: true },
      };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product definition");
  await expect(
    page.getByRole("list", { name: "Setup progress" }).getByRole("button"),
  ).toHaveText([
    "1Context",
    "2Journeys",
    "3Features",
    "4Integrations",
    "5Review",
  ]);
  await page
    .getByLabel("Customer or user", { exact: true })
    .fill("Warehouse operator");
  await page
    .getByLabel("Product design message", { exact: true })
    .fill("Keep my proposed stock reservation journey");
  const before = await page.locator(".product-document").boundingBox();
  await page.setViewportSize({ width: 1280, height: 768 });
  const progressWidth = await page
    .getByRole("list", { name: "Setup progress", exact: true })
    .evaluate((element) => ({
      scroll: element.scrollWidth,
      visible: element.clientWidth,
    }));
  expect(progressWidth.scroll).toBeLessThanOrEqual(progressWidth.visible);
  const main = page.locator("main");
  const panel = page.locator("dialog.product-assistant-panel");
  const fields = page.locator(".wizard-field-scroll");
  for (let owner = 0; owner < 6; owner++)
    await page.getByRole("button", { name: "Add owner", exact: true }).click();
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 1280, height: 768 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(panel).toHaveCSS("position", "static");
    await fields.hover();
    await page.mouse.wheel(0, 800);
    await expect
      .poll(() => fields.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    expect(await main.evaluate((element) => element.scrollTop)).toBe(0);
    const bounds = await main.boundingBox();
    for (const control of [
      page.getByRole("heading", { name: "AI design assistant", exact: true }),
      page.getByRole("button", { name: "New conversation", exact: true }),
      page.getByRole("button", { name: "Close design assistant", exact: true }),
      panel.locator(".assistant-composer"),
      page.getByRole("button", { name: "Continue", exact: true }),
    ]) {
      const box = await control.boundingBox();
      expect
        .soft(box!.y, `${viewport.width}: control above main`)
        .toBeGreaterThanOrEqual(bounds!.y);
      expect
        .soft(box!.y + box!.height, `${viewport.width}: control below main`)
        .toBeLessThanOrEqual(bounds!.y + bounds!.height);
    }
    await page.mouse.wheel(0, -800);
    await expect
      .poll(() => fields.evaluate((element) => element.scrollTop))
      .toBe(0);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page
    .getByRole("button", { name: "Close design assistant", exact: true })
    .click();
  await expect(
    page.getByRole("complementary", { name: "Product design assistant" }),
  ).toBeHidden();
  const after = await page.locator(".product-document").boundingBox();
  expect(after!.width).toBeGreaterThan(before!.width + 300);
  await expect(
    page.getByRole("button", { name: "Open design assistant", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    page.getByLabel("Product design message", { exact: true }),
  ).toHaveValue("Keep my proposed stock reservation journey");
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Warehouse operator");
  await page
    .getByRole("button", { name: "Close design assistant", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Ask about this step", exact: true })
    .click();
  const contextReview = page.getByRole("dialog", {
    name: "Review step context",
    exact: true,
  });
  await expect(contextReview).toContainText("Warehouse operator");
  await expect(contextReview).toContainText(
    "Keep my proposed stock reservation journey",
  );
  await contextReview
    .getByRole("button", { name: "Append context", exact: true })
    .click();
  await expect(
    page.getByLabel("Product design message", { exact: true }),
  ).toHaveValue(
    /Keep my proposed stock reservation journey[\s\S]*Current editor content/,
  );
  await page
    .getByRole("button", { name: "Close design assistant", exact: true })
    .click();
  await page.reload();
  await navigate(page, "Product definition");
  await expect(
    page.getByRole("complementary", { name: "Product design assistant" }),
  ).toBeHidden();
  await page.setViewportSize({ width: 320, height: 568 });
  const open = page.getByRole("button", {
    name: "Open design assistant",
    exact: true,
  });
  await open.click();
  await expect(
    page.getByRole("dialog", { name: "Product design assistant", exact: true }),
  ).toBeVisible();
  const assistantHeading = await page
    .getByRole("heading", { name: "AI design assistant", exact: true })
    .evaluate((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return range.getBoundingClientRect().right;
    });
  const newConversation = await page
    .getByRole("button", { name: "New conversation", exact: true })
    .boundingBox();
  expect(assistantHeading).toBeLessThanOrEqual(newConversation!.x - 4);
  await page.keyboard.press("Escape");
  await expect(open).toBeFocused();
  await expect(
    page.getByRole("dialog", { name: "Product design assistant", exact: true }),
  ).toBeHidden();
});

for (const viewport of [
  { width: 1440, height: 768 },
  { width: 320, height: 568 },
]) {
  test(`product fields stay reachable with dedicated actions at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      const path = new URL(route.request().url()).pathname;
      await route.fulfill({
        json: path.endsWith("/repositories")
          ? [
              {
                id: "a".repeat(32),
                name: "Inventory",
                url: "https://github.com/example/inventory",
                provider: "github",
                default_branch: "main",
              },
            ]
          : path.endsWith("/product") || path.endsWith("/brand")
            ? null
            : [],
      });
    });
    await page.goto("/");
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("test-bootstrap-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await navigate(page, "Product definition");
    if (viewport.width > 1100)
      await page
        .getByRole("button", { name: "Close design assistant", exact: true })
        .click();
    await expect(
      page.getByLabel("Customer or user", { exact: true }),
    ).toBeVisible();
    const bounds = await page.evaluate(() => {
      const rect = (selector: string) => {
        const r = document.querySelector(selector)!.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom };
      };
      return {
        title: rect(".product-workbench-heading"),
        main: rect(".workspace-main>main"),
        body: rect(".wizard-field-scroll"),
        customer: rect("#product-customer"),
        actions: rect(".wizard-actions"),
        footer: rect(".workspace-main>footer"),
        problemLabel: rect('label[for="product-problem"]'),
        problem: rect("#product-problem"),
        owner: rect("#product-owners-0"),
        overflow: getComputedStyle(
          document.querySelector(".wizard-field-scroll")!,
        ).overflow,
      };
    });
    expect(bounds.title.top).toBeGreaterThanOrEqual(bounds.main.top);
    if (viewport.width > 1100) {
      expect(bounds.overflow).toBe("auto");
      expect(bounds.body.bottom).toBeLessThanOrEqual(bounds.actions.top);
      const owner = page.getByLabel("Product owners", { exact: true });
      await owner.scrollIntoViewIfNeeded();
      const ownerBounds = await owner.boundingBox();
      const fieldBounds = await page
        .locator(".wizard-field-scroll")
        .boundingBox();
      expect(ownerBounds!.y).toBeGreaterThanOrEqual(fieldBounds!.y);
      expect(ownerBounds!.y + ownerBounds!.height).toBeLessThanOrEqual(
        fieldBounds!.y + fieldBounds!.height,
      );
    } else {
      expect(bounds.overflow).toBe("visible");
      expect(bounds.actions.top).toBeGreaterThanOrEqual(bounds.problem.bottom);
      expect(bounds.actions.top).toBeGreaterThanOrEqual(bounds.owner.bottom);
    }
    await page
      .getByRole("button", { name: "Continue", exact: true })
      .scrollIntoViewIfNeeded();
    const action = await page.locator(".wizard-actions").boundingBox();
    const footer = await page.locator(".workspace-main>footer").boundingBox();
    expect(action!.y + action!.height).toBeLessThanOrEqual(footer!.y);
    await page.getByLabel("Problem to solve", { exact: true }).focus();
    const focused = await page
      .getByLabel("Problem to solve", { exact: true })
      .boundingBox();
    const scroller = await page.locator(".workspace-main>main").boundingBox();
    expect(focused!.y + focused!.height).toBeLessThanOrEqual(
      scroller!.y + scroller!.height,
    );
    await page
      .getByLabel("Customer or user", { exact: true })
      .fill("Warehouse operator");
    await page
      .getByLabel("Problem to solve", { exact: true })
      .fill("Reserve inventory safely.");
    await page
      .getByLabel("Product owners", { exact: true })
      .fill("Inventory team");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .getByLabel("Customer journey", { exact: true })
      .fill("Reserve available stock");
    const statement = page.getByLabel("User stories", { exact: true });
    await statement.fill(
      "As an authorized warehouse operator, I want to reserve available stock so that my reservation is persisted and available stock is decremented atomically.",
    );
    const statementSize = await statement.evaluate((element) => ({
      height: element.clientHeight,
      scrollHeight: element.scrollHeight,
    }));
    expect(statementSize.scrollHeight).toBeLessThanOrEqual(
      statementSize.height + 2,
    );
  });
}

test("duplicate product names can be selected by distinct safe source labels", async ({
  page,
}) => {
  const first = "a".repeat(32),
    second = "b".repeat(32);
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    const repositories = [
      {
        id: first,
        name: "Inventory",
        url: "https://alice:private@example.com/team/api?key=private#private",
        provider: "github",
        default_branch: "main",
      },
      {
        id: second,
        name: "Inventory",
        url: "git@example.com:team/web.git",
        provider: "git",
        default_branch: "develop",
      },
    ];
    await route.fulfill({
      json: path.endsWith("/repositories")
        ? repositories
        : path.endsWith("/product") || path.endsWith("/brand")
          ? null
          : [],
    });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product definition");
  const select = page.getByLabel("Product project", { exact: true });
  await expect(select.getByRole("option")).toHaveText([
    "Inventory · example.com/team/api · github · main",
    "Inventory · example.com/team/web · git · develop",
  ]);
  await select.selectOption({
    label: "Inventory · example.com/team/web · git · develop",
  });
  await expect(select).toHaveValue(second);
  await expect(select).not.toContainText("private");
  await expect(select).not.toContainText("alice");
});
