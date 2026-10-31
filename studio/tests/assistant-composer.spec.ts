/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { assistantChatFixture } from "./assistant-chat-fixture";

const repository = "a".repeat(32);
async function open(
  page: Page,
  fixture: ReturnType<typeof assistantChatFixture>,
) {
  await page.route("**/api/v1/**", async (route) => {
    if ((await mockIdentity(route)) || (await fixture.handle(route))) return;
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({
      json: path.endsWith("/repositories")
        ? [
            {
              id: repository,
              name: "Inventory",
              provider: "git",
              url: "/local/fixture",
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
    .fill("fixture-chat-operator-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product definition");
  const viewport = page.viewportSize();
  if (viewport && (viewport.width < 1100 || viewport.height <= 680))
    await page
      .getByRole("button", { name: "Open design assistant", exact: true })
      .click();
  await expect(
    page.getByLabel("Product design message", { exact: true }),
  ).toBeEnabled();
}

test("a rejected creation preserves the draft and permits an explicit new attempt", async ({
  page,
}) => {
  const fixture = assistantChatFixture(repository, "empty");
  fixture.state.createOutcome = "rejected";
  await open(page, fixture);
  const message = page.getByLabel("Product design message", { exact: true });
  await message.fill("Keep the reservation draft");
  await page.locator(".assistant-send").click();
  await expect(page.getByRole("alert")).toContainText(
    "Choose a supported task",
  );
  await expect(message).toHaveValue("Keep the reservation draft");
  await expect(message).toBeEnabled();
  expect(fixture.state.created).toBe(1);
  expect(fixture.state.sent).toBe(0);
  fixture.state.createOutcome = "completed";
  await page.locator(".assistant-send").click();
  await expect.poll(() => fixture.state.sent).toBe(1);
  expect(fixture.state.created).toBe(2);
});

test("lost creation requires explicit recorded selection without a duplicate create", async ({
  page,
}) => {
  const fixture = assistantChatFixture(repository, "empty");
  fixture.state.createOutcome = "uncertain";
  await open(page, fixture);
  const message = page.getByLabel("Product design message", { exact: true });
  await message.fill("Keep the unconfirmed draft");
  await page.locator(".assistant-send").click();
  await expect(page.locator(".integration-status")).toContainText(
    "new conversation could not be confirmed",
  );
  await expect(message).toHaveValue("Keep the unconfirmed draft");
  await expect(message).toBeDisabled();
  await page
    .locator(".assistant-composer")
    .evaluate((form: HTMLFormElement) => form.requestSubmit());
  await page
    .getByRole("button", { name: "Conversation history", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Refresh record", exact: true })
    .click();
  const selection = page.getByLabel("Recorded conversation", { exact: true });
  await expect(selection).toHaveValue("");
  await expect(message).toBeDisabled();
  expect(fixture.state.created).toBe(1);
  expect(fixture.state.sent).toBe(0);
  await selection.selectOption(fixture.conversation);
  await expect(message).toBeEnabled();
  await page.locator(".assistant-send").click();
  await expect.poll(() => fixture.state.sent).toBe(1);
  expect(fixture.state.created).toBe(1);
});

test("history navigation retains each conversation's uncertain turn until exact readback", async ({
  page,
}) => {
  const fixture = assistantChatFixture(repository),
    other = "c".repeat(32);
  fixture.state.turnOutcome = "uncertain";
  fixture.state.sessions.push({
    ...fixture.state.sessions[0],
    id: other,
    task: "architecture",
  });
  await open(page, fixture);
  const message = page.getByLabel("Product design message", { exact: true });
  await message.fill("An uncertain turn");
  await page.locator(".assistant-send").click();
  await expect(message).toBeDisabled();
  const history = page.getByRole("button", {
    name: "Conversation history",
    exact: true,
  });
  await history.click();
  await page
    .getByLabel("Recorded conversation", { exact: true })
    .selectOption(other);
  await expect(message).toBeEnabled();
  await history.click();
  await page
    .getByLabel("Recorded conversation", { exact: true })
    .selectOption(fixture.conversation);
  await expect(message).toBeDisabled();
  expect(fixture.state.sent).toBe(1);
  fixture.state.turns.push({
    ...fixture.state.turns[0],
    id: "e".repeat(32),
    request_id: fixture.state.turnBodies[0].request_id,
    message: "An uncertain turn",
  });
  await history.click();
  await page
    .getByRole("button", { name: "Refresh record", exact: true })
    .click();
  await expect(message).toBeEnabled();
  expect(fixture.state.sent).toBe(1);
});

test("an attachment action creates only its own session and retains selected file identity", async ({
  page,
}) => {
  const fixture = assistantChatFixture(repository, "empty");
  await open(page, fixture);
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "Conversation files", exact: true }),
  ).toBeVisible();
  expect(fixture.state.created).toBe(1);
  expect(fixture.state.sent).toBe(0);
  await page
    .getByLabel(`Include ${fixture.file.filename} in next message`, {
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Close conversation files", exact: true })
    .click();
  await expect(
    page.locator(".assistant-composer .conversation-file-tray"),
  ).toContainText(fixture.file.filename);
  await page
    .getByLabel("Product design message", { exact: true })
    .fill("Use the attached notes");
  await page.locator(".assistant-send").click();
  await expect.poll(() => fixture.state.sent).toBe(1);
  expect(fixture.state.turnBodies[0].attachment_ids).toEqual([fixture.file.id]);
  expect(fixture.state.created).toBe(1);
});

test("unmount during session creation never posts a late turn", async ({
  page,
}) => {
  const fixture = assistantChatFixture(repository, "empty");
  let release = () => {};
  fixture.state.creationWait = new Promise<void>((resolve) => {
    release = resolve;
  });
  await open(page, fixture);
  await page
    .getByLabel("Product design message", { exact: true })
    .fill("Do not send after unmount");
  await page.locator(".assistant-send").click();
  await expect.poll(() => fixture.state.created).toBe(1);
  await navigate(page, "Connections");
  release();
  await navigate(page, "Product definition");
  await expect(page.locator(".assistant-conversation-context")).toContainText(
    "Product definition",
  );
  expect(fixture.state.created).toBe(1);
  expect(fixture.state.sent).toBe(0);
});

test("composer autosizes and composition Enter never sends a message", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 568 });
  const fixture = assistantChatFixture(repository, "empty");
  await open(page, fixture);
  const message = page.getByLabel("Product design message", { exact: true });
  const before = await message.boundingBox();
  const empty = await message.evaluate((node) => ({
    full: node.scrollHeight,
    client: node.clientHeight,
    overflow: getComputedStyle(node).overflowY,
  }));
  expect(empty.full).toBeLessThanOrEqual(empty.client + 1);
  expect(empty.overflow).toBe("hidden");
  await message.fill(
    "A first line\nA second line\nA third line\nA fourth line",
  );
  await expect
    .poll(async () => (await message.boundingBox())!.height)
    .toBeGreaterThan(before!.height);
  await message.dispatchEvent("keydown", { key: "Enter", isComposing: true });
  expect(fixture.state.created).toBe(0);
  await message.press("Shift+Enter");
  expect(fixture.state.created).toBe(0);
  await message.press("Enter");
  await expect.poll(() => fixture.state.sent).toBe(1);
  expect(fixture.state.created).toBe(1);
});
