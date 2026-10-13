/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { navigate } from "./navigation";

const legacy = {
  provider: "brave",
  endpoint: "https://api.search.brave.com/res/v1/web/search",
  revision: "a".repeat(32),
  configured: true,
  max_results: 10,
  live_search_verified: true,
};
const studioVersion = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
).version;
async function fixture(
  page: Page,
  options: {
    fresh?: boolean;
    admin?: boolean;
    uncertain?: boolean;
    quota?: boolean;
    rotateDuringTest?: boolean;
    rejectKey?: boolean;
    removedKey?: boolean;
    malformedSave?: boolean;
  } = {},
) {
  let saved = options.fresh
    ? {
        ...legacy,
        revision: null,
        configured: false,
        live_search_verified: false,
      }
    : {
        ...legacy,
        ...(options.removedKey
          ? { configured: false, live_search_verified: false }
          : {}),
      };
  const writes: unknown[] = [],
    calls: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname,
      method = request.method();
    calls.push(method + " " + path);
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: { mode: "local_bootstrap", issuer: "", cookie_secure: false },
      });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name: "Fixture operator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: options.admin !== false,
          memberships: [],
          roles: {
            owner: ["read", "edit", "execute", "review", "release", "members"],
            viewer: ["read"],
          },
        },
      });
    if (path.endsWith("/search/configuration")) {
      if (method === "PUT") {
        const body = request.postDataJSON();
        writes.push(body);
        if (options.rejectKey)
          return route.fulfill({
            status: 422,
            json: { detail: "Rejected fixture key: " + body.api_key },
          });
        saved = {
          ...saved,
          ...(body.provider
            ? {
                provider: body.provider,
                endpoint:
                  body.provider === "tavily"
                    ? "https://api.tavily.com/search"
                    : legacy.endpoint,
                include_domains: body.include_domains ?? [],
              }
            : {}),
          ...(body.include_domains
            ? { include_domains: body.include_domains }
            : {}),
          configured: body.clear_secret ? false : true,
          revision: "b".repeat(32),
          live_search_verified: false,
        };
        if (options.uncertain) return route.abort("failed");
        if (options.malformedSave)
          return route.fulfill({
            json: { provider: "unknown", configured: "true" },
          });
      }
      return route.fulfill({ json: saved });
    }
    if (path.endsWith("/search/test")) {
      if (options.quota)
        return route.fulfill({
          status: 409,
          json: {
            detail:
              "Provider quota exhausted. <img src=x onerror=alert(1)> " +
              "x".repeat(10000),
            search_error: { provider: "brave", kind: "quota", status: 432 },
          },
        });
      saved = {
        ...saved,
        live_search_verified: !options.rotateDuringTest,
        ...(options.rotateDuringTest ? { revision: "c".repeat(32) } : {}),
      };
      return route.fulfill({
        json: {
          provider: "brave",
          live_search_verified: true,
          result_count: 1,
        },
      });
    }
    return route.fulfill({
      json: [
        "/repositories",
        "/runs",
        "/providers",
        "/projects",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((suffix) => path.endsWith(suffix))
        ? []
        : {},
    });
  });
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  await navigate(page, "Administration");
  return { writes, calls };
}
async function openSearch(page: Page) {
  await page
    .locator("main")
    .getByRole("button", { name: "Internet search", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Internet search", exact: true }),
  ).toBeVisible();
}
async function next(page: Page) {
  await page.getByRole("button", { name: "Continue", exact: true }).click();
}

test("visible footer uses the tracked Studio release version", async ({
  page,
}) => {
  await fixture(page);
  await expect(page.locator(".workspace-main>footer")).toContainText(
    "v" + studioVersion,
  );
});

test("preserves saved Brave and keeps saving separate from a billable test", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { writes, calls } = await fixture(page);
  await openSearch(page);
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await expect(page.getByLabel("Search provider", { exact: true })).toHaveValue(
    "brave",
  );
  await next(page);
  await expect(page.getByLabel("Provider key", { exact: true })).toHaveValue(
    "",
  );
  await page
    .getByLabel("Allowed websites", { exact: true })
    .fill("Docs.GitHub.com\nfireflyframework.org");
  await next(page);
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("saved");
  expect(writes).toEqual([
    { include_domains: ["docs.github.com", "fireflyframework.org"] },
  ]);
  expect(calls.filter((value) => value.includes("/search/test"))).toEqual([]);
  await expect(page.getByText("Not tested", { exact: true })).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("search-saved-light.png"),
  });
  await page
    .getByRole("button", { name: "Test saved connection", exact: true })
    .click();
  await expect(
    page.getByText("Verified for this revision", { exact: true }),
  ).toBeVisible();
  expect(calls.filter((value) => value.includes("/search/test"))).toHaveLength(
    1,
  );
});

