/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

// Controlled native API shapes. Browser replay does not execute campaign work.
const projectId = "a".repeat(32),
  campaignId = "b".repeat(32);
const component = {
  id: "api",
  name: "Inventory API",
  repository_id: "c".repeat(32),
  path: ".",
  responsibility: "Inventory",
  kind: "api",
  backend: "pyfly",
  frontend: "none",
  verification: [],
  depends_on: [],
};
const project = {
  id: projectId,
  revision: 1,
  digest: "d".repeat(64),
  definition: {
    name: "Warehouse inventory",
    description: "Controlled execution policy",
    topology: "monorepo",
    owners: ["Product team"],
    components: [component],
  },
};
const secondProject = {
  ...project,
  id: "6".repeat(32),
  definition: { ...project.definition, name: "Distribution inventory" },
};
const policy = () => ({
  version: 1,
  project_id: projectId,
  policy: { enabled: true, dispatch_window: 50, max_parallel_runs: 4 },
  digest: null as string | null,
  effective_digest: "e".repeat(64),
  request_id: null as string | null,
  permissions: { configure: true },
});
const campaign = () => ({
  id: campaignId,
  project_id: projectId,
  status: "paused",
  created_at: "2026-10-25T00:00:00Z",
  control: null,
  launch: { publish: false },
  execution_budget_state: {
    version: 1,
    granted_dispatches: 50,
    consumed_dispatches: 50,
    remaining_dispatches: 0,
    max_parallel_runs: 4,
  },
  children: [
    {
      current: true,
      component_id: "api",
      run_id: "1".repeat(32),
      status: "paused",
    },
    {
      current: true,
      component_id: "web",
      run_id: "2".repeat(32),
      status: "interrupted",
    },
  ],
  journal: { verified: true, head: "f".repeat(64) },
});
async function openProject(
  page: Page,
  options: {
    configure?: boolean;
    save?: number | "lost";
    renew?: number | "lost";
    unverified?: boolean;
    disabled?: boolean;
    collapsed?: boolean;
    anotherProject?: boolean;
    holdSave?: boolean;
  } = {},
) {
  let current = policy(),
    active = campaign();
  current.permissions.configure = options.configure ?? true;
  if (options.disabled) current.policy.enabled = false;
  if (options.unverified) active.journal.verified = false;
  let releaseSave: (() => void) | undefined;
  const state = {
    policyReads: 0,
    initialPolicyReads: 0,
    releaseSave: () => releaseSave?.(),
    denyConfigure: () => {
      current = { ...current, permissions: { configure: false } };
    },
    policyWrites: [] as Record<string, unknown>[],
    renewals: [] as Record<string, unknown>[],
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/campaign-execution-policy")) {
      if (options.anotherProject && path.includes(secondProject.id)) {
        state.policyReads++;
        return route.fulfill({
          json: {
            ...policy(),
            project_id: secondProject.id,
            policy: {
              enabled: true,
              dispatch_window: 12,
              max_parallel_runs: 2,
            },
          },
        });
      }
      if (req.method() === "GET") {
        state.policyReads++;
        return route.fulfill({ json: current });
      }
      const body = req.postDataJSON();
      state.policyWrites.push(body);
      if (options.holdSave)
        await new Promise<void>((resolve) => {
          releaseSave = resolve;
        });
      if (options.save === "lost") return route.abort("failed");
      if (typeof options.save === "number") {
        current = {
          ...current,
          digest: "9".repeat(64),
          effective_digest: "9".repeat(64),
          request_id: "9".repeat(32),
          policy: { ...current.policy, dispatch_window: 80 },
        };
        return route.fulfill({
          status: options.save,
          json: { detail: "private denial must not render" },
        });
      }
      current = {
        ...current,
        policy: body.policy,
        request_id: body.request_id,
        digest: "8".repeat(64),
        effective_digest: "8".repeat(64),
      };
      return route.fulfill({ json: current });
    }
    if (path.endsWith("/renew-budget")) {
      const body = req.postDataJSON();
      state.renewals.push(body);
      if (options.renew === "lost") return route.abort("failed");
      if (typeof options.renew === "number")
        return route.fulfill({
          status: options.renew,
          json: { detail: "private denial must not render" },
        });
      active = {
        ...active,
        journal: { verified: true, head: "7".repeat(64) },
        execution_budget_state: {
          ...active.execution_budget_state,
          granted_dispatches: 75,
          remaining_dispatches: 25,
        },
      };
      return route.fulfill({ json: active });
    }
    if (path.endsWith(`/campaigns/${campaignId}`))
      return route.fulfill({ json: active });
    if (path.endsWith("/campaigns")) return route.fulfill({ json: [active] });
    const json = path.endsWith("/repositories")
      ? [
          {
            id: component.repository_id,
            name: "Inventory API",
            provider: "git",
            url: "/fixture",
            default_branch: "main",
          },
        ]
      : path.endsWith("/projects")
        ? options.anotherProject
          ? [project, secondProject]
          : [project]
        : [projectId, secondProject.id].some((id) =>
              path.endsWith(`/projects/${id}`),
            )
          ? {
              ...(path.endsWith(secondProject.id) ? secondProject : project),
              components: [
                {
                  ...component,
                  latest_status: "not_started",
                  repository_run_count: 0,
                  runs: [],
                },
              ],
              counts: {},
            }
          : path.endsWith("/health")
            ? { status: "ok", execution_ready: false }
            : [];
    return route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("controlled-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product projects");
  await expect(
    page.getByRole("heading", { name: "Warehouse inventory", exact: true }),
  ).toBeVisible();
  const panel = page.getByRole("region", {
    name: "Campaign execution",
    exact: true,
  });
  await expect(panel.getByText(/50 dispatches per window/)).toBeVisible();
  state.initialPolicyReads = state.policyReads;
  if (options.collapsed) return { state, panel };
  expect(state.initialPolicyReads).toBeGreaterThanOrEqual(1);
  await panel
    .getByRole("button", {
      name: options.configure === false ? "View settings" : "Configure",
      exact: true,
    })
    .click();
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveValue(
    "50",
  );
  return { state, panel };
}
test("saves an explicit positive execution window above old limits with the current revision and readback", async ({
  page,
}) => {
  const { state, panel } = await openProject(page);
  await panel.getByLabel("Dispatches per execution window").fill("2048");
  await panel.getByLabel("Parallel runs").fill("64");
  await panel
    .getByRole("button", { name: "Save execution settings", exact: true })
    .click();
  await expect(panel.getByRole("status")).toContainText(
    "Execution settings saved",
  );
  expect(state.policyWrites).toHaveLength(1);
  expect(state.policyWrites[0]).toMatchObject({
    expected_previous_digest: null,
    policy: { enabled: true, dispatch_window: 2048, max_parallel_runs: 64 },
  });
  expect(state.policyWrites[0].request_id).toMatch(/^[a-f0-9]{32}$/);
  expect(state.policyReads).toBe(state.initialPolicyReads + 1);
  await expect(panel.getByText("Saved revision 88888888")).toBeVisible();
  await expect(panel.getByText(/Any number of components/)).toBeVisible();
});
test("invalid or empty integer drafts do not send settings", async ({
  page,
}) => {
  const { state, panel } = await openProject(page);
  for (const value of ["", "0", "-1", "1.5"]) {
    await panel.getByLabel("Dispatches per execution window").fill(value);
    await expect(
      panel.getByRole("button", {
        name: "Save execution settings",
        exact: true,
      }),
    ).toBeDisabled();
  }
  expect(state.policyWrites).toHaveLength(0);
});
test("readers see saved policy but cannot configure it", async ({ page }) => {
  const { panel } = await openProject(page, { configure: false });
  await expect(
    panel.getByLabel("Dispatches per execution window"),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "Save execution settings", exact: true }),
  ).toHaveCount(0);
  await expect(panel.getByText(/A factory administrator/)).toBeVisible();
});
for (const status of [403, 409, "lost"] as const)
  test(`settings ${status} refetches current policy without retrying or exposing server detail`, async ({
    page,
  }) => {
    const { state, panel } = await openProject(page, { save: status });
    await panel.getByLabel("Dispatches per execution window").fill("60");
    await panel
      .getByRole("button", { name: "Save execution settings", exact: true })
      .click();
    await expect(panel.getByRole("alert")).toContainText(
      status === "lost" ? "not confirmed" : "declined",
    );
    expect(state.policyWrites).toHaveLength(1);
    expect(state.policyReads).toBe(state.initialPolicyReads + 1);
    await expect(
      panel.getByLabel("Dispatches per execution window"),
    ).toHaveValue(status === "lost" ? "50" : "80");
    await expect(panel).not.toContainText("private denial");
  });
