/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page, type Request } from "@playwright/test";
import type { ProductSnapshot } from "../src/api";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

const a = "a".repeat(32),
  b = "b".repeat(32);
const product = (repository_id: string, revision = 1): ProductSnapshot => ({
  repository_id,
  revision,
  digest: String(revision).repeat(64),
  created_at: "2026-10-16T00:00:00Z",
  origin: { kind: "manual" },
  definition: {
    customer: repository_id === a ? "Saved A" : "Saved B",
    problem: "Manage fictional stock",
    owners: ["Product owner"],
    journey: ["Review stock"],
    user_stories: ["An operator sees stock"],
    acceptance: ["Stock is visible"],
    security_requirements: [],
    nonfunctional_requirements: [],
    architecture: null,
    design_review: null,
  },
});
async function setup(page: Page) {
  let revision = 1,
    writes = 0,
    identityUnavailable = false,
    identityFailures = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      identityUnavailable &&
      path.includes("/auth/") &&
      !path.endsWith("/logout")
    ) {
      identityFailures++;
      await route.fulfill({
        status: 503,
        json: { detail: "Identity temporarily unavailable" },
      });
      return;
    }
    if (await mockIdentity(route)) return;
    let json: unknown = [];
    if (path.endsWith("/repositories"))
      json = [a, b].map((id, index) => ({
        id,
        name: index ? "Product B" : "Product A",
        url: `https://github.com/example/product-${index}`,
        provider: "github",
        default_branch: "main",
      }));
    if (path.endsWith("/product")) {
      json = product(path.includes(a) ? a : b, revision);
      if (route.request().method() === "PUT") {
        writes++;
        json = {
          ...product(path.includes(a) ? a : b, ++revision),
          definition: route.request().postDataJSON(),
        };
      }
    }
    if (path.endsWith("/design/preview")) {
      const svg =
        '<svg xmlns="http://www.w3.org/2000/svg" width="390" height="844"><title>Fictional stock reference</title></svg>';
      json = {
        digest: "c".repeat(64),
        definition_digest: "1".repeat(64),
        brand_digest: null,
        method: "wireframe",
        font_family: "Manrope",
        coverage: { journeys: 1, screen_count: 1 },
        screens: [
          {
            id: "stock",
            title: "Review stock",
            desktop_svg: svg,
            desktop_dark_svg: svg,
            mobile_svg: svg,
            mobile_dark_svg: svg,
            interaction_contract: {
              journey: "Review stock",
              acceptance_ids: ["acceptance-1"],
              primary_action: "Read stock",
              states: ["default"],
            },
          },
        ],
      };
    }
    if (path.endsWith("/brand")) json = null;
    await route.fulfill({ json });
  });
  await page.goto("/");
  await connect(page);
  await navigate(page, "Product definition");
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Saved A");
  return {
    changeRevision: () => {
      revision++;
    },
    writes: () => writes,
    identityFailures: () => identityFailures,
    failIdentityRefresh: () => {
      identityUnavailable = true;
    },
    restoreIdentity: () => {
      identityUnavailable = false;
    },
  };
}
async function connect(page: Page) {
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
}
const customer = (page: Page) =>
  page.getByLabel("Customer or user", { exact: true });
const select = (page: Page, id: string) =>
  page.getByLabel("Product project", { exact: true }).selectOption(id);
async function unloadPrevented(page: Page) {
  return page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
}

