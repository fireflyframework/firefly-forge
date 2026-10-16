/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
test("recorded design response seeds an unapproved wizard draft without saving or executing it", async ({
  page,
}) => {
  const conversationId = "c".repeat(32),
    turnId = "d".repeat(32);
  let savedInput: Record<string, unknown> | null = null;
  let sessions: Record<string, unknown>[] = [];
  let sent: Record<string, unknown> | null = null;
  let writes = 0;
  const proposal = {
    customer: "Warehouse supervisors",
    problem: "Reserve inventory safely",
    journey: ["Reserve stock"],
    user_stories: ["As a supervisor I reserve available stock"],
    acceptance: ["Excess reservations are rejected"],
    owners: ["Inventory team"],
    nonfunctional_requirements: [],
    security_requirements: [],
    functional_reviewed: false,
    architecture: null,
    design_review: null,
    features: [],
    processes: [],
  };
  let turn: Record<string, unknown> | null = null;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    let json: unknown = {};
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/repositories"))
      json = [
        {
          id: "a".repeat(32),
          name: "Inventory",
          provider: "github",
          url: "https://github.com/example/inventory",
          default_branch: "main",
        },
      ];
    if (
      path.endsWith("/runs") ||
      path.endsWith("/providers") ||
      path.endsWith("/deployment-targets")
    )
      json = [];
    if (
      path.endsWith("/product") ||
      path.endsWith("/product/conditional") ||
      path.endsWith("/brand")
    ) {
      if (req.method() === "PUT") {
        writes++;
        savedInput = req.postDataJSON().definition;
        json = {
          repository_id: "a".repeat(32),
          revision: 1,
          digest: "b".repeat(64),
          definition: req.postDataJSON().definition,
          created_at: "2026-10-12T00:00:00Z",
        };
      } else json = null;
    }
    if (path.endsWith("/assistant/conversations")) {
      if (req.method() === "POST") {
        expect(req.postDataJSON()).toEqual({ task: "product" });
        const item = {
          id: conversationId,
          repository_id: "a".repeat(32),
          agent: "bridge",
          created_at: "2026-10-12T00:00:00Z",
        };
        sessions = [item];
        json = item;
      } else json = sessions;
    }
    if (path.endsWith("/assistant/conversations/" + conversationId))
      json = {
        ...sessions[0],
        turns: turn ? [turn] : [],
        journal: { verified: true, head: "b".repeat(64), entries: [] },
      };
    if (path.endsWith("/turns")) {
      sent = req.postDataJSON();
      turn = {
        id: turnId,
        request_id: sent?.request_id,
        message: sent?.message,
        status: "completed",
        created_at: "2026-10-12T00:00:00Z",
        reply: {
          message: "Review this bounded proposal.",
          questions: ["Who owns incident response?"],
          assumptions: ["Only authenticated staff reserve stock."],
          proposal,
          citation_ids: ["source-1"],
        },
        detail: "Proposal retained; no definition was changed",
        provenance: {
          kind: "ai_generated",
          provider: "bridge",
          model: "bridge/default",
          generated_at: "2026-10-12T00:00:00Z",
          conversation_id: conversationId,
          turn_id: turnId,
          proposal_digest: "e".repeat(64),
        },
      };
      json = turn;
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product definition");
  await expect(
    page.getByRole("heading", {
      name: "AI design assistant",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "New conversation", exact: true })
    .click();
  await page
    .getByLabel("Product design message")
    .fill("Help define our reservation workflow");
  await page
    .getByRole("button", { name: "Send design message", exact: true })
    .click();
  await expect(
    page.getByText("Who owns incident response?", { exact: true }),
  ).toBeVisible();
  expect(sent?.request_id).toMatch(/^[a-f0-9]{32}$/);
  await page
    .getByRole("button", { name: "Use proposal as wizard draft", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Apply unreviewed draft", exact: true })
    .click();
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue(proposal.customer);
  await expect(
    page.getByText("AI-assisted draft · review needed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "AI design assistant",
      exact: true,
    }),
  ).toBeVisible();

  await page
    .getByLabel("Customer or user", { exact: true })
    .fill("Edited supervisors");
  await page
    .getByRole("button", { name: "Use proposal as wizard draft", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Apply unreviewed draft", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Keep current draft", exact: true })
    .click();
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Edited supervisors");
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole("button", { name: "Open design assistant", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Product design assistant" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Open design assistant", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Edited supervisors");
  await page.setViewportSize({ width: 1440, height: 768 });
  await page
    .locator(".workspace-main>main")
    .evaluate((element) => (element.scrollTop = 0));
  const heading = await page
    .getByRole("heading", { name: "Product definition", exact: true })
    .boundingBox();
  const shellHeader = await page
    .locator(".workspace-main>header")
    .boundingBox();
  expect(heading!.y).toBeGreaterThanOrEqual(
    shellHeader!.y + shellHeader!.height,
  );
  for (const name of [
    "Customer or user",
    "Product owners",
    "Problem to solve",
  ]) {
    const input = page.getByLabel(name, { exact: true });
    await input.focus();
    const geometry = await input.evaluate((element) => {
      const field = element.getBoundingClientRect(),
        scroll = element
          .closest(".wizard-field-scroll")!
          .getBoundingClientRect();
      return {
        top: field.top,
        bottom: field.bottom,
        scrollTop: scroll.top,
        scrollBottom: scroll.bottom,
      };
    });
    expect(geometry.top).toBeGreaterThanOrEqual(geometry.scrollTop - 1);
    expect(geometry.bottom).toBeLessThanOrEqual(geometry.scrollBottom + 1);
  }
  const panel = await page
    .getByRole("complementary", { name: "Product design assistant" })
    .boundingBox();
  const sendButton = await page
    .getByRole("button", { name: "Send design message", exact: true })
    .boundingBox();
  expect(sendButton!.y + sendButton!.height).toBeLessThanOrEqual(
    panel!.y + panel!.height - 8,
  );

  expect(
    (await page.locator(".assistant-turns").boundingBox())!.height,
  ).toBeGreaterThanOrEqual(100);
  expect(writes).toBe(0);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByText("Functional definition pending", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Add feature", exact: true }).click();
  await page
    .getByLabel("Feature name", { exact: true })
    .fill("Reserve inventory");
  await page
    .getByLabel("Excess reservations are rejected", { exact: true })
    .check();
  await page
    .getByLabel("Given", { exact: true })
    .fill("Authenticated operator and available stock");
  await page
    .getByLabel("When", { exact: true })
    .fill("A valid quantity is reserved");
  await page
    .getByLabel("Then", { exact: true })
    .fill("Stock is reduced atomically");
  await page
    .getByLabel("Failure and exception paths", { exact: true })
    .fill("Excess quantity returns409 without mutation");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  const functionalReview = page.getByLabel(
    "I reviewed the mapped behaviors, failure paths and acceptance links.",
    { exact: true },
  );
  await expect(functionalReview).toBeEnabled();
  await expect(functionalReview).not.toBeChecked();
  await functionalReview.check();
  await page
    .getByRole("button", { name: /Save (?:draft|reviewed) definition/ })
    .click();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  expect(writes).toBe(1);
  expect(savedInput?.ai_origin).toEqual({
    conversation_id: conversationId,
    turn_id: turnId,
    proposal_digest: "e".repeat(64),
  });
});

test("new conversation task selection leaves recorded dialogue and edited draft unchanged", async ({
  page,
}) => {
  const repository = "a".repeat(32),
    previous = "b".repeat(32),
    next = "c".repeat(32),
    writes: unknown[] = [];
  const record = (id: string, task: string) => ({
    id,
    repository_id: repository,
    agent: "agentic",
    task,
    created_at: "2026-10-12T10:00:00Z",
    model_selection: {
      profile_id: "product-team",
      profile_revision: 3,
      provider: "agentic",
      task,
      execution_config: { model: "recorded-text-model" },
    },
    turns: [],
    journal: { verified: true, head: "d".repeat(64), entries: [] },
  });
  let sessions = [record(previous, "product")];
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request(),
      path = new URL(request.url()).pathname;
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
    if (path.endsWith("/assistant/conversations")) {
      if (request.method() === "POST") {
        const body = request.postDataJSON();
        writes.push(body);
        const value = record(next, body.task);
        sessions = [value, ...sessions];
        json = value;
      } else json = sessions;
    }
    if (path.endsWith(`/assistant/conversations/${previous}`))
      json = record(previous, "product");
    if (path.endsWith(`/assistant/conversations/${next}`))
      json = record(next, "functions");
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
    .getByLabel("Customer or user", { exact: true })
    .fill("Edited warehouse supervisors");
  await expect(page.locator(".assistant-conversation-context")).toContainText(
    "Product definition",
  );
  await expect(page.locator(".assistant-conversation-context")).toContainText(
    "recorded-text-model",
  );
  await page
    .getByLabel("Task for next conversation", { exact: true })
    .selectOption("functions");
  await expect(page.locator(".assistant-conversation-context")).toContainText(
    "Product definition",
  );
  expect(writes).toEqual([]);
  await page
    .getByRole("button", { name: "New conversation", exact: true })
    .click();
  await expect(page.locator(".assistant-conversation-context")).toContainText(
    "Features and journeys",
  );
  expect(writes).toEqual([{ task: "functions" }]);
  await page
    .getByLabel("Task for next conversation", { exact: true })
    .selectOption("architecture");
  await page.getByText("History", { exact: true }).click();
  await page
    .getByLabel("Recorded conversation", { exact: true })
    .selectOption(previous);
  await expect(page.locator(".assistant-conversation-context")).toContainText(
    "Product definition",
  );
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Edited warehouse supervisors");
  expect(writes).toHaveLength(1);
});
