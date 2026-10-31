/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Synthetic factory data; no provider, native recovery or Original requests.
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { mockIdentity } from "./auth-fixture";
import { assistantChatFixture } from "./assistant-chat-fixture";
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
import { navigate } from "./navigation";

const h = "a".repeat(64),
  repo = {
    id: "1".repeat(32),
    name: "Foundation fixture API",
    provider: "git",
    url: "/fixture",
    default_branch: "main",
  };
const run = {
  id: "4".repeat(32),
  repository_id: repo.id,
  repository_name: repo.name,
  brief: "Foundation retained result fixture",
  status: "paused",
  attempt: 3,
  agent: "bridge",
  publish: false,
  base_revision: "b".repeat(40),
  consumed_terminal_recovery: {
    state: "claimed",
    request_id: "5".repeat(32),
    record_digest: h,
    claim_digest: h,
  },
};
// Mirrors the API-backed schema emitted by providers.py from configuration.FIELDS.
const provider = {
  id: "agentic",
  name: "Firefly Agentic · API",
  ready: false,
  detail:
    "Configure an API-backed agent. This browser fixture sends no model request.",
  auth_mode: "provider_api_key",
  model: "openai-responses:gpt-6-luna",
  fields: ["model", "endpoint", "api_key", "timeout", "max_tokens"].map(
    (key) => ({
      key,
      label:
        key === "api_key"
          ? "API key"
          : key.charAt(0).toUpperCase() + key.slice(1).replaceAll("_", " "),
      secret: key === "api_key",
      required: ["api_key", "model"].includes(key),
      placeholder: (
        {
          model: "openai-responses:gpt-6-luna",
          endpoint: "https://api.example.com/v1",
          api_key: "Enter your API key",
          timeout: "1800",
          max_tokens: "16384",
        } as Record<string, string>
      )[key],
    }),
  ),
  config: {
    model: "openai-responses:gpt-6-luna",
    timeout: "1800",
    max_tokens: "16384",
    api_key_configured: false,
  },
};
async function setup(page: Page, theme: "light" | "dark", healthy = true) {
  const chat = assistantChatFixture(repo.id, "recorded");
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    if (await chat.handle(route)) return;
    if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
      return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/reconciliation"))
      return route.fulfill({
        status: 404,
        json: { detail: "No reconciliation receipt" },
      });
    let json: unknown = [];
    if (path.endsWith("/repositories")) json = [repo];
    else if (path.endsWith("/providers")) json = [provider];
    else if (
      path.endsWith("/product") ||
      path.endsWith("/brand") ||
      path.endsWith("/product/conditional")
    )
      json = null;
    else if (path.endsWith("/runs")) json = [run];
    else if (path.endsWith(`/runs/${run.id}`)) json = run;
    else if (path.endsWith("/plan"))
      json = {
        revision: 1,
        tasks: [{ id: "implement", status: "paused", attempt: 3 }],
      };
    else if (path.endsWith("/journal")) json = { valid: true, entries: [] };
    else if (path.endsWith("/health"))
      json = { status: "ok", execution_ready: healthy };
    else if (path.endsWith("/skills")) json = { skills: [] };
    else if (path.endsWith("/reconciliation-candidate"))
      json = {
        version: 1,
        run_id: run.id,
        request_id: run.consumed_terminal_recovery.request_id,
        same_task_attempt: 3,
        ...Object.fromEntries(
          [
            "owner_digest",
            "actor_digest",
            "policy_digest",
            "runtime_contract",
            "profile_digest",
            "run_digest",
            "task_digest",
            "journal_digest",
            "recovery_digest",
            "claim_digest",
            "source_digest",
            "index_digest",
            "digest",
          ].map((key) => [key, h]),
        ),
      };
    return route.fulfill({ json });
  });
  await page.goto("/");
  await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("foundation-fictional-bootstrap-token");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(page.locator(".wizard-rail")).toBeVisible();
}
async function frame(page: Page, info: TestInfo, name: string) {
  await page.evaluate(() => document.fonts.ready);

  const geometry = await page.evaluate(() => {
    const main = document.querySelector<HTMLElement>(".workspace-main > main")!;
    const footer = document.querySelector<HTMLElement>(
      ".workspace-main > footer",
    )!;
    const wizard = document.querySelector<HTMLElement>(".wizard-frame");
    const heading = wizard?.querySelector<HTMLElement>(".wizard-heading h2");
    const first = wizard?.querySelector<HTMLElement>(".step-number");
    const fields = wizard?.querySelector<HTMLElement>(".wizard-field-scroll");
    const action =
      wizard?.querySelector<HTMLElement>(".wizard-actions button.primary") ??
      document.querySelector<HTMLElement>(
        ".consumed-terminal-reconciliation button.primary",
      );
    const rect = (e: Element | null | undefined) =>
      e
        ? {
            x: e.getBoundingClientRect().x,
            y: e.getBoundingClientRect().y,
            width: e.getBoundingClientRect().width,
            height: e.getBoundingClientRect().height,
            bottom: e.getBoundingClientRect().bottom,
          }
        : null;
    return {
      main: rect(main)!,
      footer: rect(footer)!,
      wizard: rect(wizard),
      heading: rect(heading),
      first: rect(first),
      fields: rect(fields),
      action: rect(action),
      ordinals: Array.from(wizard?.querySelectorAll(".step-number") ?? []).map(
        (n) => n.textContent,
      ),
      mainScroll: {
        height: main.clientHeight,
        full: main.scrollHeight,
        position: main.scrollTop,
      },
      fieldsScroll: fields
        ? {
            height: fields.clientHeight,
            full: fields.scrollHeight,
            position: fields.scrollTop,
            overflow: getComputedStyle(fields).overflowY,
          }
        : null,
      horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      mode: wizard?.dataset.layout ?? "bounded",
      variant: wizard?.classList.contains("wizard-rail") ? "rail" : "steps",
    };
  });
  await writeFile(
    info.outputPath(`${name}-geometry.json`),
    JSON.stringify(geometry, null, 2),
  );
  await page.screenshot({ path: info.outputPath(`${name}.png`) });
  return geometry;
}
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
  { width: 320, height: 568 },
])
  for (const theme of ["light", "dark"] as const) {
    test(`shared form foundation at ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      await setup(page, theme);
      await page
        .locator(".provider-card")
        .getByRole("button", { name: "Configure", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "Continue", exact: true }),
      ).toHaveCount(0);
      await expect(page.locator(".provider-card")).toHaveCount(0);
      if (viewport.width < 760) {
        const stepSelector = page.getByRole("combobox", {
          name: "Setup step",
          exact: true,
        });
        const control = await stepSelector.boundingBox();
        expect(control!.height).toBeGreaterThanOrEqual(44);
        await expect(stepSelector).toBeDisabled();
        await expect(stepSelector).toHaveCSS("opacity", "1");
      }
      const providerFrame = await frame(page, info, "provider");
      await page.getByLabel("Max tokens", { exact: true }).focus();
      const region = await page
        .getByLabel("Max tokens", { exact: true })
        .boundingBox();
      const providerMain = await page
        .locator(".workspace-main > main")
        .boundingBox();
      expect(region!.y).toBeGreaterThanOrEqual(providerMain!.y);
      expect(region!.y + region!.height).toBeLessThanOrEqual(
        providerMain!.y + providerMain!.height,
      );
      await page
        .getByRole("button", {
          name: "Save settings",
          exact: true,
        })
        .focus();
      await page.screenshot({
        path: info.outputPath("provider-keyboard-actions.png"),
      });
      await page
        .getByRole("button", { name: "Cancel settings", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "Continue", exact: true }),
      ).toBeEnabled();
      await navigate(page, "Product definition");
      await expect(
        page.getByLabel("Customer or user", { exact: true }),
      ).toBeVisible();
      if (viewport.width >= 1100)
        await expect(page.getByRole("log")).toContainText(
          "Outline safe inventory reservations.",
        );
      const productFrame = await frame(page, info, "product");
      if (viewport.width < 1100) {
        await page
          .getByRole("button", { name: "Open design assistant", exact: true })
          .click();
        await expect(page.getByRole("log")).toContainText(
          "Outline safe inventory reservations.",
        );
        await page.screenshot({ path: info.outputPath("product-chat.png") });
        await page.keyboard.press("Escape");
      }
      await page
        .getByLabel("Customer or user", { exact: true })
        .fill("Operators");
      await page
        .getByLabel("Problem to solve", { exact: true })
        .fill("A clearly scoped product definition");

      await page.getByLabel("Product owners", { exact: true }).focus();
      await page.screenshot({
        path: info.outputPath("product-keyboard-actions.png"),
      });
      await navigate(page, "Factory");
      await openFactoryRun(page, run.brief);
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      const recovery = page.getByRole("region", {
        name: "Claimed result reconciliation",
        exact: true,
      });
      await recovery
        .getByRole("button", { name: "Check claimed action", exact: true })
        .click();
      await expect(
        recovery.getByRole("heading", {
          name: "Review the original action",
          exact: true,
        }),
      ).toBeVisible();
      await frame(page, info, "recovery");
      await recovery.getByRole("checkbox").check();
      const reconcile = recovery.getByRole("button", {
        name: "Reconcile saved result",
        exact: true,
      });
      await reconcile.focus();
      await expect(reconcile).toBeFocused();
      const actionBounds = await reconcile.boundingBox(),
        mainBounds = await page.locator(".workspace-main > main").boundingBox();
      expect(actionBounds!.y).toBeGreaterThanOrEqual(mainBounds!.y);
      expect(actionBounds!.y + actionBounds!.height).toBeLessThanOrEqual(
        mainBounds!.y + mainBounds!.height + 1,
      );
      await page.screenshot({
        path: info.outputPath("recovery-keyboard-action.png"),
      });
      const violations: string[] = [];
      for (const [name, g] of [
        ["provider", providerFrame],
        ["product", productFrame],
      ] as const) {
        if (g.horizontalOverflow)
          violations.push(`${name}: horizontal overflow`);
        if (!g.ordinals.every((n, i) => n?.trim() === String(i + 1)))
          violations.push(`${name}: ordinal number replaced`);
        if (
          g.variant !== "rail" &&
          g.heading &&
          g.first &&
          Math.abs(g.heading.x - g.first.x) > 1
        )
          violations.push(`${name}: progress/heading inset differs`);
        if (g.heading && g.fields && Math.abs(g.heading.x - g.fields.x) > 1)
          violations.push(`${name}: heading/field inset differs`);
        if (g.heading && g.heading.y < g.main.y)
          violations.push(`${name}: heading clipped by main header`);
        if (
          g.fieldsScroll &&
          (g.fieldsScroll.overflow !== "visible" ||
            g.fieldsScroll.full > g.fieldsScroll.height + 1)
        )
          violations.push(
            `${name}: full-page fields have an internal viewport`,
          );
        if (g.hint)
          violations.push(`${name}: page flow has compensating scroll chatter`);
      }
      expect(violations).toEqual([]);
    });
  }

test("provider editing retains failed input and requires an explicit save or cancel before step navigation", async ({
  page,
}) => {
  await setup(page, "light");
  let writes = 0;
  await page.route(
    "**/api/v1/providers/agentic/configuration",
    async (route) => {
      writes++;
      const input = route.request().postDataJSON();
      if (writes === 1)
        return route.fulfill({
          status: 409,
          json: { detail: "Configuration changed; review your settings." },
        });
      expect(input).toEqual({
        values: {
          model: "saved-model",
          endpoint: "",
          api_key: "",
          timeout: "1800",
          max_tokens: "16384",
        },
        clear_secrets: [],
      });
      return route.fulfill({ json: { ...provider, config: input.values } });
    },
  );
  const configure = () =>
    page.getByRole("button", { name: "Configure", exact: true }).click();
  const next = page.getByRole("button", { name: "Continue", exact: true });
  const save = () =>
    page
      .getByRole("button", {
        name: "Save settings",
        exact: true,
      })
      .click();
  await configure();
  await expect(
    page.getByRole("heading", {
      name: "Configure Firefly Agentic · API",
      exact: true,
    }),
  ).toBeFocused();
  await page.getByLabel("Model", { exact: true }).fill("retained-model");
  await expect(next).toHaveCount(0);
  await expect(
    page
      .getByRole("list", { name: "Setup progress" })
      .getByRole("button")
      .first(),
  ).toBeDisabled();
  await save();
  await expect(
    page
      .getByRole("region", {
        name: "Configure Firefly Agentic · API",
        exact: true,
      })
      .getByRole("alert"),
  ).toContainText("Configuration changed");
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue(
    "retained-model",
  );
  await expect(next).toHaveCount(0);
  await page
    .getByRole("button", { name: "Cancel settings", exact: true })
    .click();
  await expect(next).toBeEnabled();
  expect(writes).toBe(1);
  await configure();
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue(
    "openai-responses:gpt-6-luna",
  );
  await page.getByLabel("Model", { exact: true }).fill("saved-model");
  await save();
  await expect(
    page.getByRole("status").filter({ hasText: "Settings saved" }),
  ).toBeVisible();
  await expect(next).toBeEnabled();
  expect(writes).toBe(2);
  await next.click();
  const heading = page.getByRole("heading", {
    name: "Grant repository access",
    exact: true,
  });
  await expect(heading).toBeFocused();
  const bounds = await heading.boundingBox(),
    main = await page.locator(".workspace-main > main").boundingBox();
  expect(bounds!.y).toBeGreaterThanOrEqual(main!.y);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
    main!.y + main!.height,
  );
});

test("mobile rail preserves step access rules and returns focus to the selected step heading", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await setup(page, "dark");
  const selector = page.getByRole("combobox", {
    name: "Setup step",
    exact: true,
  });
  await expect(selector).toHaveValue("1");
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(selector).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Continue", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Cancel settings", exact: true })
    .click();
  await selector.selectOption("0");
  await expect(
    page.getByRole("heading", { name: "Factory connection", exact: true }),
  ).toBeFocused();
  await expect(selector.locator('option[value="2"]')).toBeDisabled();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(selector).toHaveValue("1");
  await expect(
    page.getByRole("heading", {
      name: "Choose your coding provider",
      exact: true,
    }),
  ).toBeFocused();
});

test("unhealthy host remains visible while provider fields use the primary page scroll", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await setup(page, "light", false);
  await expect(
    page.getByRole("alert").filter({ hasText: "Execution is unavailable" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await page.getByLabel("Max tokens", { exact: true }).focus();
  expect(
    await page
      .locator(".workspace-main > main")
      .evaluate((node) => node.scrollTop),
  ).toBeGreaterThan(0);
  await expect(page.locator(".wizard-field-scroll")).toHaveCSS(
    "overflow-y",
    "visible",
  );
  await expect(
    page.getByRole("alert").filter({ hasText: "Execution is unavailable" }),
  ).toHaveCount(1);
});
