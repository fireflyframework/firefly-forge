/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  expect,
  test,
  type Locator,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { navigate } from "./navigation";

const project = "a".repeat(32),
  repository = "b".repeat(32);
const workspace = "00000000-0000-0000-0000-000000000002";
const dataset = "00000000-0000-0000-0000-000000000003";
const table = "00000000-0000-0000-0000-000000000004";
const object = "00000000-0000-0000-0000-000000000005";
const snapshot = "00000000-0000-0000-0000-000000000006";
const settings = {
  enabled: true,
  endpoint: "https://analytics.example",
  tenant_id: "fictional",
  workspace_id: workspace,
  dataset_id: dataset,
};
const capabilities = {
  catalog: true,
  table: true,
  exact_schema_object: true,
  full_table_schema: false,
  prepared_results: false,
  query_execution: false,
};
const path = `/api/v1/projects/${project}/analytics`;

type Write = { method: string; path: string; body: Record<string, unknown> };
async function fixture(
  page: Page,
  options: { admin?: boolean; revision?: number } = {},
) {
  const state = {
    revision: options.revision ?? 0,
    saved: { ...settings },
    conflict: false,
    authorized: true,
  };
  const calls: string[] = [],
    writes: Write[] = [],
    unexpected: string[] = [];
  const configuration = () => ({
    project_id: project,
    revision: state.revision,
    status: state.revision && state.saved.enabled ? "configured" : "disabled",
    agent_token_configured: state.revision > 0,
    configuration: state.revision ? state.saved : {},
    capabilities,
  });
  const observation = (data: unknown, digest: string) => ({
    project_id: project,
    configuration_revision: state.revision,
    tenant_id: state.saved.tenant_id,
    workspace_id: workspace,
    dataset_id: dataset,
    observed_at: "2026-10-17T00:00:00Z",
    response_digest: digest.repeat(64),
    snapshot_assurance: "reported_identity_only",
    data,
  });
  const item = {
    id: table,
    tenant_id: "fictional",
    workspace_id: workspace,
    dataset_id: dataset,
    name: "Stock",
    qualified_name: "warehouse.stock",
    description: "Fictional stock records",
    n_columns: 2,
    current_snapshot_id: snapshot,
  };
  await page.route("https://analytics.example/**", async (route) => {
    unexpected.push("Browser attempted direct Flyquery access");
    await route.abort("blockedbyclient");
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      method = request.method();
    if (
      method !== "GET" &&
      !(method === "PUT" && url.pathname === path + "/configuration")
    )
      unexpected.push(method + " " + url.pathname);
    if (url.pathname.endsWith("/auth/configuration"))
      return route.fulfill({
        json: {
          mode: options.admin === false ? "oidc" : "local_bootstrap",
          issuer:
            options.admin === false ? "https://identity.example.test" : "",
          cookie_secure: false,
        },
      });
    if (url.pathname.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name:
              options.admin === false
                ? "Fixture reader"
                : "Fixture administrator",
            email: null,
            authentication_method:
              options.admin === false ? "oidc_session" : "local_bootstrap",
          },
          global_admin: options.admin !== false,
          memberships:
            options.admin === false
              ? [{ project_id: project, role: "viewer", permissions: ["read"] }]
              : [],
          roles: { owner: ["read", "members"], viewer: ["read"] },
        },
      });
    if (url.pathname.endsWith("/projects"))
      return route.fulfill({
        json: [
          {
            id: project,
            definition: {
              name: "Stock product",
              components: [{ repository_id: repository }],
            },
          },
        ],
      });
    if (url.pathname.startsWith(path)) {
      calls.push(method + " " + url.pathname);
      if (method === "PUT" && url.pathname === path + "/configuration") {
        const body = request.postDataJSON();
        writes.push({ method, path: url.pathname, body });
        if (options.admin === false || !state.authorized)
          return route.fulfill({
            status: 403,
            json: { detail: "Current project authority required" },
          });
        if (state.conflict || body.expected_revision !== state.revision) {
          state.revision += 1;
          return route.fulfill({
            status: 409,
            json: { detail: "Conflict: " + body.agent_token },
          });
        }
        if (
          state.revision >= 32 &&
          !(
            state.revision === 32 &&
            state.saved.enabled &&
            body.enabled === false &&
            Object.keys(settings)
              .filter((key) => key !== "enabled")
              .every(
                (key) =>
                  body[key] === state.saved[key as keyof typeof settings],
              )
          )
        ) {
          unexpected.push("Invalid terminal write");
          return route.fulfill({
            status: 409,
            json: { detail: "Terminal binding" },
          });
        }
        state.revision += 1;
        state.saved = {
          enabled: body.enabled,
          endpoint: body.endpoint,
          tenant_id: body.tenant_id,
          workspace_id: body.workspace_id,
          dataset_id: body.dataset_id,
        };
        return route.fulfill({ json: configuration() });
      }
      if (method === "GET" && url.pathname === path + "/configuration") {
        if (options.admin === false) {
          unexpected.push("Reader requested administrator configuration");
          return route.fulfill({
            status: 403,
            json: { detail: "Administrator required" },
          });
        }
        return route.fulfill({ json: configuration() });
      }
      if (!state.authorized)
        return route.fulfill({
          status: 403,
          json: { detail: "Current project access revoked" },
        });
      if (method === "GET" && url.pathname === path + "/catalog")
        return route.fulfill({
          json: observation(
            { items: [item], total: 1, offset: 0, has_more: false },
            "c",
          ),
        });
      if (method === "GET" && url.pathname === path + "/tables/" + table)
        return route.fulfill({ json: observation(item, "d") });
      if (
        method === "GET" &&
        url.pathname === path + `/tables/${table}/objects/${object}`
      )
        return route.fulfill({
          json: observation(
            {
              id: object,
              table_id: table,
              snapshot_id: snapshot,
              tenant_id: "fictional",
              workspace_id: workspace,
              qualified_name: "warehouse.stock.quantity",
              kind: "column",
              description: "Reported available quantity",
              data_type: "integer",
              is_nullable: false,
            },
            "e",
          ),
        });
      unexpected.push(method + " " + url.pathname);
      return route.fulfill({
        status: 404,
        json: { detail: "Unsupported analytics route" },
      });
    }
    return route.fulfill({
      json: [
        "/runs",
        "/repositories",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((suffix) => url.pathname.endsWith(suffix))
        ? []
        : {},
    });
  });
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);
  await navigate(page, "Administration");
  await page
    .locator("main")
    .getByRole("button", { name: "Structured analytics", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Structured analytics", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Analytics project", exact: true })
      .getByLabel("Project", { exact: true }),
  ).toHaveValue(project);
  return { state, calls, writes, unexpected };
}
async function reachable(page: Page, control: Locator) {
  await control.scrollIntoViewIfNeeded();
  await control.focus();
  await expect(control).toBeFocused();
  await expect
    .poll(() =>
      control.evaluate((element) => {
        const rect = element.getBoundingClientRect(),
          header = document
            .querySelector(".workspace-main>header")
            ?.getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main>footer")
            ?.getBoundingClientRect();
        return (
          rect.width > 0 &&
          rect.height >= 40 &&
          rect.top >= Math.max(header?.bottom ?? 0, 0) - 1 &&
          rect.bottom <=
            Math.min(footer?.top ?? innerHeight, innerHeight) + 1 &&
          rect.left >= 0 &&
          rect.right <= innerWidth &&
          element.contains(
            document.elementFromPoint(
              rect.x + rect.width / 2,
              rect.y + rect.height / 2,
            ),
          )
        );
      }),
    )
    .toBe(true);
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    )
    .toBe(true);
}
async function noOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  for (const selector of [
    ".analytics-page",
    ".analytics-explorer",
    ".analytics-grid",
    ".analytics-inspector",
  ]) {
    const elements = page.locator(selector);
    for (const element of await elements.all())
      expect(
        await element.evaluate(
          (node) => node.scrollWidth <= node.clientWidth + 1,
        ),
        selector,
      ).toBe(true);
  }
}
async function frame(page: Page, info: TestInfo, name: string) {
  await noOverflow(page);
  await info.attach(name, {
    body: await page.screenshot(),
    contentType: "image/png",
  });
}
async function secretAbsent(page: Page, secret: string) {
  expect(await page.content()).not.toContain(secret);
  expect(
    await page.evaluate(() =>
      JSON.stringify({
        local: { ...localStorage },
        tab: { ...sessionStorage },
      }),
    ),
  ).not.toContain(secret);
}
async function next(page: Page, heading: string) {
  const button = page
    .locator(".analytics-wizard")
    .getByRole("button", { name: "Continue", exact: true });
  await reachable(page, button);
  await page.keyboard.press("Enter");
  await expect(page.locator(".analytics-wizard .wizard-heading h2")).toHaveText(
    heading,
  );
  await expect(
    page.locator(".analytics-wizard .wizard-heading h2"),
  ).toBeFocused();
}
async function enterWizard(page: Page, terminal = false) {
  await page
    .locator(".analytics-connection")
    .getByRole("button", {
      name: terminal ? "Disable connection" : "Set up Flyquery",
      exact: true,
    })
    .click();
  const wizard = page.locator(".analytics-wizard");
  await expect(
    wizard.getByRole("button", { name: "Continue", exact: true }),
  ).toBeDisabled();
  await wizard.getByRole("checkbox").check();
  await next(
    page,
    terminal ? "Disable the saved connection" : "Bind this project to Flyquery",
  );
}
async function fillScope(page: Page, secret: string) {
  await page.getByLabel("Flyquery HTTPS origin").fill(settings.endpoint);
  await page.getByLabel("Tenant identifier").fill(settings.tenant_id);
  await page.getByLabel("Workspace UUID").fill(workspace);
  await page.getByLabel("Dataset UUID").fill(dataset);
  await page.getByLabel("Scoped agent token", { exact: true }).fill(secret);
}
async function readTable(page: Page) {
  await page.getByRole("button", { name: "Read catalog", exact: true }).click();
  const tableButton = page
    .getByRole("navigation", { name: "Tables in the saved dataset" })
    .getByRole("button", { name: /Stock/ });
  await reachable(page, tableButton);
  await page.keyboard.press("Enter");
  await expect(
    page.getByLabel("Known schema-object UUID", { exact: true }),
  ).toBeEnabled();
  await page
    .getByLabel("Known schema-object UUID", { exact: true })
    .fill(object);
  const lookup = page.getByRole("button", {
    name: "Read schema object",
    exact: true,
  });
  await reachable(page, lookup);
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("region", { name: "Reported schema object" }),
  ).toContainText("warehouse.stock.quantity");
}

