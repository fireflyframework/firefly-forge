/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

async function openProduct(page: Page, contextual = false) {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    const conversation = "b".repeat(32);
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
            {
              id: "f".repeat(32),
              name: contextual ? "Inventory" : "Inventory alternate",
              url: "https://github.com/example/alternate",
              provider: "github",
              default_branch: "main",
            },
          ]
        : path.endsWith("/brand")
          ? null
          : path.endsWith("/product")
            ? contextual
              ? {
                  repository_id: "a".repeat(32),
                  revision: 1,
                  digest: "e".repeat(64),
                  definition: {
                    customer: "Warehouse operator",
                    problem:
                      "Reserve available stock atomically without overselling or accessing stock in unauthorized warehouses.",
                    owners: ["Inventory team"],
                    journey: ["Reserve stock"],
                    user_stories: ["Reserve available stock"],
                    acceptance: ["Reject overselling"],
                    security_requirements: [],
                    nonfunctional_requirements: [],
                    features: [],
                    processes: [],
                  },
                  origin: {
                    kind: "ai_assisted",
                    provider: "bridge",
                    model: "bridge/default",
                    generated_at: "2026-10-12T12:00:00Z",
                    conversation_id: conversation,
                    turn_id: "c".repeat(32),
                    proposal_digest: "e".repeat(64),
                  },
                }
              : null
            : path.endsWith("/assistant/conversations")
              ? [
                  {
                    id: conversation,
                    task: "product",
                    agent: "bridge",
                    created_at: "2026-10-12T12:00:00Z",
                  },
                ]
              : path.endsWith(`/assistant/conversations/${conversation}`)
                ? {
                    id: conversation,
                    task: "product",
                    agent: "bridge",
                    turns: [
                      {
                        id: "c".repeat(32),
                        status: "completed",
                        message: "Review reservation requirements",
                        reply: {
                          message:
                            "Recorded stock reservation evidence. ".repeat(100),
                          questions: [],
                          assumptions: [],
                          citation_ids: [],
                        },
                      },
                    ],
                    journal: { verified: true },
                  }
                : path.endsWith("/attachments")
                  ? [
                      {
                        id: "d".repeat(32),
                        filename: "journey-notes.md",
                        mime_type: "text/markdown",
                        kind: "document",
                        content_bytes: 115,
                        sha256: "e".repeat(64),
                        status: "ready",
                        text_chars: 115,
                        truncated: false,
                        created_at: "2026-10-12T12:00:00Z",
                      },
                    ]
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
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Product design message", { exact: true }),
  ).toBeVisible();
}

for (const width of [1440, 1920]) {
  test(`feature mapping uses the editor width and keeps journey navigation reachable at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await openProduct(page, true);
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .getByRole("button", { name: "Add feature", exact: true })
      .click();
    await page.evaluate(() => document.fonts.ready);
    const geometry = () =>
      page.evaluate(() => {
        const rect = (selector: string) => {
          const r = document.querySelector(selector)!.getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, left: r.left, width: r.width };
        };
        return {
          component: rect(".functional-features"),
          nav: rect(".functional-features nav"),
          form: rect(".functional-features .component-editor-layout > section"),
          given: rect("#feature-behavior-given"),
          fields: rect(".wizard-field-scroll"),
          actions: rect(".wizard-actions"),
          footer: rect(".workspace-main > footer"),
          scrollTop: document.querySelector(".wizard-field-scroll")!.scrollTop,
        };
      });
    const before = await geometry();
    if (width === 1440) {
      expect.soft(before.component.width).toBeLessThan(760);
      expect
        .soft(before.given.width)
        .toBeGreaterThan(before.component.width * 0.9);
      expect.soft(before.nav.bottom).toBeLessThanOrEqual(before.form.top);
    } else {
      expect.soft(before.component.width).toBeGreaterThan(760);
      expect
        .soft(before.form.left)
        .toBeGreaterThan(before.nav.left + before.nav.width);
      expect
        .soft(before.given.width)
        .toBeGreaterThan(before.component.width * 0.65);
    }
    await page
      .getByLabel("Feature name", { exact: true })
      .fill("Reserve stock");
    const fields = await page.locator(".wizard-field-scroll").boundingBox();
    await page.mouse.move(
      fields!.x + fields!.width * 0.8,
      fields!.y + fields!.height * 0.5,
    );
    await page.mouse.wheel(0, 500);
    await expect
      .poll(async () => (await geometry()).scrollTop)
      .toBeGreaterThan(before.scrollTop + 250);
    const after = await geometry();
    expect.soft(after.actions.bottom).toBeLessThan(after.footer.top);
    if (width === 1920) {
      expect.soft(after.nav.top).toBeGreaterThanOrEqual(after.fields.top);
      expect.soft(after.nav.bottom).toBeLessThanOrEqual(after.fields.bottom);
      await expect(
        page.getByRole("button", { name: /1\. Reserve stock/ }),
      ).toBeInViewport();
    }
    await page.getByLabel("Given", { exact: true }).fill("Stock is available");
    await expect(page.getByLabel("Feature name", { exact: true })).toHaveValue(
      "Reserve stock",
    );
    await expect(page.getByLabel("Given", { exact: true })).toHaveValue(
      "Stock is available",
    );
  });
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1280, height: 768 },
]) {
  test(`saved AI context with selected source keeps titles and normal fields readable at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await openProduct(page, true);
    await page.evaluate(() => document.fonts.ready);
    for (const selector of [
      ".product-workbench-heading h1",
      ".product-assistant h2",
    ]) {
      const title = await page.locator(selector).evaluate((element) => ({
        height: element.getBoundingClientRect().height,
        line: Number.parseFloat(getComputedStyle(element).lineHeight),
        icon: element.querySelector("svg")?.getBoundingClientRect().height ?? 0,
      }));
      expect
        .soft(title.height)
        .toBeLessThanOrEqual(Math.max(title.line, title.icon) + 1);
    }
    expect
      .soft(
        await page
          .locator("main")
          .evaluate((element) => element.scrollHeight - element.clientHeight),
      )
      .toBeLessThanOrEqual(1);
    if (viewport.height === 900) {
      const owner = await page
        .getByLabel("Product owners", { exact: true })
        .boundingBox();
      const fields = await page.locator(".wizard-field-scroll").boundingBox();
      expect(owner!.y + owner!.height).toBeLessThanOrEqual(
        fields!.y + fields!.height,
      );
    }
  });
}