test("renewal reviews held reservations and confirms additional work without resetting consumption", async ({
  page,
}) => {
  const { state, panel } = await openProject(page);
  await panel
    .getByRole("button", { name: "Review campaign reservations", exact: true })
    .click();
  await panel.getByLabel("Campaign to review").selectOption(campaignId);
  await expect(panel.getByText("2 held reservations")).toBeVisible();
  await expect(
    panel.getByText(
      "Paused and interrupted runs keep their reservations. Additional dispatches do not replay unknown effects or reset past consumption.",
      { exact: true },
    ),
  ).toBeVisible();
  await panel.getByLabel("Additional dispatches").fill("25");
  await panel
    .getByRole("button", { name: "Review renewal", exact: true })
    .click();
  const dialog = panel.getByRole("dialog", {
    name: "Confirm campaign renewal",
  });
  await expect(dialog).toContainText("50 dispatches already consumed");
  expect(state.renewals).toHaveLength(0);
  await dialog
    .getByRole("button", {
      name: "Confirm 25 additional dispatches",
      exact: true,
    })
    .click();
  await expect(panel.getByRole("status")).toContainText(
    "Additional execution window confirmed",
  );
  expect(state.renewals).toHaveLength(1);
  expect(state.renewals[0]).toMatchObject({
    expected_journal_head: "f".repeat(64),
    expected_policy_digest: "e".repeat(64),
    additional_dispatches: 25,
    max_parallel_runs: 4,
  });
  await expect(panel.getByText("50 consumed", { exact: true })).toBeVisible();
  await expect(panel.getByText("25 remaining", { exact: true })).toBeVisible();
});
test("unverified campaign history cannot create a renewal", async ({
  page,
}) => {
  const { state, panel } = await openProject(page, { unverified: true });
  await panel
    .getByRole("button", { name: "Review campaign reservations", exact: true })
    .click();
  await expect(panel.getByRole("alert")).toContainText("could not be verified");
  await expect(
    panel.getByRole("button", { name: "Review renewal", exact: true }),
  ).toHaveCount(0);
  expect(state.renewals).toHaveLength(0);
});