test("raw edits and steps survive page navigation and remain isolated between products", async ({
  page,
}) => {
  const state = await setup(page);
  await customer(page).fill("Human A");
  await page
    .getByLabel("Problem to solve", { exact: true })
    .fill("  Keep **these edits**\n");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "What does the customer need to do?" }),
  ).toBeVisible();
  await select(page, b);
  await expect(customer(page)).toHaveValue("Saved B");
  await customer(page).fill("Human B");
  await navigate(page, "Connections");
  expect(await unloadPrevented(page)).toBe(true);
  await navigate(page, "Product definition");
  await expect(page.getByLabel("Product project", { exact: true })).toHaveValue(
    b,
  );
  await expect(customer(page)).toHaveValue("Human B");
  await select(page, a);
  await expect(
    page.getByRole("heading", { name: "What does the customer need to do?" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(customer(page)).toHaveValue("Human A");
  await expect(
    page.getByLabel("Problem to solve", { exact: true }),
  ).toHaveValue("  Keep **these edits**\n");
  expect(state.writes()).toBe(0);
});

test("discard requires confirmation and clears only the selected product", async ({
  page,
}) => {
  const state = await setup(page);
  await customer(page).fill("Keep A");
  await select(page, b);
  await expect(customer(page)).toHaveValue("Saved B");
  await customer(page).fill("Discard B");
  await page
    .getByRole("button", { name: "Discard edits", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Discard product edits",
    exact: true,
  });
  await expect(dialog).toContainText("Product B");
  await page.keyboard.press("Escape");
  await expect(customer(page)).toHaveValue("Discard B");
  await page
    .getByRole("button", { name: "Discard edits", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Discard this draft", exact: true })
    .click();
  await expect(customer(page)).toHaveValue("Saved B");
  await select(page, a);
  await expect(customer(page)).toHaveValue("Keep A");
  expect(await unloadPrevented(page)).toBe(true);
  expect(state.writes()).toBe(0);
});

test("a changed observed server revision holds saving and preserves the local draft", async ({
  page,
}) => {
  const state = await setup(page);
  await customer(page).fill("Retained A");
  await navigate(page, "Connections");
  state.changeRevision();
  await navigate(page, "Product definition");
  await expect(customer(page)).toHaveValue("Retained A");
  await expect(page.getByRole("alert")).toContainText(
    "saved definition changed",
  );
  await expect(
    page.getByText("Editing saved revision 1", { exact: true }),
  ).toBeVisible();
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Save draft definition", exact: true }),
  ).toBeDisabled();
  expect(state.writes()).toBe(0);
});

test("signing out clears the prior identity's drafts and reload warning", async ({
  page,
}) => {
  await setup(page);
  await customer(page).fill("Previous session private edit");
  expect(await unloadPrevented(page)).toBe(true);
  await page
    .getByRole("button", { name: "Signed in as Test operator", exact: true })
    .click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByText("Connect your factory first", { exact: true }),
  ).toBeVisible();
  expect(await unloadPrevented(page)).toBe(false);
  await connect(page);
  await navigate(page, "Product definition");
  await expect(customer(page)).toHaveValue("Saved A");
});

for (const view of [
  { width: 1440, height: 900, theme: "light" },
  { width: 1440, height: 900, theme: "dark" },
  { width: 390, height: 844, theme: "dark" },
  { width: 320, height: 400, theme: "dark" },
] as const)
  test(`draft recovery controls ${view.width}x${view.height} ${view.theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize(view);
    await setup(page);
    await page
      .getByRole("combobox", { name: "Appearance", exact: true })
      .selectOption(view.theme);
    await customer(page).fill("Retained fictional product");
    await page
      .getByRole("button", { name: "Discard edits", exact: true })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "Discard product edits",
      exact: true,
    });
    await expect(dialog).toBeVisible();
    await dialog
      .getByRole("button", { name: "Discard this draft", exact: true })
      .focus();
    await expect(
      dialog.getByRole("button", { name: "Discard this draft", exact: true }),
    ).toBeFocused();
    const geometry = await dialog.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const controls = [...element.querySelectorAll("button")].map((button) =>
        button.getBoundingClientRect(),
      );
      return {
        left: rect.left,
        right: rect.right,
        top: rect.top,
        bottom: rect.bottom,
        width: innerWidth,
        height: innerHeight,
        overflow: element.scrollWidth > element.clientWidth,
        controlsInside: controls.every(
          (control) =>
            control.left >= rect.left &&
            control.right <= rect.right &&
            control.top >= rect.top &&
            control.bottom <= rect.bottom,
        ),
      };
    });
    expect(geometry.left).toBeGreaterThanOrEqual(0);
    expect(geometry.right).toBeLessThanOrEqual(geometry.width);
    expect(geometry.top).toBeGreaterThanOrEqual(0);
    expect(geometry.bottom).toBeLessThanOrEqual(geometry.height);
    expect(geometry.overflow).toBe(false);
    expect(geometry.controlsInside).toBe(true);
    await page.screenshot({
      path: info.outputPath("draft-discard-review.png"),
    });
    await dialog
      .getByRole("button", { name: "Keep editing", exact: true })
      .click();
    await expect(customer(page)).toHaveValue("Retained fictional product");
    const notice = await page
      .getByRole("button", { name: "Discard edits", exact: true })
      .evaluate((button) => {
        const element = button.parentElement!;
        const heading = element.closest(".product-workbench-heading")!;
        const bounds = element.getBoundingClientRect();
        const copy = element.querySelector("span")!.getBoundingClientRect();
        const action = button.getBoundingClientRect();
        return {
          width: bounds.width,
          available: heading.getBoundingClientRect().width,
          copyWidth: copy.width,
          separated:
            copy.right + 4 <= action.left || copy.bottom + 4 <= action.top,
          overflow: element.scrollWidth > element.clientWidth,
        };
      });
    expect(notice.width).toBeGreaterThanOrEqual(notice.available - 1);
    expect(notice.copyWidth).toBeGreaterThanOrEqual(
      Math.min(240, notice.width - 28),
    );
    expect(notice.separated).toBe(true);
    expect(notice.overflow).toBe(false);
    await page.screenshot({ path: info.outputPath("draft-retained.png") });
  });

test("unfinished reference review can be explicitly removed after navigation", async ({
  page,
}) => {
  const state = await setup(page);
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Review reference views", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Generate wireframes", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Journey reference viewer", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Done reviewing journeys", exact: true })
    .click();
  await navigate(page, "Connections");
  await navigate(page, "Product definition");
  await expect(
    page.getByText("Reference review was interrupted.", { exact: false }),
  ).toBeVisible();
  expect(state.writes()).toBe(0);
  await page
    .getByRole("button", { name: "Remove reference review", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save draft definition", exact: true })
    .click();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
});

test("confirmed logout clears drafts even when the following identity refresh fails", async ({
  page,
}) => {
  const state = await setup(page);
  await customer(page).fill("Prior identity private edit");
  state.failIdentityRefresh();
  await page
    .getByRole("button", { name: "Signed in as Test operator", exact: true })
    .click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByText("Connect your factory first", { exact: true }),
  ).toBeVisible();
  expect(await unloadPrevented(page)).toBe(false);
  await expect.poll(state.identityFailures).toBeGreaterThan(0);
  state.restoreIdentity();
  await connect(page);
  await navigate(page, "Product definition");
  await expect(customer(page)).toHaveValue("Saved A");
});

test("a re-entry GET captured before save completion cannot regress the saved revision", async ({
  page,
}) => {
  await setup(page);
  let acceptPut!: () => void, acceptRead!: () => void;
  const putGate = new Promise<void>((resolve) => {
    acceptPut = resolve;
  });
  const readGate = new Promise<void>((resolve) => {
    acceptRead = resolve;
  });
  let putStarted = false,
    readStarted = false;
  await page.route(`**/repositories/${a}/product`, async (route) => {
    if (route.request().method() === "PUT") {
      putStarted = true;
      const definition = route.request().postDataJSON();
      await putGate;
      await route.fulfill({ json: { ...product(a, 2), definition } });
    } else {
      readStarted = true;
      const older = product(a);
      await readGate;
      await route.fulfill({ json: older });
    }
  });
  await customer(page).fill("Submitted A");
  for (let step = 0; step < 3; step++)
    await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save draft definition", exact: true })
    .click();
  await expect.poll(() => putStarted).toBe(true);
  await navigate(page, "Connections");
  await navigate(page, "Product definition");
  await expect.poll(() => readStarted).toBe(true);
  const savedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      response.url().endsWith(`/repositories/${a}/product`),
  );
  acceptPut();
  await savedResponse;
  await expect.poll(() => unloadPrevented(page)).toBe(false);
  acceptRead();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  for (let step = 0; step < 4; step++)
    await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(customer(page)).toHaveValue("Submitted A");
  await expect(
    page.getByText("Editing saved revision 2", { exact: true }),
  ).toBeVisible();
});

test("confirmed logout cancels an older identity read before a failed replacement refresh", async ({
  page,
}) => {
  const state = await setup(page);
  await customer(page).fill("Prior session unsaved edit");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let heldRequest: Request | null = null;
  let holdNext = true;
  await page.route("**/auth/session", async (route) => {
    if (!holdNext) {
      await route.fallback();
      return;
    }
    holdNext = false;
    heldRequest = route.request();
    await gate;
    if (route.request().failure()?.errorText === "net::ERR_ABORTED") return;
    try {
      await route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name: "Test operator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: true,
          memberships: [],
          roles: {
            owner: ["read", "edit", "execute", "review", "release", "members"],
            maintainer: ["read", "edit", "execute", "review", "release"],
            developer: ["read", "edit", "execute"],
            reviewer: ["read", "review"],
            viewer: ["read"],
          },
        },
      });
    } catch (cause) {
      if (route.request().failure()?.errorText !== "net::ERR_ABORTED")
        throw cause;
    }
  });
  try {
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(() => heldRequest !== null).toBe(true);
    state.failIdentityRefresh();
    await page
      .getByRole("button", { name: "Signed in as Test operator", exact: true })
      .click();
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect.poll(state.identityFailures).toBeGreaterThan(0);
    release();
    await expect
      .poll(() => heldRequest?.failure()?.errorText)
      .toBe("net::ERR_ABORTED");
    await expect(
      page.getByRole("button", {
        name: "Signed in as Test operator",
        exact: true,
      }),
    ).toHaveCount(0);
    expect(await unloadPrevented(page)).toBe(false);
    state.restoreIdentity();
    const reloaded = page.waitForRequest(
      (request) =>
        request.method() === "GET" &&
        new URL(request.url()).pathname === "/api/v1/repositories",
    );
    await connect(page);
    await reloaded;
    await navigate(page, "Product definition");
    await expect(customer(page)).toHaveValue("Saved A");
  } finally {
    release();
  }
});
