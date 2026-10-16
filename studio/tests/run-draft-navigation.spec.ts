/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

const a = "a".repeat(32),
  b = "b".repeat(32);
const brief = (page: Page) =>
  page.getByLabel("What should this change accomplish?", { exact: true });
async function connect(page: Page) {
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
async function setup(page: Page) {
  let writes = 0,
    identityFailures = 0,
    failIdentity = false;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (failIdentity && path.includes("/auth/") && !path.endsWith("/logout")) {
      identityFailures++;
      await route.fulfill({
        status: 503,
        json: { detail: "Identity temporarily unavailable" },
      });
      return;
    }
    if (await mockIdentity(route)) return;
    if (route.request().method() !== "GET") writes++;
    let json: unknown = [];
    if (path.endsWith("/repositories"))
      json = [a, b].map((id) => ({
        id,
        name: id === a ? "Product A" : "Product B",
        url: `https://github.com/example/${id}`,
        provider: "github",
        default_branch: "main",
      }));
    if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    if (path.endsWith("/health")) json = { execution_ready: true };
    if (path.endsWith("/skills"))
      json = {
        skills: [
          {
            id: "test-policy",
            name: "Fictional verification policy",
            description: "Run the declared fictional checks",
            instructions: "Use declared checks",
            category: "quality",
            kind: "quality_gate",
            default: false,
          },
        ],
      };
    if (path.endsWith("/policies")) json = { skills: [] };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await connect(page);
  await navigate(page, "Start a run");
  return {
    writes: () => writes,
    identityFailures: () => identityFailures,
    failIdentityRefresh: () => {
      failIdentity = true;
    },
    restoreIdentity: () => {
      failIdentity = false;
    },
  };
}
async function startSource(page: Page, name: string) {
  await navigate(page, "Repositories");
  await page
    .locator(".repository")
    .filter({ has: page.getByRole("heading", { name, exact: true }) })
    .getByRole("button", { name: "Start a run", exact: true })
    .click();
}
async function unloadPrevented(page: Page) {
  return page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
}

test("unfinished run retains raw fields, checks, step and policy choices through page navigation", async ({
  page,
}) => {
  const state = await setup(page);
  const text = "  Keep **these fictional run instructions**\n";
  const commands = '  [["pytest", "-q"]]\n';
  await brief(page).fill(text);
  await page.getByLabel("Source repository", { exact: true }).selectOption(b);
  await page
    .getByLabel("Run purpose", { exact: true })
    .selectOption("maintain");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Backend", { exact: true }).selectOption("larafly");
  await page.getByLabel("Frontend", { exact: true }).selectOption("vue");
  await page
    .getByLabel("Execution agent", { exact: true })
    .selectOption("claude");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Commands to run", { exact: true }).fill(commands);
  await page
    .getByLabel("Independent review provider", { exact: true })
    .selectOption("codex");
  await page.getByLabel("Override project policies for this run").check();
  await page
    .getByLabel("Fictional verification policy", { exact: false })
    .check();
  await navigate(page, "Connections");
  expect(await unloadPrevented(page)).toBe(true);
  await navigate(page, "Start a run");
  await expect(
    page.getByRole("heading", { name: "Set the checks" }),
  ).toBeVisible();
  await expect(page.getByLabel("Commands to run", { exact: true })).toHaveValue(
    commands,
  );
  await expect(
    page.getByLabel("Independent review provider", { exact: true }),
  ).toHaveValue("codex");
  await expect(
    page.getByLabel("Override project policies for this run"),
  ).toBeChecked();
  await expect(
    page.getByLabel("Fictional verification policy", { exact: false }),
  ).toBeChecked();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page.getByLabel("Backend", { exact: true })).toHaveValue(
    "larafly",
  );
  await expect(page.getByLabel("Frontend", { exact: true })).toHaveValue("vue");
  await expect(page.getByLabel("Execution agent", { exact: true })).toHaveValue(
    "claude",
  );
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(brief(page)).toHaveValue(text);
  await expect(page.getByLabel("Run purpose", { exact: true })).toHaveValue(
    "maintain",
  );
  await expect(
    page.getByLabel("Source repository", { exact: true }),
  ).toHaveValue(b);
  expect(state.writes()).toBe(0);
});

test("explicit source launches keep their unfinished run drafts isolated", async ({
  page,
}) => {
  const state = await setup(page);
  await startSource(page, "Product A");
  await brief(page).fill("Retained fictional A instructions");
  await page.getByLabel("Source repository", { exact: true }).selectOption(b);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await startSource(page, "Product B");
  await expect(brief(page)).toHaveValue("");
  await brief(page).fill("Retained fictional B instructions");
  await startSource(page, "Product A");
  await expect(
    page.getByRole("heading", { name: "Choose your foundation" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(brief(page)).toHaveValue("Retained fictional A instructions");
  await expect(
    page.getByLabel("Source repository", { exact: true }),
  ).toHaveValue(a);
  await startSource(page, "Product B");
  await expect(brief(page)).toHaveValue("Retained fictional B instructions");
  expect(state.writes()).toBe(0);
});

test("confirmed sign-out clears unfinished run work despite a failed identity refresh", async ({
  page,
}) => {
  const state = await setup(page);
  await brief(page).fill("Private previous-identity run instructions");
  expect(await unloadPrevented(page)).toBe(true);
  state.failIdentityRefresh();
  await page
    .getByRole("button", { name: "Signed in as Test operator", exact: true })
    .click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect.poll(state.identityFailures).toBeGreaterThan(0);
  await expect.poll(() => unloadPrevented(page)).toBe(false);
  state.restoreIdentity();
  await connect(page);
  await navigate(page, "Start a run");
  await expect(brief(page)).toHaveValue("");
  expect(state.writes()).toBe(0);
});

test("changing the API connection clears the prior connection's unfinished run", async ({
  page,
}) => {
  const state = await setup(page);
  await brief(page).fill("Private first-connection instructions");
  await navigate(page, "Connections");
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  const base = new URL("/second/api/v1", page.url()).href;
  await page.getByLabel("API address", { exact: true }).fill(base);
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  const session = page.waitForResponse(`${base}/auth/session`);
  await page.getByRole("button", { name: "Load server", exact: true }).click();
  await session;
  await expect(
    page.getByRole("button", {
      name: "Signed in as Test operator",
      exact: true,
    }),
  ).toBeVisible();
  await navigate(page, "Start a run");
  await expect(brief(page)).toHaveValue("");
  expect(await unloadPrevented(page)).toBe(false);
  expect(state.writes()).toBe(0);
});
