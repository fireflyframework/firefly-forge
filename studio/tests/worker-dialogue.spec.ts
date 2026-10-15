/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import contract from "./worker-dialogue-contract-fixture.json" with { type: "json" };

const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: null,
  brief: "Inventory decision fixture",
  status: "paused",
  publish: false,
};
function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const row = value as Record<string, unknown>;
  return `{${Object.keys(row)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(row[key])}`)
    .join(",")}}`;
}
function seal(body: Record<string, unknown>) {
  return {
    ...body,
    digest: createHash("sha256").update(canonical(body)).digest("hex"),
  };
}
const feedback = contract.feedback;
async function setup(page: Page, lost = false) {
  const state = {
    posts: 0,
    reads: 0,
    events: [feedback] as Record<string, unknown>[],
    deny: false,
  };
  await page.context().route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/respond")) {
      state.posts++;
      const body = route.request().postDataJSON();
      const markers = await page.evaluate(() =>
        Object.entries(localStorage).filter(([key]) =>
          key.startsWith("forge.worker-reply.v1:"),
        ),
      );
      expect(markers).toHaveLength(1);
      expect(markers[0][1]).not.toContain(body.answer);
      expect(markers[0][1]).toContain(body.id);
      state.events.push(
        seal({
          run_id: run.id,
          sequence: 4,
          task_id: "implement",
          idempotency_key: `worker-response:${body.id}`,
          kind: "worker_response",
          created_at: "2026-10-15T12:02:00+00:00",
          previous_hash: "b".repeat(64),
          payload: { ...body, request_id: "reservation-limit" },
        }),
      );
      if (lost) return route.abort("failed");
      return route.fulfill({ json: state.events[1] });
    }
    if (path.endsWith("/worker-messages")) {
      state.reads++;
      if (state.deny)
        return route.fulfill({
          status: 403,
          json: { detail: "private-denial-canary" },
        });
      return route.fulfill({ json: state.events });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: run.repository_id,
            name: "Inventory",
            url: "https://example.test/inventory.git",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [run]
        : path.endsWith(`/runs/${run.id}`)
          ? run
          : path.endsWith("/health")
            ? { status: "ok", execution_ready: false }
            : [];
    return route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("owned-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Inventory decision fixture/ })
    .click();
  await page.getByRole("tab", { name: "Agent questions", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    }),
  ).toBeVisible();
  return state;
}
test("review, retain one identity, send once and confirm the recorded answer without resuming", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "14 items", exact: true }).click();
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Review your answer", exact: true }),
  ).toBeFocused();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByText(
      "Answer recorded. The run stays paused until you resume it.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  expect(state.reads).toBeGreaterThan(1);
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toHaveCount(0);
});
test("a lost delivery keeps its original identity and reload reconciles with GET only", async ({
  page,
}) => {
  const state = await setup(page, true);
  await page
    .getByLabel("Your answer", { exact: true })
    .fill("Reserve at most 14 items.\nKeep the operation atomic.");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Check original answer", exact: true }),
  ).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: /Inventory decision fixture/ })
    .click();
  await page.getByRole("tab", { name: "Agent questions", exact: true }).click();
  await expect(
    page.getByText(
      "Answer recorded. The run stays paused until you resume it.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});

test("missing readback and permission refusal never create a replacement answer or reflect private errors", async ({
  page,
}) => {
  const state = await setup(page, true);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Check original answer", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Delivery is unconfirmed. Check the original answer; another answer will not be sent automatically.",
      { exact: true },
    ),
  ).toBeVisible();
  state.events = [feedback];
  await page
    .getByRole("button", { name: "Check original answer", exact: true })
    .click();
  await expect(
    page.getByText(
      "No matching answer is currently available. Keep the original request; this does not prove it was not sent.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByLabel("Your answer", { exact: true })).toHaveCount(0);
  state.deny = true;
  await page
    .getByRole("button", { name: "Check original answer", exact: true })
    .click();
  await expect(
    page.getByText(
      "Your current session cannot read this conversation. Sign in or ask the project owner to review your access.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByText("private-denial-canary")).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toBe(1);
});

test("unavailable durable tracking refuses the write before dispatch", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("storage unavailable");
    };
  });
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByText(
      "The answer identity could not be safely retained. Nothing further will be sent from this form; review the original conversation before continuing.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toBe(0);
});

test("two open tabs retain one original answer before either can dispatch", async ({
  page,
}) => {
  const state = await setup(page);
  const second = await page.context().newPage();
  await second.goto("/");
  await second
    .getByRole("button", { name: /Inventory decision fixture/ })
    .click();
  await second
    .getByRole("tab", { name: "Agent questions", exact: true })
    .click();
  for (const tab of [page, second]) {
    await tab.getByLabel("Your answer", { exact: true }).fill("14 items");
    await tab
      .getByRole("button", { name: "Review answer", exact: true })
      .click();
  }
  await Promise.all(
    [page, second].map((tab) =>
      tab.getByRole("button", { name: "Send answer", exact: true }).click(),
    ),
  );
  await expect.poll(() => state.posts).toBe(1);
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toHaveCount(0);
  await expect(
    second.getByRole("button", { name: "Send answer", exact: true }),
  ).toBeDisabled();
  expect(state.posts).toBe(1);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 600 },
  { width: 320, height: 400 },
]) {
  for (const theme of ["light", "dark"]) {
    test(`answer review fits ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page
        .getByLabel("Your answer", { exact: true })
        .fill("Reserve up to **14 items**.\n\nKeep the reservation atomic.");
      await page
        .getByRole("button", { name: "Review answer", exact: true })
        .click();
      const send = page.getByRole("button", {
        name: "Send answer",
        exact: true,
      });
      await send.scrollIntoViewIfNeeded();
      await expect(send).toBeInViewport();
      const box = await send.boundingBox();
      expect(box).not.toBeNull();
      const footer = await page
        .locator(".workspace-main > footer")
        .boundingBox();
      if (box && footer)
        expect(box.y + box.height).toBeLessThanOrEqual(footer.y + 1);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath("answer-review.png"),
      });
    });
  }
}
