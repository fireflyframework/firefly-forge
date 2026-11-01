/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Controlled API fixtures qualify approval UX, never native QA or original CRM recovery.
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";
const runId = "2".repeat(32),
  repositoryId = "3".repeat(32),
  workerId = "4".repeat(32);
function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  const row = value as Record<string, unknown>;
  return (
    "{" +
    Object.keys(row)
      .sort()
      .map((key) => JSON.stringify(key) + ":" + canonical(row[key]))
      .join(",") +
    "}"
  );
}
async function setup(page: Page, theme = "light") {
  const run = {
    id: runId,
    repository_id: repositoryId,
    brief: "Verification approval fixture",
    status: "paused",
    agent: "bridge",
    worker_request: { id: workerId, kind: "verification", status: "pending" },
  };
  const state = {
    posts: 0,
    candidateAgents: [] as string[],
    lost: false,
    denied: "" as "" | "candidate" | "submission" | "receipt",
    body: null as Record<string, unknown> | null,
    candidate: {
      version: 1,
      run_id: runId,
      worker_request_id: workerId,
      agent: "bridge",
      pending_request_digest: "1".repeat(64),
      source_digest: "2".repeat(64),
      commands_digest: "3".repeat(64),
      policy_digest: "4".repeat(64),
      provider_digest: "5".repeat(64),
      commands_count: 7,
      max_continuations: 1,
      reusable_build_candidates: [
        {
          command_index: 2,
          command_digest: "6".repeat(64),
          argv: ["python", "-m", "ruff", "check", "backend"],
          purpose: "source_build",
          requires_declaration: true,
        },
      ],
    },
  };
  function receipt() {
    return {
      version: 1,
      request: state.body,
      body_digest: createHash("sha256")
        .update(canonical(state.body))
        .digest("hex"),
      actor: {
        user_id: "f".repeat(32),
        authentication_method: "local_bootstrap",
      },
      policy_snapshot: { digest: state.candidate.policy_digest },
    };
  }
  await page.route("**/api/v1/**", async (route) => {
    if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
      return;
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/verification-handoffs/candidates")) {
      state.candidateAgents.push(
        new URL(req.url()).searchParams.get("agent") ?? "",
      );
      if (state.denied === "candidate")
        return route.fulfill({
          status: 403,
          json: { detail: "Access revoked" },
        });
      return route.fulfill({ json: state.candidate });
    }
    if (path.includes("/verification-handoffs/adoptions")) {
      if (req.method() === "POST") {
        state.posts++;
        state.body = req.postDataJSON();
        if (state.denied === "submission")
          return route.fulfill({
            status: 403,
            json: { detail: "Access revoked" },
          });
        if (state.lost) return route.abort("connectionreset");
      }
      if (req.method() === "GET" && state.denied === "receipt")
        return route.fulfill({
          status: 403,
          json: { detail: "Access revoked" },
        });
      return route.fulfill({ json: receipt() });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: repositoryId,
            name: "Verification approval fixture",
            url: "/fixture",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [run]
        : path.endsWith("/runs/" + runId)
          ? run
          : path.endsWith("/plan")
            ? { revision: 1, tasks: [] }
            : path.endsWith("/journal")
              ? { valid: true, entries: [] }
              : [];
    return route.fulfill({ json });
  });
  await page.goto("/");
  await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fictional-ui-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  async function open() {
    await navigate(page, "Factory");
    await openFactoryRun(page, /Verification approval fixture/);
    await page
      .getByRole("tab", { name: "Agent questions", exact: true })
      .click();
  }
  await open();
  return { state, open };
}
async function choose(page: Page) {
  const panel = page.getByRole("region", {
    name: "Factory verification approval",
    exact: true,
  });
  await panel
    .getByRole("button", { name: "Read approved checks", exact: true })
    .click();
  await panel.getByRole("checkbox", { name: /Check 2/ }).check();
  await panel
    .getByRole("button", { name: "Review verification approval", exact: true })
    .click();
  await expect(
    panel.getByRole("button", {
      name: "Save verification approval",
      exact: true,
    }),
  ).toBeDisabled();
  await panel
    .getByRole("checkbox", { name: /I approve the frozen checks/ })
    .check();
  return panel;
}
async function approvalHeadingInFrame(page: Page) {
  const bounds = await page
    .locator(".verification-adoption h3")
    .evaluate((heading) => {
      const box = heading.getBoundingClientRect();
      return {
        top: box.top,
        bottom: box.bottom,
        headerBottom: document
          .querySelector(".workspace-main > header")!
          .getBoundingClientRect().bottom,
        footerTop: document
          .querySelector(".workspace-main > footer")!
          .getBoundingClientRect().top,
      };
    });
  expect(bounds.top).toBeGreaterThanOrEqual(bounds.headerBottom);
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.footerTop);
}
for (const width of [1440, 390])
  for (const theme of ["light", "dark"]) {
    test(`verification approval wizard fits ${width}px ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      const { state } = await setup(page, theme);
      const panel = page.getByRole("region", {
        name: "Factory verification approval",
        exact: true,
      });
      await panel
        .getByRole("button", { name: "Read approved checks", exact: true })
        .click();
      await expect(panel).toContainText("Run all 7 frozen checks now");
      await panel.getByRole("checkbox", { name: /Check 2/ }).check();
      await captureWalkthrough(
        page,
        "verification-approval-choose",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/verification-adoption.spec.ts",
          actionSelector:
            '.verification-adoption button:has-text("Review verification approval")',
          focusSelector: ".verification-adoption",
        },
      );
      await approvalHeadingInFrame(page);
      await panel
        .getByRole("button", {
          name: "Review verification approval",
          exact: true,
        })
        .click();
      await panel
        .getByRole("checkbox", { name: /I approve the frozen checks/ })
        .check();
      await captureWalkthrough(
        page,
        "verification-approval-review",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/verification-adoption.spec.ts",
          actionSelector:
            '.verification-adoption button:has-text("Save verification approval")',
          focusSelector: ".verification-adoption",
        },
      );
      await approvalHeadingInFrame(page);
      await panel
        .getByRole("button", {
          name: "Save verification approval",
          exact: true,
        })
        .click();
      await expect(panel).toContainText("Verification approval recorded");
      expect(state.posts).toBe(1);
      expect(state.body?.reusable_build_checks).toEqual([
        {
          command_digest: "6".repeat(64),
          purpose: "source_build",
          independent_of_time_and_external_state: true,
        },
      ]);
      await captureWalkthrough(
        page,
        "verification-approval-recorded",
        theme,
        width === 390 ? "mobile" : "desktop",
        {
          producer: "studio/tests/verification-adoption.spec.ts",
          actionSelector:
            '.verification-adoption button:has-text("Read saved verification approval")',
          focusSelector: ".verification-adoption",
        },
      );
      await approvalHeadingInFrame(page);
    });
  }
test("lost approval reply survives reload and is read without a second POST", async ({
  page,
}) => {
  const { state, open } = await setup(page);
  const panel = await choose(page);
  state.lost = true;
  await panel
    .getByRole("button", { name: "Save verification approval", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toBeVisible();
  expect(state.posts).toBe(1);
  await page.reload();
  await open();
  const restored = page.getByRole("region", {
    name: "Factory verification approval",
    exact: true,
  });
  await expect(
    restored.getByRole("button", {
      name: "Save verification approval",
      exact: true,
    }),
  ).toHaveCount(0);
  await restored
    .getByRole("button", {
      name: "Read saved verification approval",
      exact: true,
    })
    .click();
  await expect(restored).toContainText("Verification approval recorded");
  expect(state.posts).toBe(1);
});
test("foreign source candidate never reaches approval controls", async ({
  page,
}) => {
  const { state } = await setup(page);
  state.candidate.run_id = "a".repeat(32);
  const panel = page.getByRole("region", {
    name: "Factory verification approval",
    exact: true,
  });
  await panel
    .getByRole("button", { name: "Read approved checks", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toBeVisible();
  expect(state.posts).toBe(0);
  await expect(
    panel.getByRole("button", {
      name: "Review verification approval",
      exact: true,
    }),
  ).toHaveCount(0);
});

test("unavailable browser storage blocks approval before any POST", async ({
  page,
}) => {
  const { state } = await setup(page);
  const panel = await choose(page);
  await page.evaluate(() => {
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.verification-adoption.v1:"))
        throw new DOMException("Storage unavailable", "QuotaExceededError");
      setItem.call(this, key, value);
    };
  });
  await panel
    .getByRole("button", { name: "Save verification approval", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toContainText(
    "No submission was sent",
  );
  await expect(panel.getByRole("button")).toHaveCount(0);
  expect(state.posts).toBe(0);
});

for (const provider of ["agentic", "bridge", "codex", "claude"])
  test(`approval uses the explicitly selected ${provider} provider`, async ({
    page,
  }) => {
    const { state } = await setup(page);
    state.candidate.agent = provider;
    await page
      .getByLabel("Verification coding provider")
      .selectOption(provider);
    const panel = await choose(page);
    await panel
      .getByRole("button", { name: "Save verification approval", exact: true })
      .click();
    await expect(panel).toContainText("Verification approval recorded");
    expect(state.candidateAgents).toEqual([provider]);
    expect(state.body?.agent).toBe(provider);
    expect(state.posts).toBe(1);
  });

for (const stage of ["candidate", "submission", "receipt"] as const)
  test(`access refusal at ${stage} clears private review and keeps saved identity`, async ({
    page,
  }) => {
    const { state } = await setup(page);
    const panel = page.getByRole("region", {
      name: "Factory verification approval",
      exact: true,
    });
    if (stage === "candidate") {
      state.denied = stage;
      await panel
        .getByRole("button", { name: "Read approved checks", exact: true })
        .click();
    } else {
      await choose(page);
      state.denied = stage;
      await panel
        .getByRole("button", {
          name: "Save verification approval",
          exact: true,
        })
        .click();
      if (stage === "receipt") {
        await expect(panel).toContainText("Verification approval recorded");
        await panel
          .getByRole("button", {
            name: "Read saved verification approval",
            exact: true,
          })
          .click();
      }
    }
    await expect(panel.getByRole("alert")).toContainText(
      "Current execution access could not be verified",
    );
    await expect(panel.getByRole("checkbox")).toHaveCount(0);
    await expect(panel).not.toContainText("python -m ruff");
    await expect(panel).not.toContainText("Verification approval recorded");
    await expect(
      panel.getByRole("button", {
        name: "Save verification approval",
        exact: true,
      }),
    ).toHaveCount(0);
    expect(state.posts).toBe(stage === "candidate" ? 0 : 1);
    const saved = await page.evaluate(() =>
      Object.keys(localStorage).filter((key) =>
        key.startsWith("forge.verification-adoption.v1:"),
      ),
    );
    expect(saved).toHaveLength(stage === "candidate" ? 0 : 1);
  });
