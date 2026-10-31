/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
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
              options: [
                { id: "TODO", name: "To do" },
                { id: "REVIEW", name: "In review" },
              ],
            },
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
async function account(page: Page, screenshot?: string) {
  await page
    .getByRole("button", { name: "Set up GitHub Projects", exact: true })
    .click();
  await page.getByLabel("Project OAuth connection").selectOption("delivery");
  await page
    .getByRole("button", { name: "Verify connected account", exact: true })
    .click();
  await expect(
    page.getByText("Connected as octocat", { exact: true }),
  ).toBeVisible();
  if (screenshot) await page.screenshot({ path: screenshot });
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
    page.getByRole("button", { name: "Save board settings", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("Managed field", { exact: true })
    .selectOption("FIELD_1");
  for (const name of [
    "Queued",
    "Running",
    "Paused",
    "Needs reconciliation",
    "Failed",
    "Cancelled",
    "Interrupted",
    "Succeeded",
  ])
    await page.getByLabel(name, { exact: true }).selectOption("REVIEW");
  await page
    .getByLabel(
      "I authorize Forge to manage this field on verified issue and pull request cards.",
    )
    .check();
  for (const theme of ["light", "dark"]) {
    await page.evaluate(
      (value) => (document.documentElement.dataset.theme = value),
      theme,
    );
    for (const [width, height] of [
      [1440, 900],
      [390, 844],
      [320, 568],
    ]) {
      await page.setViewportSize({ width, height });
      const geometry = await page
        .getByRole("dialog", { name: "GitHub Projects setup", exact: true })
        .evaluate((dialog) => {
          const r = dialog.getBoundingClientRect(),
            actions = dialog
              .querySelector(".wizard-actions")!
              .getBoundingClientRect(),
            field = dialog.querySelector(".wizard-field-scroll")!;
          return {
            width: r.width <= innerWidth,
            height: r.height <= innerHeight,
            actions: actions.bottom <= r.bottom,
            overflow: field.scrollWidth > field.clientWidth,
          };
        });
      expect(geometry).toEqual({
        width: true,
        height: true,
        actions: true,
        overflow: false,
      });
      await page
        .locator(".github-projects-dialog .wizard-field-scroll")
        .evaluate((element) => {
          element.scrollTop = 0;
        });
      if (width !== 320)
        await page.screenshot({
          path: info.outputPath(`board-mapping-${width}-${theme}.png`),
        });
    }
  }
  await page
    .getByRole("button", { name: "Save board settings", exact: true })
    .click();
  await expect.poll(() => state.saves).toBe(1);
  expect(state.body?.state_options).toEqual(
    Object.fromEntries(
      [
        "queued",
        "running",
        "paused",
        "needs_reconciliation",
        "failed",
        "cancelled",
        "interrupted",
        "succeeded",
      ].map((key) => [key, "REVIEW"]),
    ),
  );
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
test("a foreign account proof clears setup authority and keyboard close restores focus", async ({
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
      .getByRole("dialog", { name: "GitHub Projects setup", exact: true })
      .getByRole("alert"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Close GitHub Projects setup",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Continue", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("dialog", { name: "GitHub Projects setup", exact: true }),
  ).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Set up GitHub Projects", exact: true }),
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
    await page.evaluate(
      (value) => (document.documentElement.dataset.theme = value),
      theme,
    );
    await page.screenshot({
      path: info.outputPath(`board-creation-${width}-${theme}.png`),
    });
  }
  await page
    .getByRole("button", { name: "Create private personal board", exact: true })
    .click();
  await expect(page.getByLabel("Managed field", { exact: true })).toBeVisible();
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
