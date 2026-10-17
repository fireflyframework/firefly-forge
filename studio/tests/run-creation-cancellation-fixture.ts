/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Controlled run-admission decisions; no native run or provider executes.
import { expect, type BrowserContext, type Page } from "@playwright/test";
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

export async function fixture(context: BrowserContext) {
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
export async function open(page: Page, login = false) {
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
export async function submit(page: Page, brief: string) {
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
export async function markers(page: Page) {
  return page.evaluate(() =>
    Object.entries(localStorage)
      .filter(([key]) => key.startsWith("forge.run-creation.v1:"))
      .map(([, value]) => JSON.parse(value) as { requestId: string }),
  );
}
