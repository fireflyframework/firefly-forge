/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

const repository = "a".repeat(32);
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, canonical(item)]),
    );
  return value;
}
async function digest(value: unknown) {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify(canonical(value))),
  );
  return Array.from(new Uint8Array(bytes), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

async function fixture(context: BrowserContext) {
  const originals = new Map<string, Record<string, unknown>>();
  const cancelled = new Map<string, unknown>();
  const calls: Array<{ id: string; method: string; action: string }> = [];
  let delayCancel = false;
  let releaseCancel: (() => void) | undefined;
  await context.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request(),
      path = new URL(request.url()).pathname;
    const match = path.match(
      /\/run-creation-requests\/([a-f0-9]{32})(?:\/(cancel|decision))?$/,
    );
    if (match) {
      const [, id, action = "original"] = match;
      calls.push({ id, method: request.method(), action });
      if (action === "original") {
        originals.set(id, request.postDataJSON());
        await route.fulfill({
          status: 503,
          json: { detail: "Delivery unknown" },
        });
      } else if (action === "cancel") {
        const body = originals.get(id)!;
        const input = body.input as Record<string, unknown>;
        const actor = {
          user_id: "f".repeat(32),
          authentication_method: "local_bootstrap",
        };
        const unsigned = {
          version: 1,
          request_id: id,
          original_actor: actor,
          request_digest: await digest(body),
          repository_id: input.repository_id,
          project_id: input.project_id ?? null,
          component_id: input.component_id ?? null,
          cancelled_by: actor,
          cancelled_at: "2026-10-17T00:00:00+00:00",
        };
        expect(request.postDataJSON()).toEqual({
          version: 1,
          request_digest: unsigned.request_digest,
          repository_id: repository,
          project_id: null,
          component_id: null,
        });
        const decision = {
          version: 1,
          outcome: "cancelled",
          cancellation: { ...unsigned, digest: await digest(unsigned) },
        };
        cancelled.set(id, decision);
        if (delayCancel)
          await new Promise<void>((resolve) => {
            releaseCancel = resolve;
          });
        await route.fulfill({ status: 201, json: decision });
      } else {
        await route.fulfill(
          cancelled.has(id)
            ? { json: cancelled.get(id) }
            : { status: 404, json: { detail: "No terminal decision" } },
        );
      }
      return;
    }
    let json: unknown = [];
    if (path.endsWith("/repositories"))
      json = [
        {
          id: repository,
          name: "Product A",
          url: "https://github.com/example/product",
          provider: "github",
          default_branch: "main",
        },
      ];
    if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    if (path.endsWith("/health")) json = { execution_ready: true };
    if (path.endsWith("/skills") || path.endsWith("/policies"))
      json = { skills: [] };
    await route.fulfill({ json });
  });
  return {
    calls,
    delay: () => {
      delayCancel = true;
    },
    release: () => releaseCancel!(),
  };
}
async function open(page: Page, login = false) {
  await page.goto("/");
  if (login) {
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("test-bootstrap-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await expect(
      page.getByRole("button", {
        name: "Signed in as Test operator",
        exact: true,
      }),
    ).toBeVisible();
  }
  await navigate(page, "Repositories");
  await page
    .locator(".repository")
    .filter({
      has: page.getByRole("heading", { name: "Product A", exact: true }),
    })
    .getByRole("button", { name: "Start a run", exact: true })
    .click();
}
async function submit(page: Page, brief: string) {
  await page
    .getByLabel("What should this change accomplish?", { exact: true })
    .fill(brief);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Commands to run", { exact: true })
    .fill('[["pytest", "-q"]]');
  await page.getByRole("button", { name: "Review run", exact: true }).click();
  await page
    .getByRole("button", { name: "Start building", exact: true })
    .click();
  await expect(
    page.getByText("Delivery unknown", { exact: true }),
  ).toBeVisible();
}
async function markers(page: Page) {
  return page.evaluate(() =>
    Object.entries(localStorage)
      .filter(([key]) => key.startsWith("forge.run-creation.v1:"))
      .map(([, value]) => JSON.parse(value) as { requestId: string }),
  );
}

test("a missing decision holds the original until explicit verified cancellation allows new input", async ({
  page,
  context,
}) => {
  const f = await fixture(context);
  await open(page, true);
  expect(await page.evaluate(() => typeof navigator.locks.request)).toBe(
    "function",
  );
  await submit(page, "Original fictional work");
  const [original] = await markers(page);
  await page
    .getByRole("button", { name: "Check original result", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Send original request again",
      exact: true,
    }),
  ).toBeVisible();
  expect((await markers(page))[0].requestId).toBe(original.requestId);
  expect(f.calls.filter((call) => call.action === "original")).toHaveLength(1);
  await page
    .getByRole("button", { name: "Cancel this request", exact: true })
    .click();
  await expect(
    page.getByText("Request cancelled. You can edit and start again.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(await markers(page)).toEqual([]);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await submit(page, "New fictional work after cancellation");
  const [next] = await markers(page);
  expect(next.requestId).not.toBe(original.requestId);
  expect(f.calls.map((call) => call.action)).toEqual([
    "original",
    "decision",
    "cancel",
    "original",
  ]);
});

test("a delayed old cancellation cannot erase a new request retained in another tab", async ({
  page,
  context,
}) => {
  const f = await fixture(context);
  await open(page, true);
  await submit(page, "First tab original");
  const [original] = await markers(page);
  f.delay();
  await page
    .getByRole("button", { name: "Cancel this request", exact: true })
    .click();
  await expect
    .poll(() => f.calls.filter((call) => call.action === "cancel").length)
    .toBe(1);
  const other = await context.newPage();
  await open(other);
  await expect(
    other.getByRole("button", { name: "Check original result", exact: true }),
  ).toBeVisible();
  await other
    .getByRole("button", { name: "Check original result", exact: true })
    .click();
  await expect(
    other.getByText("Request cancelled. You can edit and start again.", {
      exact: true,
    }),
  ).toBeVisible();
  await submit(other, "Second tab new request");
  const [next] = await markers(other);
  expect(next.requestId).not.toBe(original.requestId);
  f.release();
  await expect(
    page.getByText(
      "This request is resolved. Another request is waiting; check its result before starting again.",
      { exact: true },
    ),
  ).toBeVisible();
  expect((await markers(page))[0].requestId).toBe(next.requestId);
  await other.reload();
  await open(other);
  await other
    .getByRole("button", { name: "Check original result", exact: true })
    .click();
  await expect(
    other.getByRole("button", {
      name: "Send original request again",
      exact: true,
    }),
  ).toBeVisible();
  expect((await markers(other))[0].requestId).toBe(next.requestId);
  expect(f.calls.filter((call) => call.action === "original")).toHaveLength(2);
  await other.close();
});
