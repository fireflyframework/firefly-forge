/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// PREPARED ONLY. Controlled API fixtures exercise the real UI, not native recovery.
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import {
  setup,
  open,
  continueOriginal,
} from "./agent-checkpoint-recovery-fixture";
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`one-use original result review is readable and keyboard reachable at ${width}px in ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const state = await setup(page, "restore", "valid", theme);
      const panel = page.getByRole("region", {
        name: "Original model result",
        exact: true,
      });
      await expect(
        panel.getByLabel("Original implementation attempt"),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Review original result", exact: true })
        .focus();
      await page.keyboard.press("Enter");
      await expect(
        panel.getByRole("heading", {
          name: "Review continuation",
          exact: true,
        }),
      ).toBeFocused();
      const action = page.getByRole("button", {
        name: "Continue from original result",
        exact: true,
      });
      await expect(action).toBeDisabled();
      await panel.getByRole("checkbox").focus();
      await page.keyboard.press("Space");
      await expect(action).toBeEnabled();
      expect(state.posts).toBe(0);
      expect(
        await panel.evaluate((node) => node.scrollWidth > node.clientWidth + 1),
      ).toBe(false);
      await action.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`original-result-${width}-${theme}-viewport.png`),
      });
      await panel.screenshot({
        path: info.outputPath(`original-result-${width}-${theme}-panel.png`),
      });
    });
for (const kind of ["restore", "observation"] as const)
  test(`lost ${kind} response and exact404 keep one original identity across reload`, async ({
    page,
  }) => {
    const state = await setup(page, kind);
    if (kind === "restore") await continueOriginal(page);
    else
      await page
        .getByRole("button", { name: "Check original result", exact: true })
        .click();
    await expect(
      page.getByText(/Existing request identities stay held/),
    ).toBeVisible();
    const original = state.request;
    expect(state.posts).toBe(1);
    await page.reload();
    await open(page);
    await page
      .getByRole("button", { name: "Read exact saved request", exact: true })
      .click();
    await expect(
      page.getByText(/This does not prove the request was not accepted/),
    ).toBeVisible();
    expect(state.posts).toBe(1);
    expect(state.request).toBe(original);
    expect(
      await page
        .getByRole("button", {
          name: "Continue from original result",
          exact: true,
        })
        .count(),
    ).toBe(0);
    state.readStatus = 200;
    await page
      .getByRole("button", { name: "Read exact saved request", exact: true })
      .click();
    await expect(
      page.getByText(
        kind === "restore"
          ? /This confirms the original implementation receipt/
          : /Model result pending/,
      ),
    ).toBeVisible();
    expect(state.posts).toBe(1);
  });
test("failed marker persistence sends no observation or restore POST", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.agent-checkpoint.v1:"))
        throw new Error("Owned storage refusal");
      original.call(this, key, value);
    };
  });
  const state = await setup(page);
  await continueOriginal(page);
  await expect(
    page.getByText(/Request storage or access could not be verified/),
  ).toBeVisible();
  expect(state.posts).toBe(0);
});
test("late403 clears private review and keeps exact reconciliation without exposing server text", async ({
  page,
}) => {
  const state = await setup(page, "restore", "denied");
  await continueOriginal(page);
  await expect(
    page.getByText(/Private result details are cleared/),
  ).toBeVisible();
  await expect(page.getByLabel("Original implementation attempt")).toHaveCount(
    0,
  );
  await expect(page.getByText("private denial canary")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Read exact saved request", exact: true }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});
test("Bridge choices preserve unset values and require recovery before checkpoints without a connection probe", async ({
  page,
}) => {
  const saved: Record<string, unknown>[] = [];
  let probes = 0;
  const provider = {
    id: "bridge",
    name: "Bridge",
    ready: true,
    detail: "Owned configuration fixture",
    config: { model: "bridge/default" } as Record<string, string>,
    fields: [
      {
        key: "model",
        label: "Model",
        secret: false,
        required: false,
        placeholder: "",
      },
      {
        key: "completion_recovery",
        label: "Completion Recovery",
        secret: false,
        required: false,
        placeholder: "",
      },
      {
        key: "checkpoints",
        label: "Checkpoints",
        secret: false,
        required: false,
        placeholder: "",
      },
    ],
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/providers/bridge/configuration")) {
      const body = route.request().postDataJSON();
      saved.push(body.values);
      Object.assign(provider.config, body.values);
      return route.fulfill({ json: provider });
    }
    if (path.endsWith("/providers/bridge/test")) probes++;
    return route.fulfill({
      json: path.endsWith("/providers")
        ? [provider]
        : path.endsWith("/health")
          ? { status: "ok", execution_ready: false }
          : [],
    });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fictional-ui-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(
    page.getByLabel("Original model result recovery", { exact: true }),
  ).toHaveValue("");
  await page.getByLabel("Model", { exact: true }).fill("bridge/other");
  await page
    .getByRole("button", { name: "Save Bridge settings", exact: true })
    .click();
  expect(saved).toEqual([{ model: "bridge/other" }]);
  await page.getByRole("button", { name: "Configure", exact: true }).click();
  await page
    .getByLabel("Implementation checkpoints", { exact: true })
    .selectOption("required");
  await page
    .getByRole("button", { name: "Save Bridge settings", exact: true })
    .click();
  expect(saved).toHaveLength(1);
  await expect(
    page.getByText(
      "Required implementation checkpoints need required original result recovery.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByLabel("Original model result recovery", { exact: true })
    .selectOption("required");
  await page
    .getByRole("button", { name: "Save Bridge settings", exact: true })
    .click();
  expect(saved[1]).toEqual({
    model: "bridge/other",
    completion_recovery: "required",
    checkpoints: "required",
  });
  expect(probes).toBe(0);
});

test("malformed refreshed evidence closes an acknowledged review and offers no stale action", async ({
  page,
}) => {
  const state = await setup(page, "restore", "valid");
  await page
    .getByRole("button", { name: "Review original result", exact: true })
    .click();
  await page
    .getByLabel(
      "I understand this result is consumed once and a later failure may remain held.",
    )
    .check();
  Object.assign(state.candidate, { response: "private response canary" });
  await page
    .getByRole("button", { name: "Refresh saved result", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Continue from original result",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(page.getByText("private response canary")).toHaveCount(0);
  expect(state.posts).toBe(0);
});

test("a different server-discovered ticket remains readable while the local uncertain identity stays held", async ({
  page,
}) => {
  const state = await setup(page);
  await continueOriginal(page);
  await expect(
    page.getByText(/Existing request identities stay held/),
  ).toBeVisible();
  const original = state.request;
  Object.assign(state.candidate, {
    state: "ticket_recorded",
    ticket: { id: "9".repeat(32), status: "queued" },
  });
  await page
    .getByRole("button", { name: "Refresh saved result", exact: true })
    .click();
  state.readStatus = 200;
  await page
    .getByRole("button", { name: "Read recorded continuation", exact: true })
    .click();
  await expect(
    page.getByText(/This confirms the original implementation receipt/),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  const saved = await page.evaluate(
    () =>
      Object.entries(sessionStorage).find(([key]) =>
        key.startsWith("forge.agent-checkpoint.v1:"),
      )?.[1],
  );
  expect(saved).toContain(original);
  await expect(
    page.getByRole("button", {
      name: "Continue from original result",
      exact: true,
    }),
  ).toHaveCount(0);
});

test("original result review scrolls within a short viewport without footer overlap", async ({
  page,
}, info) => {
  const viewport = { width: 390, height: 560 };
  await page.setViewportSize(viewport);
  const state = await setup(page, "restore", "valid", "dark");
  const panel = page.getByRole("region", {
    name: "Original model result",
    exact: true,
  });
  await panel
    .getByRole("button", { name: "Review original result", exact: true })
    .click();
  await panel.getByRole("checkbox").check();
  const action = panel.getByRole("button", {
    name: "Continue from original result",
    exact: true,
  });
  await action.scrollIntoViewIfNeeded();
  await action.focus();
  await expect(action).toBeFocused();
  await expect(action).toBeEnabled();
  const main = page.locator(".workspace-main > main");
  const bounds = await action.boundingBox(),
    mainBounds = await main.boundingBox(),
    footer = await page.locator(".workspace-main > footer").boundingBox();
  expect(bounds && mainBounds && footer).toBeTruthy();
  expect(mainBounds!.y + mainBounds!.height).toBeLessThanOrEqual(footer!.y + 1);
  expect(bounds!.y).toBeGreaterThanOrEqual(mainBounds!.y);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(footer!.y);
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
  expect(footer!.y + footer!.height).toBeLessThanOrEqual(viewport.height + 1);
  expect(await main.evaluate((node) => node.scrollTop > 0)).toBe(true);
  expect(
    await main.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
  ).toBe(true);
  expect(
    await action.evaluate((node) => {
      const r = node.getBoundingClientRect();
      return node.contains(
        document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
      );
    }),
  ).toBe(true);
  expect(state.posts).toBe(0);
  await page.screenshot({
    path: info.outputPath("original-result-short-viewport.png"),
  });
  await panel.screenshot({
    path: info.outputPath("original-result-short-panel.png"),
  });
});
