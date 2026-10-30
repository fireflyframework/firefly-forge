/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { navigate, expectAuthenticated } from "./navigation";
const original = {
  version: 1,
  revision: "a".repeat(32),
  digest: "b".repeat(64),
  settings: {
    enabled: true,
    projected_retrieval: false,
    max_age_hours: 72,
    max_candidates: 8,
    max_lookup_calls: 30,
    max_repeated_attempts: 2,
  },
};
async function fixture(
  page: Page,
  options: {
    admin?: boolean;
    uncertain?: boolean;
    legacy?: boolean;
    malformed?: boolean;
    learningDisabled?: boolean;
  } = {},
) {
  let saved = structuredClone(original);
  if (options.learningDisabled) {
    saved.settings.enabled = false;
    saved.settings.projected_retrieval = true;
  }
  const writes: Record<string, unknown>[] = [],
    calls: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    calls.push(req.method() + " " + path);
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
            display_name: "Fixture administrator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: options.admin !== false,
          memberships: [],
          roles: {},
        },
      });
    if (path.endsWith("/learning/policy")) {
      if (req.method() === "PUT") {
        const body = req.postDataJSON();
        writes.push(body);
        saved = {
          ...saved,
          revision: "c".repeat(32),
          digest: "d".repeat(64),
          settings: body,
        };
        if (options.uncertain) return route.abort("failed");
      }
      if (options.malformed)
        return route.fulfill({
          json: { ...saved, settings: { ...saved.settings, enabled: "false" } },
        });
      const result = structuredClone(saved) as typeof saved;
      if (options.legacy)
        delete (result.settings as Partial<typeof result.settings>)
          .projected_retrieval;
      return route.fulfill({ json: result });
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
  if (options.admin !== false) await navigate(page, "Administration");
  else await expectAuthenticated(page);
  return { writes, calls };
}
async function open(page: Page) {
  await page
    .locator("main")
    .getByRole("button", { name: "Learning", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Learning", exact: true }),
  ).toBeVisible();
}
const next = (page: Page) =>
  page.getByRole("button", { name: "Continue", exact: true }).click();
test("administrator reviews and saves native learning policy without changing retained repair limits", async ({
  page,
}) => {
  const { writes, calls } = await fixture(page);
  await open(page);
  await page.getByRole("button", { name: "Edit learning settings" }).click();
  await page
    .getByLabel("Read verified learning from flycanon", { exact: true })
    .check();
  await next(page);
  await page.getByLabel("Maximum age (hours)", { exact: true }).fill("48");
  await page.getByLabel("Source results per lookup", { exact: true }).fill("4");
  await next(page);
  expect(writes).toHaveLength(0);
  await expect(
    page.getByText(
      "Applies to new runs. Existing runs keep their captured policy.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save learning settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Learning settings saved",
  );
  expect(writes).toEqual([
    {
      ...original.settings,
      projected_retrieval: true,
      max_age_hours: 48,
      max_candidates: 4,
    },
  ]);
  expect(calls.filter((call) => call.startsWith("POST"))).toEqual([]);
  await page.reload();
  await navigate(page, "Administration");
  await open(page);
  await expect(page.locator("main")).toContainText("48 hours");
});
test("non-administrator cannot load or save factory learning policy", async ({
  page,
}) => {
  const { calls, writes } = await fixture(page, { admin: false });
  await expect(
    page.getByRole("button", { name: "Administration", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.locator("main").getByRole("button", { name: "Learning", exact: true }),
  ).toHaveCount(0);
  expect(calls.some((call) => call.endsWith("/learning/policy"))).toBe(false);
  expect(writes).toEqual([]);
});
test("disabled verified learning preserves its projection preference as inactive", async ({
  page,
}) => {
  const { writes } = await fixture(page, { learningDisabled: true });
  await open(page);
  await expect(
    page.getByText("Inactive while learning is disabled", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Edit learning settings" }).click();
  const projected = page.getByLabel("Read verified learning from flycanon", {
    exact: true,
  });
  await expect(projected).toBeDisabled();
  await expect(projected).toBeChecked();
  await page.getByLabel("Enable verified learning", { exact: true }).check();
  await expect(projected).toBeEnabled();
  await expect(projected).toBeChecked();
  await page.getByLabel("Enable verified learning", { exact: true }).uncheck();
  await next(page);
  await page.getByLabel("Maximum age (hours)").fill("24");
  await next(page);
  await expect(
    page.getByText("Inactive while learning is disabled", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save learning settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Learning settings saved",
  );
  expect(writes).toEqual([
    {
      ...original.settings,
      enabled: false,
      projected_retrieval: true,
      max_age_hours: 24,
    },
  ]);
});
test("uncertain save cannot replay and requires reading the saved revision", async ({
  page,
}) => {
  const { writes } = await fixture(page, { uncertain: true });
  await open(page);
  await page.getByRole("button", { name: "Edit learning settings" }).click();
  await next(page);
  await page.getByLabel("Maximum age (hours)").fill("24");
  await next(page);
  await page
    .getByRole("button", { name: "Save learning settings", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Reload saved settings");
  await expect(
    page.getByRole("button", { name: "Save learning settings", exact: true }),
  ).toBeDisabled();
  expect(writes).toHaveLength(1);
  await page
    .getByRole("button", { name: "Reload saved settings", exact: true })
    .click();
  await expect(page.locator("main")).toContainText("24 hours");
  expect(writes).toHaveLength(1);
});
test("legacy backend cannot enable a capability it does not expose", async ({
  page,
}) => {
  const { writes } = await fixture(page, { legacy: true });
  await open(page);
  await page.getByRole("button", { name: "Edit learning settings" }).click();
  await expect(
    page.getByLabel("Read verified learning from flycanon", { exact: true }),
  ).toBeDisabled();
  await next(page);
  await page.getByLabel("Maximum age (hours)").fill("24");
  await next(page);
  await page
    .getByRole("button", { name: "Save learning settings", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText(
    "Learning settings saved",
  );
  expect(writes).toHaveLength(1);
  expect(writes[0]).not.toHaveProperty("projected_retrieval");
});
test("malformed native settings block edits instead of silently creating defaults", async ({
  page,
}) => {
  const { writes } = await fixture(page, { malformed: true });
  await open(page);
  await expect(page.getByRole("alert")).toContainText(
    "unsupported learning settings",
  );
  await expect(
    page.getByRole("button", { name: "Edit learning settings" }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});
test("empty numeric limits stay editable and cannot advance or save", async ({
  page,
}) => {
  const warnings: string[] = [];
  page.on("console", (value) => {
    if (value.type() === "error") warnings.push(value.text());
  });
  const { writes } = await fixture(page);
  await open(page);
  await page.getByRole("button", { name: "Edit learning settings" }).click();
  await next(page);
  await page.getByLabel("Maximum age (hours)").fill("");
  await next(page);
  await expect(
    page.getByRole("heading", {
      name: "How much knowledge can a run retrieve?",
    }),
  ).toBeVisible();
  expect(writes).toEqual([]);
  expect(warnings.some((value) => value.includes("NaN"))).toBe(false);
  await page.getByLabel("Maximum age (hours)").fill("48");
  await next(page);
  await expect(
    page.getByRole("heading", { name: "Review learning settings" }),
  ).toBeVisible();
});
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 2560, height: 1080 },
  { width: 1024, height: 768 },
  { width: 1440, height: 560 },
  { width: 812, height: 375 },
  { width: 390, height: 844 },
]) {
  for (const theme of ["light", "dark"]) {
    test(`learning wizard uses available space ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (value) => localStorage.setItem("forge.appearance", value),
        theme,
      );
      await fixture(page);
      await open(page);
      await page
        .getByRole("button", { name: "Edit learning settings" })
        .click();
      for (let step = 0; step < 3; step++) {
        await expect
          .poll(async () => {
            const fields = await page
              .locator(".learning-wizard .wizard-field-scroll")
              .boundingBox();
            return fields?.height ?? 0;
          })
          .toBeGreaterThanOrEqual(120);
        const action = page.getByRole("button", {
          name: step === 2 ? "Save learning settings" : "Continue",
          exact: true,
        });
        await action.scrollIntoViewIfNeeded();
        const geometry = await action.evaluate((button) => {
          const main = button.closest("main")!,
            box = button.getBoundingClientRect(),
            bounds = main.getBoundingClientRect();
          const footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
          const heading = main
            .querySelector(".learning-wizard .wizard-heading")!
            .getBoundingClientRect();
          return {
            fits:
              box.left >= bounds.left &&
              box.right <= bounds.right &&
              box.top >= bounds.top &&
              box.bottom <= bounds.bottom &&
              box.bottom <= footer.top,
            horizontal: main.scrollWidth <= main.clientWidth + 1,
            context: innerHeight < 560 || heading.top >= bounds.top,
          };
        });
        expect(geometry).toEqual({
          fits: true,
          horizontal: true,
          context: true,
        });
        if (process.env.FORGE_LEARNING_CAPTURE_DIR) {
          await page.evaluate(() => document.fonts.ready);
          await expect(page.locator("html")).toHaveAttribute(
            "data-theme",
            theme,
          );
          await page.screenshot({
            path: `${process.env.FORGE_LEARNING_CAPTURE_DIR}/learning-${step + 1}-${viewport.width}x${viewport.height}-${theme}.png`,
            fullPage: true,
          });
        }
        if (step < 2) await action.click();
      }
    });
  }
}