for (const status of [403, 409, "lost"] as const)
  test(`renewal ${status} holds the uncertain operation and rereads without replay`, async ({
    page,
  }) => {
    const { state, panel } = await openProject(page, { renew: status });
    await panel
      .getByRole("button", {
        name: "Review campaign reservations",
        exact: true,
      })
      .click();
    await panel.getByLabel("Campaign to review").selectOption(campaignId);
    await panel.getByLabel("Additional dispatches").fill("25");
    await panel
      .getByRole("button", { name: "Review renewal", exact: true })
      .click();
    await panel
      .getByRole("button", {
        name: "Confirm 25 additional dispatches",
        exact: true,
      })
      .click();
    await expect(panel.getByRole("alert")).toContainText(
      status === "lost" ? "not confirmed" : "declined",
    );
    expect(state.renewals).toHaveLength(1);
    await expect(panel.getByText("50 consumed", { exact: true })).toBeVisible();
    await expect(panel.getByText("0 remaining", { exact: true })).toBeVisible();
    await expect(panel.getByRole("dialog")).toHaveCount(0);
    await expect(panel).not.toContainText("private denial");
  });
test("disabled project dispatches keep reservations visible without offering renewal", async ({
  page,
}) => {
  const { state, panel } = await openProject(page, { disabled: true });
  await panel
    .getByRole("button", { name: "Review campaign reservations", exact: true })
    .click();
  await panel.getByLabel("Campaign to review").selectOption(campaignId);
  await expect(panel.getByText("2 held reservations")).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "Review renewal", exact: true }),
  ).toHaveCount(0);
  expect(state.renewals).toHaveLength(0);
});
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const)
    test(`execution policy fits ${width}px ${theme} with a visible confirmation`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme: theme });
      const { panel, state } = await openProject(page, { collapsed: true });
      await expect(
        panel.getByLabel("Dispatches per execution window"),
      ).toHaveCount(0);
      const compact = await panel.boundingBox();
      expect(compact!.height).toBeLessThanOrEqual(width === 390 ? 260 : 180);
      if (process.env.FORGE_CAMPAIGN_CAPTURE_DIR) {
        await panel.evaluate((node) => {
          const main = node.closest("main")!;
          main.scrollTop +=
            node.getBoundingClientRect().top -
            main.getBoundingClientRect().top -
            20;
        });
        await page.screenshot({
          path: `${process.env.FORGE_CAMPAIGN_CAPTURE_DIR}/campaign-collapsed-${width}-${theme}.png`,
        });
      }
      await panel
        .getByRole("button", { name: "Configure", exact: true })
        .click();
      if (width === 1440) {
        const save = await panel
          .getByRole("button", { name: "Save execution settings", exact: true })
          .boundingBox();
        expect(save!.width).toBeLessThan(compact!.width / 2);
      }
      if (process.env.FORGE_CAMPAIGN_CAPTURE_DIR) {
        await panel.evaluate((node) => {
          const main = node.closest("main")!;
          main.scrollTop +=
            node.getBoundingClientRect().top -
            main.getBoundingClientRect().top -
            20;
        });
        await page.screenshot({
          path: `${process.env.FORGE_CAMPAIGN_CAPTURE_DIR}/campaign-expanded-${width}-${theme}.png`,
        });
      }
      await panel
        .getByRole("button", {
          name: "Review campaign reservations",
          exact: true,
        })
        .click();
      await panel.getByLabel("Campaign to review").selectOption(campaignId);
      await panel.getByLabel("Additional dispatches").fill("25");
      await panel
        .getByRole("button", { name: "Review renewal", exact: true })
        .click();
      await expect(
        panel.getByRole("dialog", { name: "Confirm campaign renewal" }),
      ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      if (process.env.FORGE_CAMPAIGN_CAPTURE_DIR) {
        await panel.getByRole("dialog").scrollIntoViewIfNeeded();
        await page.screenshot({
          path: `${process.env.FORGE_CAMPAIGN_CAPTURE_DIR}/campaign-renewal-viewport-${width}-${theme}.png`,
        });
      }
      await panel
        .getByRole("button", { name: "Cancel renewal", exact: true })
        .click();
      await expect(panel.getByRole("dialog")).toHaveCount(0);
      expect(state.renewals).toHaveLength(0);
    });

