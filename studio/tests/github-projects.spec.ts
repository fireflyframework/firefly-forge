/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { captureWalkthrough as capture } from "./walkthrough-capture";
const projectId = "a".repeat(32),
  repositoryId = "b".repeat(32);
const component = {
  id: "api",
  name: "Product API",
  repository_id: repositoryId,
  path: ".",
  kind: "api",
  backend: "pyfly",
  frontend: "none",
  verification: [],
  depends_on: [],
  responsibility: "Product delivery",
};
const project = {
  id: projectId,
  revision: 1,
  digest: "c".repeat(64),
  created_at: "2026-10-31T00:00:00Z",
  updated_at: "2026-10-31T00:00:00Z",
  definition: {
    name: "Delivery product",
    description: "Controlled browser fixture",
    owners: ["Team"],
    topology: "monorepo",
    components: [component],
  },
};
const board = {
  id: "PVT_1",
  title: "Delivery",
  url: "https://github.com/users/octocat/projects/9",
  owner_kind: "user",
  owner: "octocat",
  number: 9,
  viewer_can_update: true,
  closed: false,
  private: true,
};
const proof = {
  version: 1,
  project_id: projectId,
  repository_id: repositoryId,
  connection_name: "delivery",
  viewer: { id: "U_1", login: "octocat" },
  repository: { id: "R_1", name_with_owner: "team/product", private: true },
};
const fieldOptions = [
  { id: "TODO", name: "To do" },
  { id: "IN_PROGRESS", name: "In progress" },
  { id: "REVIEW", name: "In review" },
  { id: "ATTENTION", name: "Needs attention" },
  { id: "CANCELLED", name: "Cancelled" },
  { id: "ARCHIVED", name: "Archived" },
];
const mapping: Record<string, string> = {
  Queued: "TODO",
  Running: "IN_PROGRESS",
  Paused: "REVIEW",
  "Needs reconciliation": "ATTENTION",
  Failed: "ATTENTION",
  Cancelled: "CANCELLED",
  Interrupted: "ATTENTION",
};
const expectedMapping = {
  queued: "TODO",
  running: "IN_PROGRESS",
  paused: "REVIEW",
  needs_reconciliation: "ATTENTION",
  failed: "ATTENTION",
  cancelled: "CANCELLED",
  interrupted: "ATTENTION",
  succeeded: "REVIEW",
};
const oauth = {
  name: "delivery",
  revision: 1,
  provider: "github-projects-private",
  client_id: "synthetic-client",
  project_id: projectId,
  status: "authorized_unverified",
  resource: "https://api.github.com/graphql",
  scopes: ["project", "repo"],
  agent_ready: false,
  remote_revocation_supported: false,
  policy_current: true,
  authentication: "client_secret_post",
  public_client: false,
  secret_configured: true,
  callback_uri: "https://factory.invalid/callback",
  refresh_available: false,
  grant_expires_at: null,
  effect_state: "none",
  unresolved_effects: [],
  archived_unknown_effects: [],
  reconciliation_required: false,
  allowed_actions: {
    configure: true,
    authorize: true,
    refresh: false,
    disconnect: true,
    supersede: false,
  },
  family: "d".repeat(32),
  grant_revision: 1,
  grant_status: "authorized_unverified",
  observed_at: null,
};
async function setup(page: Page, mode = "success") {
  const state = {
    creates: 0,
    saves: 0,
    reads: 0,
    proofs: 0,
    body: null as Record<string, unknown> | null,
    binding: null as Record<string, unknown> | null,
    markerBeforeCreate: false,
  };
  const view = () => ({
    version: 1,
    project_id: projectId,
    repository_id: repositoryId,
    binding: state.binding,
    permissions: { configure: mode !== "reader", sync: mode !== "reader" },
    status: state.binding ? "ready" : "not_configured",
    latest_request: null,
  });
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/repositories"))
      json = [
        {
          id: repositoryId,
          name: "Product repository",
          provider: "github",
          url: "https://github.com/team/product",
          default_branch: "main",
        },
      ];
    if (
      ["/runs", "/providers", "/deployment-targets", "/analyses"].some((p) =>
        path.endsWith(p),
      )
    )
      json = [];
    if (path.endsWith("/projects")) json = [project];
    if (path.endsWith("/projects/" + projectId))
      json = {
        ...project,
        components: [
          {
            ...component,
            latest_status: "not_started",
            repository_run_count: 0,
            runs: [],
          },
        ],
        counts: { not_started: 1 },
      };
    if (path.endsWith("/connection-oauth")) json = [oauth];
    if (path.includes("/github-projects")) {
      if (path.endsWith("/connection")) {
        if (req.method() === "PUT") {
          state.saves++;
          state.body = req.postDataJSON();
          state.binding = {
            ...state.body,
            digest: "f".repeat(64),
            revision: 1,
            board_url: board.url,
          };
          if (mode === "save-lost") {
            await route.abort();
            return;
          }
        }
        json = view();
      }
      if (path.endsWith("/account")) {
        state.proofs++;
        json =
          mode === "foreign"
            ? { ...proof, repository_id: "f".repeat(32) }
            : proof;
      }
      if (path.endsWith("/discover"))
        json = {
          version: 1,
          connection_name: "delivery",
          board,
          repository: proof.repository,
          viewer: proof.viewer,
          fields: [
            {
              id: "FIELD_1",
              name: "Status",
              options: fieldOptions,
            },
            { id: "FIELD_2", name: "Delivery status", options: fieldOptions },
          ],
          discovery_digest: "e".repeat(64),
          truncated: false,
        };
      if (path.endsWith("/creations")) {
        state.creates++;
        state.body = req.postDataJSON();
        state.markerBeforeCreate = await page.evaluate(
          (id) =>
            Object.keys(localStorage).some(
              (key) =>
                key.startsWith("forge.github-projects:") &&
                localStorage.getItem(key)?.includes(id),
            ),
          String(state.body!.request_id),
        );
        if (mode === "lost") {
          await route.abort();
          return;
        }
        json = {
          version: 1,
          id: state.body!.request_id,
          project_id: projectId,
          repository_id: repositoryId,
          request_digest: "e".repeat(64),
          status: "completed",
          board,
          replay_authorized: false,
          created_at: project.created_at,
        };
      }
      if (/\/creations\/[a-f0-9]{32}$/.test(path)) {
        state.reads++;
        json = {
          version: 1,
          id: path.split("/").at(-1),
          project_id: projectId,
          repository_id: repositoryId,
          request_digest: "e".repeat(64),
          status: "unknown",
          board: null,
          replay_authorized: false,
          created_at: project.created_at,
        };
      }
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("controlled-operator-token-at-least-24");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product projects");
  return state;
}
async function account(
  page: Page,
  screenshot?: string,
  afterVerify?: () => Promise<void>,
) {
  await page
    .getByRole("button", { name: "Set up GitHub Projects", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "GitHub Projects",
      level: 1,
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByLabel("Project OAuth connection").selectOption("delivery");
  await page
    .getByRole("button", { name: "Verify connected account", exact: true })
    .click();
  await expect(
    page.getByText("Connected as octocat", { exact: true }),
  ).toBeVisible();
  if (screenshot) await page.screenshot({ path: screenshot });
  await afterVerify?.();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
}
test("verified account and private discovery precede explicit state mapping", async ({
  page,
}, info) => {
  const state = await setup(page);
  await account(page);
  await page.getByLabel("Board number").fill("9");
  await page
    .getByRole("button", { name: "Find private board", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByLabel("GitHub board field", { exact: true }),
  ).toHaveAccessibleDescription("Delivery team/product · Private board");
  await expect(
    page.getByRole("button", { name: "Save board settings", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("GitHub board field", { exact: true })
    .selectOption("FIELD_1");
  const mappingControls = page.locator('select[id^="board-state-"]');
  await expect(mappingControls).toHaveCount(7);
  for (const select of await mappingControls.all())
    await expect(select).toHaveValue("");
  for (const select of await mappingControls.all())
    await select.selectOption("REVIEW");
  await page
    .getByLabel(
      "Allow forge to update only this field for linked issues and pull requests.",
    )
    .check();
  await page
    .getByLabel("GitHub board field", { exact: true })
    .selectOption("FIELD_2");
  for (const select of await mappingControls.all())
    await expect(select).toHaveValue("");
  await expect(
    page.getByLabel(
      "Allow forge to update only this field for linked issues and pull requests.",
    ),
  ).not.toBeChecked();
  await page
    .getByLabel("GitHub board field", { exact: true })
    .selectOption("FIELD_1");
  for (const name of [
    "Queued",
    "Running",
    "Paused",
    "Needs reconciliation",
    "Failed",
    "Cancelled",
    "Interrupted",
  ])
    await page
      .getByLabel(`GitHub board status for ${name}`, { exact: true })
      .selectOption(mapping[name]);
  await expect(
    page.getByRole("button", { name: "Save board settings", exact: true }),
  ).toBeDisabled();
  expect(state.saves).toBe(0);
  await page
    .getByLabel(
      "Allow forge to update only this field for linked issues and pull requests.",
    )
    .check();
  const setupView = page.getByRole("region", {
    name: "GitHub Projects setup",
    exact: true,
  });
  const main = page.locator(".workspace-main > main");
  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    const logo = page.getByAltText("Firefly forge", { exact: true });
    await expect(logo).toHaveAttribute(
      "src",
      `/assets/forge-lockup-${theme}.svg`,
    );
    await logo.evaluate(async (node) => {
      await (node as HTMLImageElement).decode();
    });
    for (const [width, height] of [
      [1440, 900],
      [390, 844],
      [320, 568],
    ]) {
      await page.setViewportSize({ width, height });
      await main.evaluate((node) => {
        node.scrollTop = 0;
      });
      await expect(setupView.locator(".wizard")).toHaveAttribute(
        "data-layout",
        "flow",
      );
      await expect(setupView.locator(".wizard")).toHaveClass(/wizard-rail/);
      await expect(page.getByRole("dialog")).toHaveCount(0);
      const table = setupView.getByRole("table", {
        name: "forge to GitHub status mapping",
      });
      await expect(table.getByRole("rowheader")).toHaveCount(7);
      await expect(
        table.getByRole("columnheader", {
          name: "forge run status",
          exact: true,
        }),
      ).toHaveCount(1);
      await expect(
        table.getByRole("columnheader", {
          name: "GitHub board status",
          exact: true,
        }),
      ).toHaveCount(1);
      await expect(
        page.getByText("7 of 7 mapped", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByLabel("GitHub board status for Succeeded", { exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("heading", {
          name: "GitHub Projects",
          level: 1,
          exact: true,
        }),
      ).toBeVisible();
      const fields = setupView.locator(".wizard-field-scroll");
      await expect(fields).toHaveCSS("overflow-y", "visible");
      const geometry = await fields.evaluate((node) => ({
        client: node.clientHeight,
        full: node.scrollHeight,
        scroll: node.scrollTop,
        width: document.documentElement.scrollWidth <= innerWidth,
      }));
      expect(geometry.full).toBeLessThanOrEqual(geometry.client + 1);
      expect(geometry.scroll).toBe(0);
      expect(geometry.width).toBe(true);
      await writeFile(
        info.outputPath(`board-geometry-${width}-${theme}.json`),
        JSON.stringify(geometry, null, 2),
      );
      if (width < 760)
        await expect(
          page.getByLabel("Setup step", { exact: true }),
        ).toBeVisible();
      await page.screenshot({
        path: info.outputPath(`board-mapping-${width}-${theme}.png`),
      });
      await main.evaluate((node) => {
        const table = node.querySelector(".board-status-map")!;
        node.scrollTop +=
          table.getBoundingClientRect().top -
          node.getBoundingClientRect().top -
          16;
      });
      await expect(table.getByRole("rowheader").first()).toBeInViewport();
      await expect(
        page.getByAltText("Firefly forge", { exact: true }),
      ).toBeInViewport();
      await page.screenshot({
        path: info.outputPath(`board-mapping-${width}-rows-${theme}.png`),
      });
      await main.evaluate((node) => {
        node.setAttribute("data-keyboard-scroll", "pending");
        node.addEventListener(
          "scrollend",
          () => node.setAttribute("data-keyboard-scroll", "complete"),
          { once: true },
        );
      });
      await main.focus();
      await page.keyboard.press("End");
      await expect(main).toHaveAttribute("data-keyboard-scroll", "complete");
      await expect
        .poll(() => main.evaluate((node) => node.scrollTop))
        .toBeGreaterThan(0);
      const consent = page.getByLabel(
        "Allow forge to update only this field for linked issues and pull requests.",
      );
      await consent.focus();
      await expect(consent).toBeFocused();
      const action = page.getByRole("button", {
        name: "Save board settings",
        exact: true,
      });
      await action.focus();
      await expect(action).toBeFocused();
      await expect(action).toBeInViewport();
      const actionBox = await action.boundingBox(),
        footer = await page.locator(".workspace-main > footer").boundingBox();
      expect(actionBox!.y + actionBox!.height).toBeLessThanOrEqual(footer!.y);
      await expect(
        page.getByAltText("Firefly forge", { exact: true }),
      ).toBeInViewport();
      await page.screenshot({
        path: info.outputPath(`board-mapping-${width}-end-${theme}.png`),
      });
      const mainBox = await main.boundingBox();
      await page.mouse.move(mainBox!.x + 8, mainBox!.y + mainBox!.height / 2);
      const end = await main.evaluate((node) => node.scrollTop);
      await page.mouse.wheel(0, -150);
      await expect
        .poll(() => main.evaluate((node) => node.scrollTop))
        .toBeLessThan(end);
    }
  }
  await page
    .getByRole("button", { name: "Save board settings", exact: true })
    .click();
  await expect.poll(() => state.saves).toBe(1);
  expect(state.body?.state_options).toEqual(expectedMapping);
  expect(state.creates).toBe(0);
});
test("a lost private creation retains its ID before dispatch and only permits readback after reload", async ({
  page,
}) => {
  const state = await setup(page, "lost");
  await account(page);
  await page.getByLabel("Board action").selectOption("create");
  await page.getByLabel("Board title").fill("Delivery");
  await expect(
    page.getByRole("button", {
      name: "Create private personal board",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByLabel("Create a private personal board under octocat.")
    .check();
  await page
    .getByRole("button", { name: "Create private personal board", exact: true })
    .click();
  await expect.poll(() => state.creates).toBe(1);
  await expect.poll(() => state.markerBeforeCreate).toBe(true);
  await page.reload();
  await navigate(page, "Product projects");
  await page
    .getByRole("button", { name: "Read creation receipt", exact: true })
    .click();
  await expect.poll(() => state.reads).toBe(1);
  await expect(page.getByText(/Creation remains unknown/)).toBeVisible();
  await account(page);
  await page.getByLabel("Board action").selectOption("create");
  await page.getByLabel("Board title").fill("Delivery");
  await page
    .getByLabel("Create a private personal board under octocat.")
    .check();
  await expect(
    page.getByRole("button", {
      name: "Create private personal board",
      exact: true,
    }),
  ).toBeDisabled();
  expect(state.creates).toBe(1);
});
test("read-only membership cannot reach creation", async ({ page }) => {
  const state = await setup(page, "reader");
  await expect(
    page.getByRole("button", { name: "Set up GitHub Projects", exact: true }),
  ).toBeDisabled();
  expect(state.proofs).toBe(0);
  expect(state.creates).toBe(0);
});
test("a lost settings response survives returning to the project without repeating the save", async ({
  page,
}) => {
  const state = await setup(page, "save-lost");
  await account(page);
  await page.getByLabel("Board number").fill("9");
  await page
    .getByRole("button", { name: "Find private board", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("GitHub board field", { exact: true })
    .selectOption("FIELD_1");
  const states = page.locator('select[id^="board-state-"]');
  await expect(states).toHaveCount(7);
  for (const select of await states.all()) await select.selectOption("REVIEW");
  await page
    .getByLabel(
      "Allow forge to update only this field for linked issues and pull requests.",
    )
    .check();
  await page
    .getByRole("button", { name: "Save board settings", exact: true })
    .click();
  await expect.poll(() => state.saves).toBe(1);
  await expect(page.getByRole("alert")).toBeVisible();
  const request = String(state.body!.request_id);
  await expect(page.getByText(request, { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Back to project", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Set up GitHub Projects", exact: true }),
  ).toBeDisabled();
  await expect(page.getByText(request, { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Refresh board settings", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Set up GitHub Projects", exact: true }),
  ).toBeEnabled();
  expect(state.saves).toBe(1);
  expect(state.creates).toBe(0);
});
test("account verification keeps the dedicated view busy until its response is read", async ({
  page,
}) => {
  const state = await setup(page);
  let release!: () => void;
  const response = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/github-projects/account", async (route) => {
    await response;
    await route.fulfill({ json: proof });
  });
  await page
    .getByRole("button", { name: "Set up GitHub Projects", exact: true })
    .click();
  await page.getByLabel("Project OAuth connection").selectOption("delivery");
  await page
    .getByRole("button", { name: "Verify connected account", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Back to project", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Verify connected account", exact: true }),
  ).toBeDisabled();
  release();
  await expect(
    page.getByText("Connected as octocat", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Back to project", exact: true }),
  ).toBeEnabled();
  expect(state.creates).toBe(0);
  expect(state.saves).toBe(0);
});
test("a foreign account proof clears setup authority and returning to the project restores focus", async ({
  page,
}) => {
  const state = await setup(page, "foreign");
  await page
    .getByRole("button", { name: "Set up GitHub Projects", exact: true })
    .click();
  await page.getByLabel("Project OAuth connection").selectOption("delivery");
  await page
    .getByRole("button", { name: "Verify connected account", exact: true })
    .click();
  await expect(
    page
      .getByRole("region", { name: "GitHub Projects setup", exact: true })
      .getByRole("alert"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Back to project",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Continue", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Back to project", exact: true })
    .focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("region", { name: "GitHub Projects setup", exact: true }),
  ).not.toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Product projects",
      level: 1,
      exact: true,
    }),
  ).toBeFocused();
  expect(state.creates).toBe(0);
  expect(state.saves).toBe(0);
});
test("explicit private creation uses the proved owner and opens discovered mapping", async ({
  page,
}, info) => {
  const state = await setup(page);
  await account(page, info.outputPath("board-account-desktop.png"));
  await page.getByLabel("Board action").selectOption("create");
  await page.getByLabel("Board title").fill("Delivery");
  await page
    .getByLabel("Create a private personal board under octocat.")
    .check();
  for (const [width, theme] of [
    [1440, "dark"],
    [390, "light"],
  ] as const) {
    await page.setViewportSize({ width, height: 844 });
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await page.screenshot({
      path: info.outputPath(`board-creation-${width}-${theme}.png`),
    });
  }
  await page
    .getByRole("button", { name: "Create private personal board", exact: true })
    .click();
  await expect(
    page.getByLabel("GitHub board field", { exact: true }),
  ).toBeVisible();
  expect(state.creates).toBe(1);
  expect(state.saves).toBe(0);
  expect(state.body).toMatchObject({
    owner: "octocat",
    owner_kind: "user",
    title: "Delivery",
    acknowledge_private_creation: true,
  });
  expect(state.markerBeforeCreate).toBe(true);
});

for (const device of ["desktop", "mobile"] as const)
  for (const theme of ["light", "dark"] as const)
    test(`guided private board setup frames ${device} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const state = await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const frame = async (stage: string) => {
        await capture(page, stage, theme, device, {
          producer: "studio/tests/github-projects.spec.ts",
        });
        if (!process.env.UPDATE_DOC_SCREENSHOTS)
          await page.screenshot({ path: info.outputPath(stage + ".png") });
      };
      await account(page, undefined, () => frame("github-projects-account"));
      await page.getByLabel("Board number").fill("9");
      await page
        .getByRole("button", { name: "Find private board", exact: true })
        .click();
      await expect(page.getByText("Delivery", { exact: true })).toBeVisible();
      await frame("github-projects-private-board");
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .getByLabel("GitHub board field", { exact: true })
        .selectOption("FIELD_1");
      for (const name of [
        "Queued",
        "Running",
        "Paused",
        "Needs reconciliation",
        "Failed",
        "Cancelled",
        "Interrupted",
      ])
        await page
          .getByLabel(`GitHub board status for ${name}`, { exact: true })
          .selectOption(mapping[name]);
      await page
        .getByLabel(
          "Allow forge to update only this field for linked issues and pull requests.",
        )
        .check();
      await expect(
        page.getByRole("button", { name: "Save board settings", exact: true }),
      ).toBeEnabled();
      await frame("github-projects-board-statuses");
      expect(state.proofs).toBe(1);
      expect(state.creates).toBe(0);
      expect(state.saves).toBe(0);
    });