for (const width of [1280, 1440, 1920]) {
  test(`shared product header and proportional panels fit the available page at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await openProduct(page);
    const geometry = () =>
      page.evaluate(() => {
        const rect = (selector: string) => {
          const r = document.querySelector(selector)!.getBoundingClientRect();
          return {
            top: r.top,
            bottom: r.bottom,
            left: r.left,
            right: r.right,
            width: r.width,
            height: r.height,
          };
        };
        return {
          main: rect("main"),
          header: rect(".product-workbench-heading"),
          editor: rect(".product-document"),
          assistant: rect(".product-assistant-panel"),
          composer: rect(".assistant-composer"),
          actions: rect(".wizard-actions"),
          footer: rect(".workspace-main>footer"),
          fieldsOverflow: getComputedStyle(
            document.querySelector(".wizard-field-scroll")!,
          ).overflowY,
          mainScroll:
            document.querySelector("main")!.scrollHeight -
            document.querySelector("main")!.clientHeight,
        };
      });
    const g = await geometry();
    expect.soft(g.header.right).toBeGreaterThanOrEqual(g.assistant.right - 1);
    expect.soft(g.assistant.top).toBeCloseTo(g.editor.top, 0);
    expect.soft(g.assistant.bottom).toBeCloseTo(g.editor.bottom, 0);
    expect.soft(g.assistant.width).toBeGreaterThanOrEqual(320);
    expect.soft(g.assistant.width).toBeLessThanOrEqual(520);
    expect
      .soft(g.assistant.width)
      .toBeCloseTo(Math.min(520, Math.max(320, g.header.width * 0.32)), 0);
    expect.soft(g.fieldsOverflow).toBe("auto");
    expect.soft(g.mainScroll).toBeLessThanOrEqual(1);
    expect.soft(g.actions.bottom).toBeLessThanOrEqual(g.main.bottom - 20);
    expect.soft(g.composer.bottom).toBeLessThanOrEqual(g.main.bottom - 20);
    await page.setViewportSize({ width, height: 720 });
    await expect
      .poll(async () => (await geometry()).assistant.height)
      .toBeLessThan(g.assistant.height - 100);
    const resized = await geometry();
    expect(resized.assistant.bottom).toBeCloseTo(resized.editor.bottom, 0);
    expect(resized.actions.bottom).toBeLessThanOrEqual(
      resized.main.bottom - 20,
    );
    expect(resized.composer.bottom).toBeLessThanOrEqual(
      resized.main.bottom - 20,
    );
  });
}

for (const viewport of [
  { width: 1440, height: 600 },
  { width: 390, height: 844 },
  { width: 320, height: 568 },
]) {
  test(`compact product uses natural fields and a mounted modal assistant at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openProduct(page);
    await page
      .getByLabel("Product design message", { exact: true })
      .fill("Retain my unsent reservation question");
    await page
      .getByLabel("Customer or user", { exact: true })
      .fill("Warehouse operator draft");
    await page.getByRole("button", { name: "Files", exact: true }).click();
    await page
      .getByLabel("Include journey-notes.md in next message", { exact: true })
      .check();
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.setViewportSize(viewport);
    const trigger = page.getByRole("button", {
      name: "Open design assistant",
      exact: true,
    });
    await expect(trigger).toBeVisible();
    await expect(page.locator(".wizard-field-scroll")).toHaveCSS(
      "overflow-y",
      "visible",
    );
    await trigger.click();
    const dialog = page.getByRole("dialog", {
      name: "Product design assistant",
      exact: true,
    });
    await expect(dialog).toBeVisible();
    await expect(
      page.getByLabel("Product design message", { exact: true }),
    ).toHaveValue("Retain my unsent reservation question");
    await expect(
      dialog.getByRole("button", { name: "Files (1)", exact: true }),
    ).toBeVisible();
    const composer = await dialog.locator(".assistant-composer").boundingBox();
    expect(composer!.y + composer!.height).toBeLessThanOrEqual(
      viewport.height - 12,
    );
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(viewport.width);
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(
      page.getByRole("complementary", {
        name: "Product design assistant",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Files (1)", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByLabel("Product design message", { exact: true }),
    ).toHaveValue("Retain my unsent reservation question");
    await expect(
      page.getByLabel("Customer or user", { exact: true }),
    ).toHaveValue("Warehouse operator draft");
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(1440);
  });
}

test("a product load warning reduces the panel budget without clipping the assistant", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openProduct(page);
  const panel = page.locator(".product-assistant-panel");
  const before = await panel.boundingBox();
  await page.route(`**/repositories/${"f".repeat(32)}/product`, (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "Product source temporarily unavailable" },
    }),
  );
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption("f".repeat(32));
  await expect(page.locator(".product-definition > .alert")).toContainText(
    "Product source temporarily unavailable",
  );
  await expect
    .poll(async () => (await panel.boundingBox())!.height)
    .toBeLessThan(before!.height - 20);
  const after = await panel.boundingBox();
  const main = await page.locator("main").boundingBox();
  const header = await page.locator(".product-workbench-heading").boundingBox();
  expect(after!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
  expect(after!.y + after!.height).toBeLessThanOrEqual(
    main!.y + main!.height - 20,
  );
  expect(
    await page
      .locator("main")
      .evaluate((element) => element.scrollHeight - element.clientHeight),
  ).toBeLessThanOrEqual(1);
});