test("a double settings activation sends one native CAS request", async ({
  page,
}) => {
  const { state, panel } = await openProject(page);
  await panel.getByLabel("Dispatches per execution window").fill("100");
  await panel
    .getByRole("button", { name: "Save execution settings", exact: true })
    .evaluate((node) => {
      (node as HTMLButtonElement).click();
      (node as HTMLButtonElement).click();
    });
  await expect(panel.getByRole("status")).toContainText(
    "Execution settings saved",
  );
  expect(state.policyWrites).toHaveLength(1);
});
test("a double renewal confirmation appends one additional window", async ({
  page,
}) => {
  const { state, panel } = await openProject(page);
  await panel
    .getByRole("button", { name: "Review campaign reservations", exact: true })
    .click();
  await panel.getByLabel("Campaign to review").selectOption(campaignId);
  await panel.getByLabel("Additional dispatches").fill("25");
  await panel
    .getByRole("button", { name: "Review renewal", exact: true })
    .click();
  await panel
    .getByRole("button", {
      name: "Confirm 25 additional dispatches",
      exact: true,
    })
    .evaluate((node) => {
      (node as HTMLButtonElement).click();
      (node as HTMLButtonElement).click();
    });
  await expect(panel.getByRole("status")).toContainText(
    "Additional execution window confirmed",
  );
  expect(state.renewals).toHaveLength(1);
});