test("new setup recommends Tavily and provider switching cannot reuse a draft key", async ({
  page,
}) => {
  const { writes } = await fixture(page, { fresh: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Set up internet search", exact: true })
    .click();
  await expect(page.getByLabel("Search provider", { exact: true })).toHaveValue(
    "tavily",
  );
  await next(page);
  await page
    .getByLabel("Provider key", { exact: true })
    .fill("fixture-tavily-key");
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByLabel("Search provider", { exact: true })
    .selectOption("brave");
  await next(page);
  await expect(page.getByLabel("Provider key", { exact: true })).toHaveValue(
    "",
  );
  await page
    .getByLabel("Provider key", { exact: true })
    .fill("fixture-brave-key");
  await next(page);
  await expect(page.locator("main")).not.toContainText("fixture-brave-key");
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  expect(writes).toEqual([{ provider: "brave", api_key: "fixture-brave-key" }]);
});

test("unknown save outcome blocks resubmission until saved metadata is reloaded", async ({
  page,
}) => {
  const { writes } = await fixture(page, { uncertain: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await next(page);
  await page.getByLabel("Allowed websites", { exact: true }).fill("github.com");
  await next(page);
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("not confirmed");
  await expect(
    page.getByRole("button", { name: "Save search settings", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Reload saved settings", exact: true })
    .click();
  await expect(page.getByText("github.com", { exact: true })).toBeVisible();
  expect(writes).toHaveLength(1);
});

test("test receipts do not verify a different current revision", async ({
  page,
}) => {
  await fixture(page, { rotateDuringTest: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Test saved connection", exact: true })
    .click();
  await expect(page.getByText("Not tested", { exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("current revision");
});

test("rejected key returns to access with a cleared field and does not echo the key", async ({
  page,
}) => {
  const { writes } = await fixture(page, { rejectKey: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await page
    .getByLabel("Search provider", { exact: true })
    .selectOption("tavily");
  await next(page);
  await page
    .getByLabel("Provider key", { exact: true })
    .fill("fixture-rejected-key");
  await next(page);
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeFocused();
  await expect(page.getByRole("alert")).not.toContainText(
    "fixture-rejected-key",
  );
  await expect(page.getByLabel("Provider key", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Provider key", { exact: true })).toHaveValue(
    "",
  );
  expect(writes).toHaveLength(1);
});

test("a saved Brave revision without an active key is not treated as a new Tavily setup", async ({
  page,
}) => {
  await fixture(page, { removedKey: true });
  await openSearch(page);
  await expect(
    page.getByRole("button", { name: "Test saved connection", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await expect(page.getByLabel("Search provider", { exact: true })).toHaveValue(
    "brave",
  );
});

test("quota failures stay bounded, safe and explicit without retrying", async ({
  page,
}) => {
  const { calls } = await fixture(page, { quota: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Test saved connection", exact: true })
    .click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("quota exhausted");
  expect((await alert.textContent())!.length).toBeLessThan(1200);
  expect(await alert.locator("img").count()).toBe(0);
  expect(calls.filter((value) => value.includes("/search/test"))).toHaveLength(
    1,
  );
});

test("project membership does not expose factory search configuration", async ({
  page,
}) => {
  const { calls } = await fixture(page, { admin: false });
  await expect(
    page.getByRole("button", { name: "Internet search", exact: true }),
  ).toBeDisabled();
  expect(calls.filter((value) => value.includes("/search/"))).toEqual([]);
});

test("unchanged legacy settings do not create a revision or reset verification", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  await openSearch(page);
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await next(page);
  await next(page);
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  await expect(
    page.getByText("Verified for this revision", { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
});

test("explicit key removal disables new requests without promising historical deletion", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  await openSearch(page);
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await next(page);
  await page.getByText("Remove the saved key", { exact: true }).click();
  await expect(
    page.getByText(/Historical keys remain available/),
  ).toBeVisible();
  await page
    .getByLabel("Remove the active key when I save", { exact: true })
    .check();
  await next(page);
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Test saved connection", exact: true }),
  ).toBeDisabled();
  expect(writes).toEqual([{ clear_secret: true }]);
});

test("malformed save metadata does not claim success or allow resubmission", async ({
  page,
}) => {
  const { writes } = await fixture(page, { malformedSave: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Edit search settings", exact: true })
    .click();
  await next(page);
  await page.getByLabel("Allowed websites", { exact: true }).fill("github.com");
  await next(page);
  await page
    .getByRole("button", { name: "Save search settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("not confirmed");
  await expect(
    page.getByRole("button", { name: "Save search settings", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("status")).toHaveCount(0);
  expect(writes).toHaveLength(1);
});

test("entering search from a scrolled administration page keeps its heading below the header", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await fixture(page, { fresh: true });
  await page
    .locator(".workspace-main>main")
    .evaluate((el) => (el.scrollTop = el.scrollHeight));
  expect(
    await page.locator(".workspace-main>main").evaluate((el) => el.scrollTop),
  ).toBeGreaterThan(0);
  await openSearch(page);
  await page
    .getByRole("button", { name: "Set up internet search", exact: true })
    .click();
  await expect
    .poll(async () =>
      page
        .getByRole("heading", { name: "Internet search", exact: true })
        .evaluate((el) => {
          const h = el.getBoundingClientRect(),
            header = document
              .querySelector(".workspace-main>header")!
              .getBoundingClientRect();
          return h.top >= header.bottom + 8;
        }),
    )
    .toBe(true);
  await expect(
    page.getByLabel("Search provider", { exact: true }),
  ).toBeVisible();
});

test("phone access keeps the whole website field and instructions clear of its action row", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page, { fresh: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Set up internet search", exact: true })
    .click();
  await next(page);
  await page.getByLabel("Provider key", { exact: true }).fill("fixture-key");
  await page
    .getByLabel("Allowed websites", { exact: true })
    .fill("github.com\nfireflyframework.org");
  await page
    .getByRole("button", { name: "Continue", exact: true })
    .scrollIntoViewIfNeeded();
  const geometry = await page.locator("#search-domains-hint").evaluate((el) => {
    const hint = el.getBoundingClientRect(),
      actions = document
        .querySelector(".search-wizard .wizard-actions")!
        .getBoundingClientRect(),
      input = document
        .querySelector("#search-domains")!
        .getBoundingClientRect();
    return {
      hintBottom: hint.bottom,
      actionsTop: actions.top,
      inputBottom: input.bottom,
    };
  });
  expect(geometry.hintBottom).toBeLessThanOrEqual(geometry.actionsTop - 8);
  expect(geometry.inputBottom).toBeLessThan(geometry.actionsTop);
});

test("opening the provider form does not scroll the wide page heading under the header", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, { fresh: true });
  await openSearch(page);
  await page
    .getByRole("button", { name: "Set up internet search", exact: true })
    .click();
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect
    .poll(async () =>
      page
        .getByRole("heading", { name: "Internet search", exact: true })
        .evaluate((el) => {
          const h = el.getBoundingClientRect(),
            header = document
              .querySelector(".workspace-main>header")!
              .getBoundingClientRect();
          return h.top >= header.bottom + 8;
        }),
    )
    .toBe(true);
});

for (const viewport of [
  { width: 390, height: 844 },
  { width: 1440, height: 900 },
])
  test(`saved search card keeps content inset and its test action usable at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await fixture(page, { fresh: true });
    await openSearch(page);
    await page
      .getByRole("button", { name: "Set up internet search", exact: true })
      .click();
    await next(page);
    await page
      .getByLabel("Provider key", { exact: true })
      .fill("fixture-saved-key");
    await next(page);
    await page
      .getByRole("button", { name: "Save search settings", exact: true })
      .click();
    const action = page.getByRole("button", {
      name: "Test saved connection",
      exact: true,
    });
    await action.scrollIntoViewIfNeeded();
    const geometry = await action.evaluate((el) => {
      const b = el.getBoundingClientRect(),
        card = el.closest(".search-state")!.getBoundingClientRect(),
        heading = el
          .closest(".search-state")!
          .querySelector("h2")!
          .getBoundingClientRect(),
        f = document
          .querySelector(".workspace-main>footer")!
          .getBoundingClientRect();
      return {
        contentInset: heading.left - card.left,
        bottom: b.bottom,
        footer: f.top,
        hit:
          document
            .elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
            ?.closest("button") === el,
      };
    });
    expect(geometry.contentInset).toBeGreaterThanOrEqual(16);
    expect(geometry.bottom).toBeLessThanOrEqual(geometry.footer - 8);
    expect(geometry.hit).toBe(true);
  });

for (const viewport of [
  { width: 375, height: 667 },
  { width: 390, height: 560 },
  { width: 390, height: 844 },
  { width: 1440, height: 900 },
])
  for (const theme of ["light", "dark"])
    test(`search wizard action and key fields remain usable ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await fixture(page, { fresh: true });
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await openSearch(page);
      await page
        .getByRole("button", { name: "Set up internet search", exact: true })
        .click();
      await page.screenshot({
        path: test.info().outputPath(`search-provider-${theme}.png`),
      });
      const button = page.getByRole("button", {
        name: "Continue",
        exact: true,
      });
      await button.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("heading", { name: "Add provider access", exact: true }),
      ).toBeFocused();
      await page
        .getByLabel("Provider key", { exact: true })
        .fill("fixture-new-key");
      await page
        .getByLabel("Allowed websites", { exact: true })
        .fill("github.com");
      await button.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: test.info().outputPath(`search-access-${theme}.png`),
      });
      await page
        .getByLabel("Allowed websites", { exact: true })
        .fill("https://github.com");
      await button.click();
      await expect(page.getByRole("alert")).toContainText("public hostnames");
      await page
        .getByLabel("Allowed websites", { exact: true })
        .fill("github.com");
      await button.scrollIntoViewIfNeeded();
      const measured = await button.evaluate((el) => {
        const b = el.getBoundingClientRect(),
          f = document
            .querySelector(".workspace-main>footer")!
            .getBoundingClientRect();
        return {
          bottom: b.bottom,
          footer: f.top,
          hit:
            document
              .elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
              ?.closest("button") === el,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      expect(measured.bottom).toBeLessThanOrEqual(measured.footer - 8);
      expect(measured.hit).toBe(true);
      expect(measured.overflow).toBe(false);
      await button.click();
      await expect(
        page.getByRole("heading", {
          name: "Review search settings",
          exact: true,
        }),
      ).toBeFocused();
      await expect(page.getByText("Tavily", { exact: true })).toBeVisible();
      const save = page.getByRole("button", {
        name: "Save search settings",
        exact: true,
      });
      await save.scrollIntoViewIfNeeded();
      const finalAction = await save.evaluate((el) => {
        const b = el.getBoundingClientRect(),
          f = document
            .querySelector(".workspace-main>footer")!
            .getBoundingClientRect();
        return {
          bottom: b.bottom,
          footer: f.top,
          hit:
            document
              .elementFromPoint(b.x + b.width / 2, b.y + b.height / 2)
              ?.closest("button") === el,
        };
      });
      expect(finalAction.bottom).toBeLessThanOrEqual(finalAction.footer - 8);
      expect(finalAction.hit).toBe(true);
      await page.screenshot({
        path: test.info().outputPath(`search-review-${theme}.png`),
      });
    });