for (const theme of ["light", "dark"]) {
  test(`short landscape assistant keeps a readable conversation and reachable composer in ${theme}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openProduct(page, true);
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await page.setViewportSize({ width: 812, height: 375 });
    const trigger = page.getByRole("button", {
      name: "Open design assistant",
      exact: true,
    });
    await expect(trigger).toBeVisible();
    await trigger.click();
    const dialogue = page.getByRole("region", {
      name: "Recorded design dialogue",
      exact: true,
    });
    const reading = await dialogue.evaluate((element) => ({
      height: element.clientHeight,
      line: parseFloat(
        getComputedStyle(element.querySelector(".rich-markdown")!).lineHeight,
      ),
    }));
    expect(reading.height).toBeGreaterThanOrEqual(reading.line * 3);
    const bodyVisible = await dialogue.evaluate((element) => {
      const body = element
        .querySelector(".rich-markdown p")!
        .getBoundingClientRect();
      const viewport = element.getBoundingClientRect();
      const line = parseFloat(
        getComputedStyle(element.querySelector(".rich-markdown")!).lineHeight,
      );
      return body.top + line <= viewport.bottom;
    });
    expect(bodyVisible).toBe(true);
    const message = page.getByLabel("Product design message", { exact: true });
    await message.fill("Retain this fictional stock reservation question");
    await dialogue.hover();
    await page.mouse.wheel(0, 300);
    await expect
      .poll(() => dialogue.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(100);
    const send = page.getByRole("button", {
      name: "Send design message",
      exact: true,
    });
    const reachable = await send.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return (
        box.top >= 0 &&
        box.bottom <= innerHeight &&
        document
          .elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
          ?.closest("button") === element
      );
    });
    expect(reachable).toBe(true);
    await message.focus();
    await page.keyboard.press("Tab");
    await expect(
      page.getByRole("button", { name: "Files", exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(send).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
  });
}