test("default summary is compact and its configuration disclosure works with the keyboard", async ({
  page,
}) => {
  const { panel, state } = await openProject(page, { collapsed: true });
  await expect(
    panel.getByText("50 dispatches per window · 4 parallel runs · Enabled", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveCount(
    0,
  );
  const configure = panel.getByRole("button", {
    name: "Configure",
    exact: true,
  });
  await configure.focus();
  await page.keyboard.press("Enter");
  await expect(configure).toHaveAttribute("aria-expanded", "true");
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveValue(
    "50",
  );
  await configure.focus();
  await page.keyboard.press("Space");
  await expect(configure).toHaveAttribute("aria-expanded", "false");
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveCount(
    0,
  );
  expect(state.policyWrites).toHaveLength(0);
  expect(state.renewals).toHaveLength(0);
});
test("closing configuration keeps its unsaved values without changing the saved summary", async ({
  page,
}) => {
  const { panel, state } = await openProject(page);
  await panel.getByLabel("Dispatches per execution window").fill("2048");
  await panel.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(
    panel.getByText("50 dispatches per window · 4 parallel runs · Enabled", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByText("Changes not saved", { exact: true }),
  ).toBeVisible();
  await panel.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveValue(
    "2048",
  );
  expect(state.policyWrites).toHaveLength(0);
});

test("switching projects discards the old local draft and closes configuration", async ({
  page,
}) => {
  const { panel, state } = await openProject(page, { anotherProject: true });
  await panel.getByLabel("Dispatches per execution window").fill("2048");
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(secondProject.id);
  const fresh = page.getByRole("region", {
    name: "Campaign execution",
    exact: true,
  });
  await expect(
    fresh.getByText("12 dispatches per window · 2 parallel runs · Enabled", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(fresh.getByLabel("Dispatches per execution window")).toHaveCount(
    0,
  );
  await fresh.getByRole("button", { name: "Configure", exact: true }).click();
  await expect(fresh.getByLabel("Dispatches per execution window")).toHaveValue(
    "12",
  );
  await expect(
    fresh.getByText("Changes not saved", { exact: true }),
  ).toHaveCount(0);
  expect(state.policyWrites).toHaveLength(0);
});

async function scopeHarness(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/scope-barrier?**", (route) =>
    route.fulfill({ json: { settled: true } }),
  );
  await page.addScriptTag({
    type: "module",
    content: `
    import React from "/node_modules/.vite/deps/react.js";
    import ReactDOM from "/node_modules/.vite/deps/react-dom_client.js";
    import {CampaignExecutionSettings} from "/src/CampaignExecutionSettings.tsx";
    const host = document.createElement("section");
    host.setAttribute("aria-label", "Scope control fixture");
    host.style.cssText = "position:fixed;inset:0;z-index:10000;overflow:auto;background:var(--surface);padding:24px";
    document.body.append(host);
    function Control() {
      const [epoch, setEpoch] = React.useState(0), [changed, setChanged] = React.useState(false), [reader, setReader] = React.useState(false);
      React.useEffect(() => { void fetch("/scope-barrier?epoch=" + epoch); }, [epoch]);
      const connection = {base: "/api/v1", token: changed ? "controlled-changed-scope-token" : "controlled-initial-scope-token"};
      const session = {authenticated: true, global_admin: !reader, user: {id: "f".repeat(32), display_name: "Test operator", email: null, authentication_method: "local_bootstrap"}, memberships: [], roles: {}};
      return React.createElement(React.Fragment, null,
        React.createElement("button", {onClick: () => setEpoch(value => value + 1)}, "Rerender equivalent props"),
        React.createElement("button", {onClick: () => {setChanged(true);setEpoch(value => value + 1)}}, "Change credentials"),
        React.createElement("button", {onClick: () => {setReader(true);setEpoch(value => value + 1)}}, "Revoke configuration role"),
        React.createElement(CampaignExecutionSettings, {connection, projectId: "a".repeat(32), session}));
    }
    ReactDOM.createRoot(host).render(React.createElement(Control));
  `,
  });
  const harness = page.getByRole("region", {
    name: "Scope control fixture",
    exact: true,
  });
  const panel = harness.getByRole("region", {
    name: "Campaign execution",
    exact: true,
  });
  await expect(
    panel.getByText("50 dispatches per window · 4 parallel runs · Enabled", {
      exact: true,
    }),
  )
    .toBeVisible()
    .catch((cause) => {
      throw new Error(`${String(cause)}\nBrowser errors: ${errors.join("; ")}`);
    });
  return { harness, panel };
}
test("equivalent new connection and session objects preserve an unsaved draft without rereading", async ({
  page,
}) => {
  const { state } = await openProject(page, { collapsed: true });
  const { harness, panel } = await scopeHarness(page);
  await panel.getByRole("button", { name: "Configure", exact: true }).click();
  await panel.getByLabel("Dispatches per execution window").fill("2048");
  const before = state.policyReads;
  const settled = page.waitForResponse((response) =>
    response.url().endsWith("/scope-barrier?epoch=1"),
  );
  await harness
    .getByRole("button", { name: "Rerender equivalent props", exact: true })
    .click();
  await settled;
  await page.waitForLoadState("networkidle");
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveValue(
    "2048",
  );
  expect(state.policyReads).toBe(before);
  expect(state.policyWrites).toHaveLength(0);
});
test("equivalent prop refresh cannot unlock a pending save or send a second PUT", async ({
  page,
}) => {
  const { state } = await openProject(page, {
    collapsed: true,
    holdSave: true,
  });
  const { harness, panel } = await scopeHarness(page);
  await panel.getByRole("button", { name: "Configure", exact: true }).click();
  await panel.getByLabel("Dispatches per execution window").fill("100");
  await panel
    .getByRole("button", { name: "Save execution settings", exact: true })
    .click();
  await expect.poll(() => state.policyWrites.length).toBe(1);
  const before = state.policyReads;
  const settled = page.waitForResponse((response) =>
    response.url().endsWith("/scope-barrier?epoch=1"),
  );
  await harness
    .getByRole("button", { name: "Rerender equivalent props", exact: true })
    .click();
  await settled;
  await expect(
    panel.getByLabel("Dispatches per execution window"),
  ).toBeDisabled();
  expect(state.policyReads).toBe(before);
  await panel
    .getByRole("button", { name: "Save execution settings", exact: true })
    .evaluate((node) => {
      (node as HTMLButtonElement).click();
    });
  expect(state.policyWrites).toHaveLength(1);
  state.releaseSave();
  await expect(panel.getByRole("status")).toContainText(
    "Execution settings saved",
  );
  expect(state.policyReads).toBe(before + 1);
  expect(state.policyWrites).toHaveLength(1);
});
test("a real credential change closes the old editor and ignores its late save callback", async ({
  page,
}) => {
  const { state } = await openProject(page, {
    collapsed: true,
    holdSave: true,
  });
  const { harness, panel } = await scopeHarness(page);
  await panel.getByRole("button", { name: "Configure", exact: true }).click();
  await panel.getByLabel("Dispatches per execution window").fill("100");
  await panel
    .getByRole("button", { name: "Save execution settings", exact: true })
    .click();
  await expect.poll(() => state.policyWrites.length).toBe(1);
  await harness
    .getByRole("button", { name: "Change credentials", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "Configure", exact: true }),
  ).toHaveAttribute("aria-expanded", "false");
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveCount(
    0,
  );
  const beforeRelease = state.policyReads;
  state.releaseSave();
  await page.waitForLoadState("networkidle");
  await expect(panel.getByRole("status")).toHaveCount(0);
  expect(state.policyReads).toBe(beforeRelease);
  expect(state.policyWrites).toHaveLength(1);
});

test("a real role change clears the draft and rereads current configuration permission", async ({
  page,
}) => {
  const { state } = await openProject(page, { collapsed: true });
  const { harness, panel } = await scopeHarness(page);
  await panel.getByRole("button", { name: "Configure", exact: true }).click();
  await panel.getByLabel("Dispatches per execution window").fill("2048");
  state.denyConfigure();
  await harness
    .getByRole("button", { name: "Revoke configuration role", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "View settings", exact: true }),
  ).toHaveAttribute("aria-expanded", "false");
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveCount(
    0,
  );
  await panel
    .getByRole("button", { name: "View settings", exact: true })
    .click();
  await expect(panel.getByLabel("Dispatches per execution window")).toHaveValue(
    "50",
  );
  await expect(
    panel.getByLabel("Dispatches per execution window"),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "Save execution settings", exact: true }),
  ).toHaveCount(0);
  expect(state.policyWrites).toHaveLength(0);
});