for (const theme of ["light", "dark"] as const) {
  test(`analytics admin wizard, explicit reads and CAS hold at 1440x900 ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    const { calls, state, writes, unexpected } = await fixture(page);
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await expect(
      page.getByRole("button", { name: "Read catalog", exact: true }),
    ).toBeDisabled();
    await enterWizard(page);
    const configurationReads = () =>
      calls.filter((call) => call === "GET " + path + "/configuration").length;
    const initialConfigurationReads = configurationReads();
    expect(initialConfigurationReads).toBeGreaterThan(0);
    const secret = "owned-analytics-browser-replacement";
    await fillScope(page, secret);
    await next(page, "Review the project connection");
    await expect(page.locator(".analytics-review")).toContainText(workspace);
    await expect(page.locator(".analytics-review")).toContainText(
      "Expected saved revision: 0",
    );
    await secretAbsent(page, secret);
    expect(writes).toEqual([]);
    expect(calls.filter((call) => call.endsWith("/catalog"))).toEqual([]);
    const save = page
      .locator(".analytics-wizard")
      .getByRole("button", { name: "Save connection", exact: true });
    await reachable(page, save);
    await frame(page, info, "admin-review-before-explicit-save");
    await page.keyboard.press("Enter");
    await expect(
      page.locator(".analytics-notice-text[role=status]"),
    ).toContainText("Configuration saved");
    expect(writes).toEqual([
      {
        method: "PUT",
        path: path + "/configuration",
        body: { ...settings, agent_token: secret, expected_revision: 0 },
      },
    ]);
    await secretAbsent(page, secret);
    expect(calls.filter((call) => call.endsWith("/catalog"))).toEqual([]);
    await readTable(page);
    expect(calls.filter((call) => call.endsWith("/catalog"))).toHaveLength(1);
    await frame(page, info, "admin-read-only-explorer");
    state.conflict = true;
    await page
      .getByRole("button", { name: "Edit connection", exact: true })
      .click();
    await page.locator(".analytics-wizard").getByRole("checkbox").check();
    await next(page, "Bind this project to Flyquery");
    await expect(
      page.getByLabel("Scoped agent token", { exact: true }),
    ).toHaveValue("");
    const secondSecret = "owned-analytics-browser-conflict";
    await page.getByLabel("Tenant identifier").fill("changed-draft");
    await page
      .getByLabel("Scoped agent token", { exact: true })
      .fill(secondSecret);
    await next(page, "Review the project connection");
    await page
      .locator(".analytics-wizard")
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await expect(
      page.locator(".analytics-page .error[role=alert]"),
    ).toContainText("Save not confirmed");
    await expect(
      page
        .locator(".analytics-wizard")
        .getByRole("button", { name: "Save connection", exact: true }),
    ).toBeDisabled();
    await secretAbsent(page, secondSecret);
    expect(writes).toHaveLength(2);
    expect(writes[1].body).toEqual({
      ...settings,
      tenant_id: "changed-draft",
      agent_token: secondSecret,
      expected_revision: 1,
    });
    await frame(page, info, "admin-conflict-held-without-retry");
    const reload = page.getByRole("button", {
      name: "Reload saved configuration",
      exact: true,
    });
    await reachable(page, reload);
    expect(configurationReads()).toBe(initialConfigurationReads);
    await page.keyboard.press("Enter");
    await expect(page.locator(".analytics-connection")).toContainText(
      "revision 2",
    );
    await expect(
      page.getByRole("button", { name: "Edit connection", exact: true }),
    ).toBeEnabled();
    expect(writes).toHaveLength(2);
    expect(configurationReads()).toBe(initialConfigurationReads + 1);
    expect(unexpected).toEqual([]);
    await noOverflow(page);
  });

  test(`analytics reader current-authority refusal and unavailable queries at 390x844 ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    const { calls, writes, state, unexpected } = await fixture(page, {
      admin: false,
      revision: 7,
    });
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await expect(
      page.getByRole("button", { name: /Set up Flyquery|Edit connection/ }),
    ).toHaveCount(0);
    await expect(
      page.getByLabel("Scoped agent token", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText(
        /Full field discovery and prepared results are unavailable/,
      ),
    ).toBeVisible();
    expect(calls).toEqual([]);
    await readTable(page);
    expect(calls).toEqual([
      "GET " + path + "/catalog",
      "GET " + path + "/tables/" + table,
      "GET " + path + `/tables/${table}/objects/${object}`,
    ]);
    expect(writes).toEqual([]);
    await expect(
      page.getByRole("button", { name: /Run SQL|Run query|Prepared result/ }),
    ).toHaveCount(0);
    await frame(page, info, "reader-exact-object-and-wrapped-scope");
    state.authorized = false;
    const read = page.getByRole("button", {
      name: "Read catalog",
      exact: true,
    });
    await reachable(page, read);
    await page.keyboard.press("Enter");
    await expect(
      page.locator(".analytics-page .error[role=alert]"),
    ).toContainText("Current project access does not allow this read");
    await expect(
      page.getByRole("region", { name: "Reported schema object" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("navigation", { name: "Tables in the saved dataset" }),
    ).toHaveCount(0);
    expect(calls).toHaveLength(4);
    expect(
      calls.every(
        (call) =>
          call.startsWith("GET ") &&
          !call.includes("/configuration") &&
          !call.includes("/prepared") &&
          !call.includes("/queries"),
      ),
    ).toBe(true);
    expect(writes).toEqual([]);
    expect(unexpected).toEqual([]);
    await frame(page, info, "reader-access-revocation-clears-old-observation");
  });

  test(`analytics terminal 32 disable remains reachable and 33 cannot reenable at 320x400 ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width: 320, height: 400 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    const { writes, calls, unexpected } = await fixture(page, { revision: 32 });
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await enterWizard(page, true);
    for (const label of [
      "Flyquery HTTPS origin",
      "Tenant identifier",
      "Workspace UUID",
      "Dataset UUID",
    ])
      await expect(page.getByLabel(label, { exact: true })).toBeDisabled();
    await expect(
      page.getByLabel("Enable project readers to inspect this scope"),
    ).toBeDisabled();
    const token = page.getByLabel("Scoped agent token", { exact: true });
    await expect(token).toBeEnabled();
    await reachable(page, token);
    const secret = "owned-analytics-terminal-browser";
    await page.keyboard.insertText(secret);
    await next(page, "Review terminal disable");
    await expect(page.locator(".analytics-review")).toContainText(
      "Disable read-only access",
    );
    await expect(page.locator(".analytics-review")).toContainText(workspace);
    await secretAbsent(page, secret);
    const disable = page
      .locator(".analytics-wizard")
      .getByRole("button", { name: "Disable connection", exact: true });
    await reachable(page, disable);
    await frame(page, info, "short-phone-terminal-review-keyboard-action");
    await page.keyboard.press("Enter");
    await expect(page.locator(".analytics-connection")).toContainText(
      "Disabled · revision 33",
    );
    await expect(
      page.getByRole("button", { name: "Connection disabled", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: /Set up Flyquery|Edit connection/ }),
    ).toHaveCount(0);
    await expect(
      page.getByLabel("Scoped agent token", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Read catalog", exact: true }),
    ).toBeDisabled();
    await secretAbsent(page, secret);
    expect(writes).toEqual([
      {
        method: "PUT",
        path: path + "/configuration",
        body: {
          ...settings,
          enabled: false,
          agent_token: secret,
          expected_revision: 32,
        },
      },
    ]);
    const reload = page.getByRole("button", {
      name: "Reload saved configuration",
      exact: true,
    });
    await reachable(page, reload);
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("button", { name: "Connection disabled", exact: true }),
    ).toBeDisabled();
    expect(writes).toHaveLength(1);
    expect(calls.filter((call) => call.endsWith("/catalog"))).toEqual([]);
    expect(unexpected).toEqual([]);
    await frame(page, info, "short-phone-terminal-receipt-no-reenable");
  });
}
