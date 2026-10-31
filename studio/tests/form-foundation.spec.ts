/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Synthetic factory data; no provider, native recovery or Original requests.
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { mockIdentity } from "./auth-fixture";
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
const provider = {
  id: "codex",
  name: "Fixture coding provider",
  ready: false,
  detail: "Synthetic fields; no provider request is sent.",
  fields: [
    "Model",
    "Endpoint",
    "Organization",
    "Workspace",
    "API key",
    "Region",
  ].map((label, i) => ({
    key: `field${i}`,
    label,
    secret: label === "API key",
    required: false,
    placeholder: label,
  })),
  config: {},
};
async function setup(page: Page, theme: "light" | "dark") {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
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
      json = { status: "ok", execution_ready: false };
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
  await expect(
    page.getByRole("list", { name: "Setup progress" }),
  ).toBeVisible();
}
async function frame(page: Page, info: TestInfo, name: string) {
  await page.evaluate(() => document.fonts.ready);
  const back = page.getByRole("button", {
    name: "Back to section start",
    exact: true,
  });
  if (
    await page
      .locator(".workspace-main > main")
      .evaluate((node) => node.scrollTop > 1)
  ) {
    await expect(back).toBeEnabled();
    await back.click();
  }
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
      hint: !!document.querySelector(".form-scroll-status:not([hidden])"),
      mode: wizard?.dataset.layout ?? "bounded",
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
  { width: 1440, height: 540 },
  { width: 390, height: 844 },
  { width: 390, height: 540 },
  { width: 320, height: 568 },
  { width: 320, height: 480 },
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
      const providerFrame = await frame(page, info, "provider");
      await navigate(page, "Product definition");
      await expect(
        page.getByLabel("Customer or user", { exact: true }),
      ).toBeVisible();
      await page
        .getByLabel("Customer or user", { exact: true })
        .fill("Operators");
      await page
        .getByLabel("Problem to solve", { exact: true })
        .fill("A clearly scoped product definition");
      const productFrame = await frame(page, info, "product");
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
        if (g.heading && g.first && Math.abs(g.heading.x - g.first.x) > 1)
          violations.push(`${name}: progress/heading inset differs`);
        if (g.heading && g.fields && Math.abs(g.heading.x - g.fields.x) > 1)
          violations.push(`${name}: heading/field inset differs`);
        if (g.heading && g.heading.y < g.main.y)
          violations.push(`${name}: heading clipped by main header`);
        if (
          ((g.fieldsScroll &&
            g.fieldsScroll.full > g.fieldsScroll.height + 1) ||
            g.mainScroll.full > g.mainScroll.height + 1) &&
          !g.hint
        )
          violations.push(`${name}: remaining content has no visible cue`);
      }
      expect(violations).toEqual([]);
    });
  }
