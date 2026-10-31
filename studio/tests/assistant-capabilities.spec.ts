/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Route } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
test("image support is read for the selected frozen conversation and late replies cannot overwrite it", async ({
  page,
}) => {
  const repository = "a".repeat(32),
    first = "b".repeat(32),
    second = "c".repeat(32);
  let pending: Route | undefined,
    modelCalls = 0;
  const image = {
    id: "d".repeat(32),
    request_id: "e".repeat(32),
    filename: "screen.png",
    mime_type: "image/png",
    kind: "image",
    content_bytes: 100,
    sha256: "f".repeat(64),
    status: "ready",
    text_chars: 0,
    truncated: false,
    created_at: "2026-10-12T12:00:00Z",
  };
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
      json = [first, second].map((id) => ({
        id,
        repository_id: repository,
        agent: "bridge",
        created_at: "2026-10-12T12:00:00Z",
      }));
    if (
      path.endsWith(`/assistant/conversations/${first}`) ||
      path.endsWith(`/assistant/conversations/${second}`)
    )
      json = {
        id: path.split("/").at(-1),
        repository_id: repository,
        agent: "bridge",
        turns: [],
        journal: { verified: true },
      };
    if (path.endsWith(`/${first}/capabilities`)) {
      pending = route;
      return;
    }
    if (path.endsWith(`/${second}/capabilities`))
      json = { image_input: { status: "unverified" } };
    if (path.endsWith("/attachments")) json = [image];
    if (path.endsWith("/turns")) modelCalls++;
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
  await expect.poll(() => Boolean(pending)).toBe(true);
  await page
    .getByRole("button", { name: "Conversation history", exact: true })
    .click();
  await page
    .getByLabel("Recorded conversation", { exact: true })
    .selectOption(second);
  await pending!
    .fulfill({ json: { image_input: { status: "verified" } } })
    .catch(() => undefined);
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await page
    .getByLabel("Include screen.png in next message", { exact: true })
    .check();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.locator(".assistant-file-guidance")).toContainText(
    "Image input is not verified for this conversation",
  );
  await expect(page.locator(".assistant-file-guidance")).not.toContainText(
    "Image input is verified",
  );
  expect(modelCalls).toBe(0);
});

test("reading an AI source preserves the owner's manual scroll across status polling", async ({
  page,
}) => {
  const repository = "a".repeat(32),
    conversation = "b".repeat(32),
    source = "c".repeat(32);
  let reads = 0;
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
    if (path.endsWith("/brand")) json = null;
    if (path.endsWith("/product"))
      json = {
        repository_id: repository,
        revision: 1,
        digest: "d".repeat(64),
        definition: {
          customer: "Operator",
          problem: "Reserve inventory",
          journey: ["Reserve stock"],
          user_stories: ["Reserve available stock"],
          acceptance: ["Reject overselling"],
          owners: ["Inventory team"],
          security_requirements: [],
          nonfunctional_requirements: [],
          features: [],
          processes: [],
        },
        origin: {
          kind: "ai_assisted",
          provider: "bridge",
          model: "Recorded model",
          generated_at: "2026-10-12T12:00:00Z",
          conversation_id: conversation,
          turn_id: source,
          proposal_digest: "e".repeat(64),
          edited: false,
        },
      };
    if (path.endsWith("/assistant/conversations"))
      json = [
        {
          id: conversation,
          repository_id: repository,
          agent: "bridge",
          created_at: "2026-10-12T12:00:00Z",
        },
      ];
    if (path.endsWith(`/assistant/conversations/${conversation}`)) {
      reads++;
      json = {
        id: conversation,
        repository_id: repository,
        agent: "bridge",
        journal: { verified: true },
        turns: [source, "f".repeat(32)].map((id, index) => ({
          id,
          request_id: id,
          status: "completed",
          message: "Describe the product",
          created_at: "2026-10-12T12:00:00Z",
          reply: {
            message: index
              ? "Later response"
              : `Poll receipt ${reads}\n\n` +
                "The owner reviews the reservation behavior.\n\n".repeat(40),
            questions: [],
            assumptions: [],
            citation_ids: [],
            proposal: null,
          },
        })),
      };
    }
    if (path.endsWith("/capabilities"))
      json = { image_input: { status: "unverified" } };
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
  await page
    .getByRole("button", { name: "View AI source", exact: true })
    .click();
  const scroll = page.locator(".assistant-turns");
  await expect(page.locator(`#assistant-turn-${source}`)).toContainText(
    "Poll receipt",
  );
  await scroll.evaluate((element) => (element.scrollTop = 80));
  const before = reads;
  await expect.poll(() => reads).toBeGreaterThan(before);
  await expect(page.locator(`#assistant-turn-${source}`)).toContainText(
    `Poll receipt ${reads}`,
  );
  expect(await scroll.evaluate((element) => element.scrollTop)).toBe(80);
});
