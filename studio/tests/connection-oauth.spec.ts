/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Contract fixtures for native OAuth lifecycle and create-only registration; not provider acceptance.
import { expect, test, type Page } from "@playwright/test";
import { navigate, expectAuthenticated } from "./navigation";
import { mkdirSync } from "node:fs";
import { join } from "node:path";

async function fixture(
  page: Page,
  admitted = false,
  admin = true,
  options: {
    status?: string;
    rejected?: boolean;
    unknownRefresh?: boolean;
    foreignHandoff?: boolean;
    malformed?: boolean;
    conflictingSave?: boolean;
    conflictingCredential?: boolean;
    concurrentCreator?: boolean;
    policyChanged?: boolean;
    policyChangedAfterUnknown?: boolean;
    localDevelopment?: boolean;
    losePolicyResponse?: boolean;
    historicalPolicyReplay?: boolean;
    executionUnavailable?: boolean;
    beforeSave?: () => Promise<void>;
    newSetup?: boolean;
    denyActions?: boolean;
    closedReceipt?: boolean;
    mismatchedReceipt?: boolean;
    onRead?: (path: string) => void;
    secondAccount?: boolean;
  } = {},
) {
  const writes: { path: string; body: unknown }[] = [];
  let status = options.status ?? "authorization_required",
    grantRevision = 1,
    projectId: string | null = "a".repeat(32),
    clientId = "fixture-client",
    registered = !options.newSetup,
    revision = 1,
    unknownRevision: number | null =
      options.status === "reauthorization_required" ? 1 : null,
    clientPublic = true,
    clientAuthentication = "client_secret_post",
    archived: unknown[] = [],
    localPolicy: Record<string, unknown> | null = null,
    originalPolicy: Record<string, unknown> | null = null,
    policyCalls = 0;
  const currentPolicy = () =>
    !options.policyChanged &&
    !(
      options.policyChangedAfterUnknown &&
      ["reauthorization_required", "disconnected"].includes(status)
    );
  const hasUnknown = () => unknownRevision === revision;
  const metadata = () => ({
    name: "owned",
    revision,
    provider: "fixture",
    client_id: clientId,
    project_id: projectId,
    status,
    resource: "https://fixture.example/mcp",
    scopes: ["read"],
    agent_ready: false,
    remote_revocation_supported: false,
    policy_current: currentPolicy(),
    authentication: clientAuthentication,
    public_client: clientPublic,
    secret_configured: !clientPublic,
    callback_uri:
      "https://factory.example/api/v1/connection-oauth/owned/callback",
    refresh_available: status === "authorized_unverified",
    grant_expires_at:
      status === "authorized_unverified" ? "2026-10-15T12:00:00+00:00" : null,
    effect_state: hasUnknown() ? "unknown" : "none",
    unresolved_effects: hasUnknown()
      ? [
          {
            id: "d".repeat(32),
            kind: "refresh",
            state: "unknown",
            registration_revision: 1,
            lease_expired: true,
          },
        ]
      : [],
    archived_unknown_effects: archived,
    reconciliation_required: hasUnknown() || !!archived.length,
    allowed_actions: {
      configure:
        currentPolicy() &&
        !options.denyActions &&
        status !== "reauthorization_required",
      authorize:
        currentPolicy() &&
        !options.denyActions &&
        status !== "reauthorization_required" &&
        status !== "disconnected",
      refresh:
        currentPolicy() &&
        !options.denyActions &&
        status === "authorized_unverified",
      disconnect: !options.denyActions && status !== "disconnected",
      supersede:
        currentPolicy() &&
        !options.denyActions &&
        status === "reauthorization_required",
    },
    family: null,
    grant_revision: null,
    grant_status: null,
    observed_at: null,
    ...(status === "authorized_unverified"
      ? {
          family: "b".repeat(32),
          grant_revision: grantRevision,
          grant_status: "authorized_unverified",
        }
      : {}),
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (request.method() === "GET") options.onRead?.(path);
    if (request.method() !== "GET")
      writes.push({
        path,
        body: request.postData() ? request.postDataJSON() : null,
      });
    if (path.endsWith("/oauth-provider-policies"))
      return route.fulfill({
        json: {
          local_development_available: options.localDevelopment === true,
          policies: localPolicy ? [localPolicy] : [],
        },
      });
    if (path.endsWith("/oauth-provider-policies/local-repository-tools")) {
      const body = request.postDataJSON(),
        origin = body.local_origin;
      policyCalls += 1;
      const provider = {
        id: "local-repository-tools",
        issuer: origin,
        authorization_endpoint: origin + "/oauth2/authorize",
        token_endpoint: origin + "/oauth2/token",
        resource: origin + "/mcp/readonly",
        scopes: ["repository:read"],
        scope_omission: "reject",
        refresh_scope_omission: "reject",
        refresh_token_omission: "reject",
        prerequisite: null,
      };
      const packet = {
        name: "local-repository-tools",
        revision: body.expected_revision + 1,
        digest: "a".repeat(64),
        mode: "local_development",
        enabled: body.enabled,
        local_origin: origin,
        provider,
      };
      if (policyCalls === 1) {
        localPolicy = packet;
        originalPolicy = packet;
        if (options.losePolicyResponse) return route.abort("failed");
      }
      if (policyCalls > 1 && options.historicalPolicyReplay) {
        localPolicy = {
          ...packet,
          revision: 2,
          enabled: false,
          digest: "b".repeat(64),
        };
        return route.fulfill({ json: originalPolicy });
      }
      return route.fulfill({ json: packet });
    }
    if (path.includes("/connection-oauth/owned/effects/")) {
      if (options.closedReceipt && !options.mismatchedReceipt)
        status = "authorized_unverified";
      return route.fulfill({
        json: {
          id: "d".repeat(32),
          request_id: options.mismatchedReceipt
            ? "e".repeat(32)
            : path.split("/").at(-1),
          kind: "refresh",
          state: options.closedReceipt ? "completed" : "unknown",
          registration_revision: 1,
          lease_expired: true,
          observed_at: null,
          current_context: currentPolicy(),
        },
      });
    }
    if (path.endsWith("/connection-oauth/second"))
      return route.fulfill({
        json: {
          ...metadata(),
          name: "second",
          status: "disconnected",
          effect_state: "none",
          unresolved_effects: [],
          allowed_actions: {
            configure: true,
            authorize: false,
            refresh: false,
            disconnect: false,
            supersede: false,
          },
        },
      });
    if (path.endsWith("/connection-oauth"))
      return route.fulfill({
        json: registered
          ? [
              options.malformed
                ? { ...metadata(), agent_ready: true }
                : metadata(),
              ...(options.secondAccount
                ? [{ ...metadata(), name: "second", status: "disconnected" }]
                : []),
            ]
          : [],
      });
    if (path.endsWith("/connection-oauth/owned/prepare"))
      return route.fulfill({
        json: {
          continuation_url:
            (options.foreignHandoff
              ? "https://untrusted.example"
              : new URL(request.url()).origin) +
            "/api/v1/connection-oauth/owned/continue?ticket=fixture-ticket",
          expires_in: 300,
        },
      });
    if (path.endsWith("/connection-oauth/owned/continue")) {
      status = "authorized_unverified";
      return route.fulfill({ status: 303, headers: { Location: "/" } });
    }
    if (path.endsWith("/connection-oauth/owned/supersede")) {
      const body = request.postDataJSON();
      archived = [
        {
          id: "d".repeat(32),
          kind: "refresh",
          state: "unknown",
          registration_revision: revision,
          lease_expired: true,
        },
      ];
      revision += 1;
      status = "authorization_required";
      clientId = body.registration.client_id;
      projectId = body.registration.project_id;
      clientPublic = !body.registration.client_secret;
      clientAuthentication = body.registration.authentication;
      return route.fulfill({ json: metadata() });
    }
    if (path.endsWith("/connection-oauth/owned/refresh")) {
      grantRevision += 1;
      if (options.unknownRefresh) {
        unknownRevision = revision;
        status = "reauthorization_required";
        return route.abort("failed");
      }
      return route.fulfill({ json: metadata() });
    }
    if (path.endsWith("/connection-oauth/owned/disconnect")) {
      status = "disconnected";
      return route.fulfill({ json: metadata() });
    }
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: { mode: "local_bootstrap", issuer: "", cookie_secure: false },
      });
    if (path.endsWith("/health") && options.executionUnavailable)
      return route.fulfill({ json: { execution_ready: false } });
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
          global_admin: admin,
          memberships: [],
          roles: {},
        },
      });
    if (path.endsWith("/connection-oauth/providers"))
      return route.fulfill({
        json: [
          ...["github", "figma", "sentry", "grafana", "playwright"].map(
            (provider) => ({
              provider,
              status: "prerequisite_required",
              reason: "Fixture: provider prerequisites remain unmet.",
              agent_ready: false,
            }),
          ),
          ...(options.localDevelopment
            ? [
                {
                  provider: "local-repository-tools",
                  status: localPolicy?.enabled
                    ? "registration_available"
                    : "prerequisite_required",
                  reason: "Owned local repository service contract fixture.",
                  agent_ready: false,
                },
              ]
            : []),
          ...(admitted
            ? [
                {
                  provider: "fixture",
                  status: "registration_available",
                  reason: "Owned local authorization-server contract fixture.",
                  agent_ready: false,
                },
              ]
            : []),
        ],
      });
    if (path.endsWith("/projects"))
      return route.fulfill({
        json: [
          {
            id: "a".repeat(32),
            definition: { name: "Owned fixture project", components: [] },
          },
        ],
      });
    if (path.endsWith("/connection-oauth/owned")) {
      if (request.method() === "PUT") {
        await options.beforeSave?.();
        const body = request.postDataJSON();
        if (options.concurrentCreator) {
          registered = true;
          clientId = "other-console-client";
          projectId = "b".repeat(32);
          if (body.expected_revision === 0)
            return route.fulfill({
              status: 409,
              json: {
                detail:
                  "Connection already exists; create-only request rejected.",
              },
            });
          revision += 1;
        }
        registered = true;
        clientPublic = options.conflictingCredential
          ? true
          : !body.client_secret;
        clientAuthentication = body.authentication;
        clientId = options.conflictingSave
          ? "fixture-private-client-secret"
          : body.client_id;
        projectId = options.conflictingSave ? "b".repeat(32) : body.project_id;
      }
      if (options.rejected && request.method() === "PUT")
        return route.fulfill({
          status: 422,
          json: {
            detail:
              "fixture-private-client-secret <img src=x onerror=alert(1)>" +
              "x".repeat(20000),
          },
        });
      return route.fulfill({
        json: options.malformed
          ? { ...metadata(), agent_ready: true }
          : metadata(),
      });
    }
    return route.fulfill({
      json: [
        "/repositories",
        "/runs",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((s) => path.endsWith(s))
        ? []
        : {},
    });
  });
  await page.goto("/");
  if (admin) await navigate(page, "Administration");
  else await expectAuthenticated(page);
  return writes;
}
test("default inactive providers cannot create a registration or send a remote request", async ({
  page,
}) => {
  const writes = await fixture(page);
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Connected accounts", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Set up a connection", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Prerequisites required", { exact: true }),
  ).toHaveCount(5);
  expect(writes).toEqual([]);
});
async function openExisting(page: Page) {
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page.getByRole("button", { name: "Open owned", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Inspect the retained connection",
      exact: true,
    }),
  ).toBeVisible();
}
test("explicit browser consent returns to an actor-bound current summary without persisting the handoff", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, { newSetup: false });
  await openExisting(page);
  await page
    .getByRole("button", { name: "Review consent", exact: true })
    .click();
  expect(writes).toEqual([]);
  await page
    .getByRole("button", { name: "Open browser consent", exact: true })
    .click();
  await expect(
    page.getByText("Authorization retained; tools unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toEqual([
    { path: "/api/v1/connection-oauth/owned/prepare", body: null },
  ]);
  const marker = await page.evaluate(() =>
    sessionStorage.getItem("forge.browser-connection"),
  );
  expect(JSON.parse(marker!)).toEqual({
    name: "owned",
    base: "/api/v1",
    actor: "f".repeat(32),
    uncertain: false,
  });
  expect(marker).not.toContain("ticket");
});
test("a handoff to another origin is rejected without navigating away", async ({
  page,
}) => {
  await fixture(page, true, true, { foreignHandoff: true });
  await openExisting(page);
  await page
    .getByRole("button", { name: "Review consent", exact: true })
    .click();
  const originalOrigin = new URL(page.url()).origin;
  await page
    .getByRole("button", { name: "Open browser consent", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(new URL(page.url()).origin).toBe(originalOrigin);
  await expect(page.getByText("fixture-ticket", { exact: false })).toHaveCount(
    0,
  );
});
test("a lost refresh response retains its request across reloads and requires explicit recovery", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
    unknownRefresh: true,
  });
  await openExisting(page);
  await page
    .getByRole("button", { name: "Request refresh", exact: true })
    .click();
  await expect(
    page.getByText("Outstanding action needs server reconciliation", {
      exact: true,
    }),
  ).toBeVisible();
  const pending = await page.evaluate(
    () =>
      JSON.parse(sessionStorage.getItem("forge.browser-connection")!).pending,
  );
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  for (const reloadPage of [false, true]) {
    if (reloadPage) await page.reload();
    await expect(
      page.getByRole("button", {
        name: "Start new authorization",
        exact: true,
      }),
    ).toBeEnabled();
    await expect(
      page.getByText("Outstanding action needs server reconciliation", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Request refresh", exact: true }),
    ).toHaveCount(0);
    expect(
      await page.evaluate(
        () =>
          JSON.parse(sessionStorage.getItem("forge.browser-connection")!)
            .pending,
      ),
    ).toEqual(pending);
    expect(writes).toHaveLength(1);
  }
  await page
    .getByRole("button", { name: "Start new authorization", exact: true })
    .click();
  const save = page.getByRole("button", {
    name: "Save new authorization revision",
    exact: true,
  });
  await expect(save).toBeDisabled();
  await page
    .getByLabel("Reason for the new authorization", { exact: true })
    .fill("The original refresh response was not confirmed.");
  await expect(save).toBeDisabled();
  await page
    .getByLabel("I understand the previous provider outcome remains unknown.", {
      exact: true,
    })
    .check();
  await expect(save).toBeEnabled();
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe("/api/v1/connection-oauth/owned/refresh");
  expect(writes[0].body).toMatchObject({
    request_id: expect.stringMatching(/^[a-f0-9]{32}$/),
  });
  expect(pending.requestId).toBe(
    (writes[0].body as { request_id: string }).request_id,
  );
});
test("refresh is explicit and local disconnect asks for confirmation without claiming provider revocation", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
  });
  await openExisting(page);
  expect(writes).toEqual([]);
  await page
    .getByRole("button", { name: "Request refresh", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Refresh response retained" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Disconnect locally?", exact: true }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  await page
    .getByRole("button", { name: "Disconnect locally", exact: true })
    .click();
  await expect(
    page.getByText("Locally disconnected", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Provider-side authorization was not revoked" }),
  ).toBeVisible();
  expect(writes).toHaveLength(2);
});
test("malformed authority does not unlock consent or renewal", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, { malformed: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Request refresh", exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
  { width: 375, height: 600 },
])
  for (const theme of ["light", "dark"]) {
    test(`current state actions remain within shell at${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await fixture(page, true, true, { status: "authorized_unverified" });
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await openExisting(page);
      await page.evaluate(() => document.fonts.ready);
      await expect(page.locator(".oauth-wizard")).toHaveCSS(
        "--wizard-frame-height",
        /px/,
      );
      const geometry = await page.locator(".oauth-wizard").evaluate((frame) => {
        const heading = frame
            .querySelector(".wizard-heading")!
            .getBoundingClientRect(),
          steps = frame.querySelector(".wizard-steps")!.getBoundingClientRect(),
          actions = frame
            .querySelector(".wizard-actions")!
            .getBoundingClientRect(),
          main = frame.closest("main")!.getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
        return {
          heading: heading.top,
          steps: steps.top,
          actions: actions.bottom,
          main: main.top,
          footer: footer.top,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      expect(geometry.heading).toBeGreaterThanOrEqual(geometry.main);
      expect(geometry.steps).toBeGreaterThanOrEqual(geometry.main);
      expect(geometry.actions).toBeLessThanOrEqual(geometry.footer);
      expect(geometry.overflow).toBe(false);
    });
  }
test("administrator registers an explicit confidential client without persisting its secret", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByLabel("Service", { exact: true }).selectOption("fixture");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page
    .getByLabel("Account scope", { exact: true })
    .selectOption("a".repeat(32));
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review browser authorization",
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toEqual([
    {
      path: "/api/v1/connection-oauth/owned",
      body: {
        expected_revision: 0,
        provider: "fixture",
        project_id: "a".repeat(32),
        client_id: "fixture-client",
        authentication: "client_secret_post",
        client_secret: "fixture-private-client-secret",
      },
    },
  ]);
  const retained = await page.evaluate(() => ({
    local: JSON.stringify(localStorage),
    session: JSON.stringify(sessionStorage),
    html: document.body.innerHTML,
  }));
  expect(JSON.stringify(retained)).not.toContain(
    "fixture-private-client-secret",
  );
});
test("project ownership cannot enter connection administration", async ({
  page,
}) => {
  const writes = await fixture(page, true, false);
  await expect(
    page.getByRole("button", { name: "Administration", exact: true }),
  ).toHaveCount(0);
  await expect(
    page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});
test("changing to a public client discards the secret before keyboard submission", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page.getByLabel("Client type", { exact: true }).selectOption("public");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await expect(page.getByLabel("Client secret", { exact: true })).toHaveValue(
    "",
  );
  await page.getByLabel("Client type", { exact: true }).selectOption("public");
  await page.getByLabel("Client ID", { exact: true }).press("Enter");
  await expect(
    page.getByRole("heading", {
      name: "Review browser authorization",
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toEqual([
    {
      path: "/api/v1/connection-oauth/owned",
      body: {
        expected_revision: 0,
        provider: "fixture",
        project_id: null,
        client_id: "fixture-client",
        public_client: true,
        authentication: "client_secret_post",
      },
    },
  ]);
});
test("a rejected response cannot render provider HTML or echo the submitted client secret", async ({
  page,
}) => {
  await fixture(page, true, true, { rejected: true, newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeFocused();
  await expect(page.getByLabel("Client secret", { exact: true })).toHaveValue(
    "",
  );
  expect(await page.getByRole("alert").textContent()).not.toContain(
    "fixture-private-client-secret",
  );
  await expect(page.locator("[onerror]")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save registration", exact: true }),
  ).toBeDisabled();
  expect(
    await page.evaluate(() =>
      JSON.stringify({
        local: localStorage,
        session: sessionStorage,
        html: document.body.innerHTML,
      }),
    ),
  ).not.toContain("fixture-private-client-secret");
});
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
  { width: 375, height: 600 },
]) {
  test(`all initial steps keep the current title and actions within allocation at${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await fixture(page, true, true, { newSetup: true });
    await page
      .getByLabel("Appearance", { exact: true })
      .selectOption(viewport.width === 1440 ? "light" : "dark");
    await page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Set up a connection", exact: true })
      .click();
    async function frame(stage: string) {
      const wizard = page.locator(".oauth-wizard");
      await expect(wizard.locator('[aria-current="step"]')).toContainText(
        stage,
      );
      if (stage !== "Prerequisites")
        await expect(wizard).toHaveCSS("--wizard-frame-height", /px/);
      await page.evaluate(() => document.fonts.ready);
      const shape = await wizard.evaluate((frame) => {
        const main = frame.closest("main")!,
          m = main.getBoundingClientRect(),
          h = frame.querySelector(".wizard-heading")!.getBoundingClientRect(),
          a = frame.querySelector(".wizard-actions")!.getBoundingClientRect(),
          f = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
        const button = frame.querySelector<HTMLButtonElement>(
            ".wizard-actions .primary",
          )!,
          r = button.getBoundingClientRect(),
          target = document.elementFromPoint(
            r.x + r.width / 2,
            r.y + r.height / 2,
          );
        return {
          scroll: main.scrollTop,
          heading: h.top,
          main: m.top,
          action: a.bottom,
          footer: f.top,
          hit: target === button || button.contains(target),
        };
      });
      expect(shape.scroll).toBe(0);
      expect(shape.heading).toBeGreaterThanOrEqual(shape.main);
      expect(shape.action).toBeLessThanOrEqual(shape.footer);
      expect(shape.hit).toBe(true);
      if (process.env.OAUTH_UI_PROOF) {
        mkdirSync(process.env.OAUTH_UI_PROOF, { recursive: true });
        await page.screenshot({
          path: join(
            process.env.OAUTH_UI_PROOF,
            `oauth-${stage.replaceAll(" ", "-").replaceAll("&", "and")}-${viewport.width}x${viewport.height}.png`,
          ),
          fullPage: true,
        });
      }
    }
    await frame("Prerequisites");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await frame("Client & scope");
    await page.getByLabel("Connection name", { exact: true }).fill("owned");
    await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
    await page
      .getByRole("button", { name: "Save registration", exact: true })
      .click();
    await frame("Browser consent");
    await page
      .getByRole("button", { name: "Inspect state", exact: true })
      .click();
    await frame("Connection state");
  });
}

test("enabled local service uses a different literal cookie host while disabled configuration stays diagnostic", async ({
  page,
}) => {
  const writes = await fixture(page, false, true, { localDevelopment: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  await page
    .getByLabel("Local host", { exact: true })
    .selectOption("127.0.0.1");
  await page.getByLabel("Service port", { exact: true }).fill("49152");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("cookies");
  await expect(
    page.getByRole("heading", {
      name: "Configure a local service",
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toEqual([]);
  await page
    .getByLabel("Browser registrations", { exact: true })
    .selectOption("disabled");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save service configuration", exact: true })
    .click();
  await expect(page.getByText("Disabled", { exact: true })).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    enabled: false,
    local_origin: "http://127.0.0.1:49152",
  });
});

test("an older same-host pending policy stays retained without dispatch or silent replacement", async ({
  page,
}) => {
  const writes = await fixture(page, false, true, { localDevelopment: true });
  const marker = {
    actor: "f".repeat(32),
    base: "/api/v1",
    body: {
      enabled: true,
      local_origin: "http://127.0.0.1:49152",
      expected_revision: 0,
      request_id: "d".repeat(32),
    },
  };
  const key = `forge.local-service-action:${marker.actor}:${encodeURIComponent(marker.base)}`;
  await page.evaluate(
    ({ key, marker }) => sessionStorage.setItem(key, JSON.stringify(marker)),
    { key, marker },
  );
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirm original save", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("cookies");
  expect(writes).toEqual([]);
  expect(
    await page.evaluate((key) => JSON.parse(sessionStorage.getItem(key)!), key),
  ).toEqual(marker);
});
test("inspecting a registration does not mark browser consent completed", async ({
  page,
}) => {
  await fixture(page, true, true, { newSetup: false });
  await openExisting(page);
  await expect(
    page
      .locator(".oauth-wizard .wizard-steps button")
      .filter({ hasText: "Browser consent" }),
  ).toHaveAttribute("title", "Browser consent · visited");
  await expect(
    page.getByText("Browser authorization required", { exact: true }),
  ).toBeVisible();
});
test("the provider choice panel fits its content instead of allocating an empty long form", async ({
  page,
}) => {
  await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Choose a service",
      exact: true,
    }),
  ).toBeVisible();
  const height = await page
    .locator(".oauth-wizard")
    .evaluate((e) => e.getBoundingClientRect().height);
  expect(height).toBeLessThan(480);
});
test("a browser that cannot retain a nonsecret action reference never submits credentials", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === "forge.browser-connection")
        throw new DOMException("Fixture denied storage", "SecurityError");
      return original.call(this, key, value);
    };
  });
  const writes = await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("action reference");
  expect(writes).toEqual([]);
});
test("a successful but mismatched registration response cannot expose a secret or another project", async ({
  page,
}) => {
  await fixture(page, true, true, { conflictingSave: true, newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(await page.locator("main").textContent()).not.toContain(
    "fixture-private-client-secret",
  );
  await expect(
    page.getByRole("button", { name: "Save registration", exact: true }),
  ).toBeDisabled();
});
test("pending registration freezes its fields so a later edit cannot retain another secret", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await fixture(page, true, true, {
    beforeSave: () => pending,
    newSetup: true,
  });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  try {
    await expect(
      page.getByLabel("Client secret", { exact: true }),
    ).toBeDisabled();
    await expect(page.getByLabel("Client ID", { exact: true })).toBeDisabled();
    await expect(page.getByLabel("Client secret", { exact: true })).toHaveValue(
      "",
    );
  } finally {
    release();
  }
  await expect(
    page.getByRole("heading", {
      name: "Review browser authorization",
      exact: true,
    }),
  ).toBeVisible();
});
test("desktop client ID and private credential fields fit the initial900px form allocation", async ({
  page,
}, testInfo) => {
  await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  const boxes = await page
    .locator(".oauth-client-fields")
    .evaluate((fields) => {
      const scroll = fields
        .closest(".wizard-field-scroll")!
        .getBoundingClientRect();
      return [
        "oauth-client-id",
        "oauth-client-secret",
        "oauth-auth-method",
      ].map((id) => {
        const input = fields.querySelector<HTMLElement>(`#${id}`)!,
          label = fields.querySelector<HTMLElement>(`label[for="${id}"]`)!;
        return {
          label: label.getBoundingClientRect().top,
          input: input.getBoundingClientRect().bottom,
          top: scroll.top,
          bottom: scroll.bottom,
          grid: getComputedStyle(fields).gridTemplateColumns,
          fieldWidth: fields.getBoundingClientRect().width,
        };
      });
    });
  if (process.env.OAUTH_UI_PROOF) {
    mkdirSync(process.env.OAUTH_UI_PROOF, { recursive: true });
    await page.screenshot({
      path: join(
        process.env.OAUTH_UI_PROOF,
        "oauth-client-confidential-1440x900.png",
      ),
      fullPage: true,
    });
  }
  await testInfo.attach("client-allocation", {
    body: JSON.stringify(boxes),
    contentType: "application/json",
  });
  for (const box of boxes) {
    expect(box.label).toBeGreaterThanOrEqual(box.top);
    expect(box.input).toBeLessThanOrEqual(box.bottom);
  }
});

test("directory opens a current connection without entering a technical name and obeys server actions", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
    denyActions: true,
  });
  await openExisting(page);
  await expect(
    page.getByRole("button", { name: "Request refresh", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Existing connection name", { exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});

for (const mismatch of [false, true])
  test(`closed receipt recovery matches exact identity: mismatch=${mismatch}`, async ({
    page,
  }) => {
    const writes = await fixture(page, true, true, {
      status: "authorized_unverified",
      unknownRefresh: true,
      closedReceipt: true,
      mismatchedReceipt: mismatch,
    });
    await openExisting(page);
    await page
      .getByRole("button", { name: "Request refresh", exact: true })
      .click();
    await expect(
      page.getByText("Outstanding action needs server reconciliation", {
        exact: true,
      }),
    ).toBeVisible();
    const pending = await page.evaluate(
      () =>
        JSON.parse(sessionStorage.getItem("forge.browser-connection")!).pending,
    );
    expect(pending.requestId).toEqual(
      (writes[0].body as { request_id: string }).request_id,
    );
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(
      page.getByText("Outstanding action needs server reconciliation", {
        exact: true,
      }),
    ).toHaveCount(mismatch ? 1 : 0);
    if (!mismatch)
      await expect(
        page.getByRole("button", { name: "Request refresh", exact: true }),
      ).toBeEnabled();
    else
      await expect(
        page.getByRole("button", {
          name: "Start new authorization",
          exact: true,
        }),
      ).toBeVisible();
    expect(writes).toHaveLength(1);
  });
test("unknown revision recovery requires reason and acknowledgment and retains the archived unknown result", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "reauthorization_required",
  });
  await openExisting(page);
  await page
    .getByRole("button", { name: "Start new authorization", exact: true })
    .click();
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page
    .getByLabel("Reason for the new authorization", { exact: true })
    .fill(
      "Provider response was not confirmed; replace this authorization safely.",
    );
  await expect(
    page.getByRole("button", {
      name: "Save new authorization revision",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByLabel("I understand the previous provider outcome remains unknown.", {
      exact: true,
    })
    .check();
  await page
    .getByRole("button", {
      name: "Save new authorization revision",
      exact: true,
    })
    .click();
  await expect(
    page.getByText("Previous provider outcomes remain unknown", {
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({
    path: "/api/v1/connection-oauth/owned/supersede",
    body: {
      expected_revision: 1,
      unknown_effect_ids: ["d".repeat(32)],
      acknowledge_unknown: true,
      registration: { client_secret: "fixture-private-client-secret" },
    },
  });
  expect(
    await page.evaluate(() =>
      sessionStorage.getItem("forge.browser-connection"),
    ),
  ).not.toContain("fixture-private-client-secret");
});

test("switching accounts and a new setup preserve the first account's outstanding request", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
    unknownRefresh: true,
    secondAccount: true,
  });
  await openExisting(page);
  await page
    .getByRole("button", { name: "Request refresh", exact: true })
    .click();
  await expect(
    page.getByText("Outstanding action needs server reconciliation", {
      exact: true,
    }),
  ).toBeVisible();
  const original = await page.evaluate(
    () =>
      JSON.parse(sessionStorage.getItem("forge.browser-connection")!).pending,
  );
  await page
    .getByRole("button", { name: "Provider overview", exact: true })
    .click();
  await page.getByRole("button", { name: "Open second", exact: true }).click();
  await page
    .getByRole("button", { name: "Provider overview", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Provider overview", exact: true })
    .click();
  await page.getByRole("button", { name: "Open owned", exact: true }).click();
  await expect(
    page.getByText("Outstanding action needs server reconciliation", {
      exact: true,
    }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        JSON.parse(sessionStorage.getItem("forge.browser-connection")!).pending,
    ),
  ).toEqual(original);
  expect(writes).toHaveLength(1);
});
for (const viewport of [
  { width: 375, height: 600 },
  { width: 720, height: 300 },
])
  test(`credential fields remain reachable in short layout${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await fixture(page, true, true, { newSetup: true });
    await page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Set up a connection", exact: true })
      .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .getByLabel("Client type", { exact: true })
      .selectOption("confidential");
    for (const name of [
      "Connection name",
      "Client ID",
      "Client authentication",
      "Client secret",
    ]) {
      const input = page.getByLabel(name, { exact: true });
      await input.focus();
      await input.locator("..").scrollIntoViewIfNeeded();
      await expect(input).toBeFocused();
      const box = await input.evaluate((input) => {
        const r = input.getBoundingClientRect(),
          l = input
            .parentElement!.querySelector("label")!
            .getBoundingClientRect(),
          f = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect(),
          m = input.closest("main")!.getBoundingClientRect();
        const hit = document.elementFromPoint(
          r.x + r.width / 2,
          r.y + r.height / 2,
        );
        return {
          label: l.top,
          input: r.bottom,
          footer: f.top,
          main: m.top,
          hit: hit === input || input.contains(hit),
        };
      });
      expect(box.label).toBeGreaterThanOrEqual(box.main);
      expect(box.input).toBeLessThanOrEqual(box.footer);
      expect(box.hit).toBe(true);
    }
  });

test("a full outstanding-action registry refuses a new dispatch without evicting any unknown request", async ({
  page,
}) => {
  const marker = {
    name: "previous",
    actor: "f".repeat(32),
    base: "/api/v1",
    uncertain: true,
    pending: { requestId: "c".repeat(32), revision: 1, kind: "refresh" },
    history: Array.from({ length: 100 }, (_, i) => ({
      name: `pending-${i}`,
      uncertain: true,
      pending: {
        requestId: i.toString(16).padStart(32, "0"),
        revision: 1,
        kind: "refresh",
      },
    })),
  };
  await page.addInitScript(
    (value) =>
      sessionStorage.setItem("forge.browser-connection", JSON.stringify(value)),
    marker,
  );
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
  });
  // The retained account is unavailable in this fixture. The operator can inspect another account without erasing it.
  await navigate(page, "Administration");
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Provider overview", exact: true })
    .click();
  await page.getByRole("button", { name: "Open owned", exact: true }).click();
  await page
    .getByRole("button", { name: "Request refresh", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("action reference");
  expect(
    await page.evaluate(() =>
      JSON.parse(sessionStorage.getItem("forge.browser-connection")!),
    ),
  ).toEqual(marker);
  expect(writes).toEqual([]);
});

test("short phone service requirements do not disappear under the action row", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 600 });
  await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  const shape = await page
    .locator(".oauth-provider-step")
    .evaluate((wizard) => {
      const requirement =
        wizard.querySelector(".oauth-service-details summary") ||
        wizard.querySelector(".wizard-field-scroll .external-hint")!;
      return {
        bottom: requirement.getBoundingClientRect().bottom,
        actions: wizard
          .querySelector(".wizard-actions")!
          .getBoundingClientRect().top,
      };
    });
  expect(shape.bottom).toBeLessThanOrEqual(shape.actions);
});

test("a save response with the wrong credential mode does not mark the client registration saved", async ({
  page,
}) => {
  await fixture(page, true, true, {
    newSetup: true,
    conflictingCredential: true,
  });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByLabel("Client type", { exact: true })
    .selectOption("confidential");
  await page
    .getByLabel("Client secret", { exact: true })
    .fill("fixture-private-client-secret");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Review browser authorization",
      exact: true,
    }),
  ).toHaveCount(0);
});

test("a concurrent creator cannot be overwritten by a new-account request", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    newSetup: true,
    concurrentCreator: true,
  });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("server rejected");
  await expect(
    page.getByRole("heading", {
      name: "Review browser authorization",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({ expected_revision: 0 });
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await page
    .getByText("Connection details and request history", { exact: true })
    .click();
  await expect(
    page.getByText("other-console-client", { exact: true }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
});

test("desktop consent exposes the exact resource and requested scopes before opening the service", async ({
  page,
}) => {
  await fixture(page, true, true, { newSetup: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set up a connection", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Connection name", { exact: true }).fill("owned");
  await page.getByLabel("Client ID", { exact: true }).fill("fixture-client");
  await page
    .getByRole("button", { name: "Save registration", exact: true })
    .click();
  await expect(
    page.locator('.oauth-wizard [aria-current="step"]'),
  ).toContainText("Browser consent");
  await expect(page.locator(".oauth-wizard")).toHaveCSS(
    "--wizard-frame-height",
    /px/,
  );
  await page.evaluate(() => document.fonts.ready);
  for (const label of ["Resource", "Requested scopes"]) {
    const shape = await page
      .locator(".wizard-summary dt")
      .filter({ hasText: new RegExp(`^${label}$`) })
      .evaluate((label) => {
        const row = label.parentElement!.getBoundingClientRect(),
          scroll = label
            .closest(".wizard-field-scroll")!
            .getBoundingClientRect();
        return {
          top: row.top,
          bottom: row.bottom,
          scrollTop: scroll.top,
          scrollBottom: scroll.bottom,
        };
      });
    expect(shape.top).toBeGreaterThanOrEqual(shape.scrollTop);
    expect(shape.bottom).toBeLessThanOrEqual(shape.scrollBottom);
  }
});

test("changed service configuration remains readable while provider actions stay unavailable", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
    policyChanged: true,
  });
  await openExisting(page);
  await expect(
    page.getByText("Service configuration changed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Request refresh", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await page
    .getByRole("button", { name: "Disconnect locally", exact: true })
    .click();
  await expect(
    page.getByText("Locally disconnected", { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([
    { path: "/api/v1/connection-oauth/owned/disconnect", body: null },
  ]);
});
test("local disconnect after policy withdrawal does not reconcile the outstanding provider request", async ({
  page,
}) => {
  const writes = await fixture(page, true, true, {
    status: "authorized_unverified",
    unknownRefresh: true,
    policyChangedAfterUnknown: true,
  });
  await openExisting(page);
  await page
    .getByRole("button", { name: "Request refresh", exact: true })
    .click();
  await expect(
    page.getByText("Outstanding action needs server reconciliation", {
      exact: true,
    }),
  ).toBeVisible();
  const before = await page.evaluate(() =>
    JSON.parse(sessionStorage.getItem("forge.browser-connection")!),
  );
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(
    page.getByText("Service configuration changed", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Disconnect", exact: true }).click();
  await page
    .getByRole("button", { name: "Disconnect locally", exact: true })
    .click();
  await expect(
    page.getByText("Locally disconnected", { exact: true }),
  ).toBeVisible();
  const after = await page.evaluate(() =>
    JSON.parse(sessionStorage.getItem("forge.browser-connection")!),
  );
  expect(after.uncertain).toBe(true);
  expect(after.pending).toEqual(before.pending);
  expect(writes.map((w) => w.path)).toEqual([
    "/api/v1/connection-oauth/owned/refresh",
    "/api/v1/connection-oauth/owned/disconnect",
  ]);
});

test("local service setup is unavailable unless the native runtime admits local development", async ({
  page,
}) => {
  const writes = await fixture(page, false);
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Configure local service", exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});
test("the guided local service saves exact CAS fields and fixed server-derived endpoints before account setup", async ({
  page,
}) => {
  const writes = await fixture(page, false, true, { localDevelopment: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  await page.getByLabel("Local host", { exact: true }).selectOption("[::1]");
  await page.getByLabel("Service port", { exact: true }).fill("49152");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  await expect(
    page.getByText("repository:read", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("http://[::1]:49152/mcp/readonly", { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
  await page
    .getByRole("button", { name: "Save service configuration", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Current service configuration",
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({
    path: "/api/v1/oauth-provider-policies/local-repository-tools",
    body: {
      enabled: true,
      local_origin: "http://[::1]:49152",
      expected_revision: 0,
      request_id: expect.stringMatching(/^[a-f0-9]{32}$/),
    },
  });
  await expect(
    page.getByRole("button", { name: "Set up an account", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Set up an account", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Register the authorized client",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.locator('.oauth-wizard [aria-current="step"]'),
  ).toContainText("Client & scope");
  expect(writes).toHaveLength(1);
});
test("a lost policy response keeps the original request and explicit replay reads the current disabled revision", async ({
  page,
}) => {
  const writes = await fixture(page, false, true, {
    localDevelopment: true,
    losePolicyResponse: true,
    historicalPolicyReplay: true,
  });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  await page.getByLabel("Service port", { exact: true }).fill("49152");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save service configuration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.reload();
  await navigate(page, "Administration");
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirm original save", exact: true })
    .click();
  await expect(page.getByText("Disabled", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Set up an account", exact: true }),
  ).toBeDisabled();
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toEqual(writes[0].body);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
  { width: 375, height: 600 },
]) {
  test(`local service initial steps retain titles, actions and usable controls at${viewport.width}x${viewport.height}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    await fixture(page, false, true, { localDevelopment: true });
    await page
      .getByLabel("Appearance", { exact: true })
      .selectOption(viewport.width === 1440 ? "light" : "dark");
    await page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Configure local service", exact: true })
      .click();
    const wizard = page.locator(".oauth-service-wizard");
    async function frame(stage: string) {
      await expect(wizard.locator('[aria-current="step"]')).toContainText(
        stage,
      );
      await expect(wizard).toHaveCSS("--wizard-frame-height", /px/);
      await page.evaluate(() => document.fonts.ready);
      await expect(wizard).not.toHaveAttribute("data-layout", "flow");
      const shape = await wizard.evaluate((frame) => {
        const main = frame.closest("main")!,
          heading = frame
            .querySelector(".wizard-heading")!
            .getBoundingClientRect(),
          actions = frame
            .querySelector(".wizard-actions")!
            .getBoundingClientRect(),
          fields = frame
            .querySelector(".wizard-field-scroll")!
            .getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect(),
          button = frame.querySelector<HTMLButtonElement>(
            ".wizard-actions .primary",
          )!,
          rect = button.getBoundingClientRect(),
          target = document.elementFromPoint(
            rect.x + rect.width / 2,
            rect.y + rect.height / 2,
          );
        return {
          scroll: main.scrollTop,
          heading: heading.top,
          main: main.getBoundingClientRect().top,
          action: actions.bottom,
          footer: footer.top,
          fields: fields.bottom,
          actionTop: actions.top,
          hit: target === button || button.contains(target),
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      await testInfo.attach(`local-${stage}-allocation`, {
        body: JSON.stringify(shape),
        contentType: "application/json",
      });
      expect(shape.scroll).toBe(0);
      expect(shape.heading).toBeGreaterThanOrEqual(shape.main);
      expect(shape.fields).toBeLessThanOrEqual(shape.actionTop);
      expect(shape.action).toBeLessThanOrEqual(shape.footer);
      expect(shape.hit).toBe(true);
      expect(shape.overflow).toBe(false);
      if (process.env.OAUTH_UI_PROOF) {
        mkdirSync(process.env.OAUTH_UI_PROOF, { recursive: true });
        await page.screenshot({
          path: join(
            process.env.OAUTH_UI_PROOF,
            `oauth-service-${stage.replaceAll(" ", "-")}-${viewport.width}x${viewport.height}.png`,
          ),
          fullPage: true,
        });
      } else await page.screenshot();
      await expect(wizard).not.toHaveAttribute("data-layout", "flow");
    }
    await frame("Service address");
    for (const label of [
      "Local host",
      "Service port",
      "Browser registrations",
    ]) {
      const control = page.getByLabel(label, { exact: true });
      await control.scrollIntoViewIfNeeded();
      const visible = await control.evaluate((node) => {
        const rect = node.getBoundingClientRect(),
          fields = node
            .closest(".wizard-field-scroll")!
            .getBoundingClientRect();
        return rect.top >= fields.top && rect.bottom <= fields.bottom;
      });
      expect(visible).toBe(true);
    }
    await page.getByLabel("Service port", { exact: true }).fill("49152");
    await page
      .getByRole("button", { name: "Review configuration", exact: true })
      .click();
    await frame("Review");
    await expect(
      page.getByText("repository:read", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Save service configuration", exact: true })
      .click();
    await frame("Saved");
  });
}

test("local service storage failure prevents dispatch and invalid ports fail native form validation", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.local-service-action:"))
        throw new DOMException("Fixture denied storage", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  const writes = await fixture(page, false, true, { localDevelopment: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  const port = page.getByLabel("Service port", { exact: true });
  await port.fill("22");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  expect(
    await port.evaluate(
      (node: HTMLInputElement) => node.validity.rangeUnderflow,
    ),
  ).toBe(true);
  await expect(
    page.getByRole("heading", {
      name: "Configure a local service",
      exact: true,
    }),
  ).toBeVisible();
  await port.fill("49152");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save service configuration", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("session storage");
  expect(writes).toEqual([]);
});

test("denied local service storage reads keep navigation and account history usable without dispatch", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = function (key) {
      if (key.startsWith("forge.local-service-action:"))
        throw new DOMException("Fixture denied storage read", "SecurityError");
      return original.call(this, key);
    };
  });
  const writes = await fixture(page, false, true, { localDevelopment: true });
  await page
    .locator("main")
    .getByRole("button", { name: "Connected accounts", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Configure local service", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("session storage");
  await page.getByLabel("Service port", { exact: true }).fill("49152");
  await page
    .getByRole("button", { name: "Review configuration", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Save service configuration",
      exact: true,
    }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("session storage");
  await page
    .getByRole("button", { name: "Account overview", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved accounts", exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`execution-unavailable banner preserves complete initial client field groups at${viewport.width}x${viewport.height}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    await fixture(page, true, true, {
      newSetup: true,
      executionUnavailable: true,
    });
    await page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Set up a connection", exact: true })
      .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .getByLabel("Client type", { exact: true })
      .selectOption("confidential");
    await expect(page.getByRole("alert")).toContainText(
      "Execution is unavailable",
    );
    const ids =
      viewport.width === 1440
        ? [
            "oauth-name",
            "oauth-project",
            "oauth-client-id",
            "oauth-client-type",
            "oauth-auth-method",
            "oauth-client-secret",
          ]
        : ["oauth-name", "oauth-project"];
    const boxes = await page
      .locator(".oauth-client-fields")
      .evaluate((fields, ids) => {
        const scroll = fields
          .closest(".wizard-field-scroll")!
          .getBoundingClientRect();
        return ids.map((id) => ({
          id,
          top: fields
            .querySelector(`label[for="${id}"]`)!
            .getBoundingClientRect().top,
          bottom: fields.querySelector(`#${id}`)!.getBoundingClientRect()
            .bottom,
          visibleTop: scroll.top,
          visibleBottom: scroll.bottom,
        }));
      }, ids);
    await testInfo.attach("banner-client-allocation", {
      body: JSON.stringify(boxes),
      contentType: "application/json",
    });
    if (process.env.OAUTH_UI_PROOF) {
      mkdirSync(process.env.OAUTH_UI_PROOF, { recursive: true });
      await page.screenshot({
        path: join(
          process.env.OAUTH_UI_PROOF,
          `oauth-client-execution-unavailable-${viewport.width}x${viewport.height}.png`,
        ),
        fullPage: true,
      });
    }
    for (const box of boxes) {
      expect(box.top).toBeGreaterThanOrEqual(box.visibleTop);
      expect(box.bottom).toBeLessThanOrEqual(box.visibleBottom);
    }
  });
}

for (const width of [1200, 1320]) {
  test(`banner layout preserves readable selected credential choices at${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await fixture(page, true, true, {
      newSetup: true,
      executionUnavailable: true,
    });
    await page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Set up a connection", exact: true })
      .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .getByLabel("Client type", { exact: true })
      .selectOption("confidential");
    await page
      .getByLabel("Client authentication", { exact: true })
      .selectOption("client_secret_basic");
    await page.evaluate(() => document.fonts.ready);
    for (const label of ["Client type", "Client authentication"]) {
      const fits = await page
        .getByLabel(label, { exact: true })
        .evaluate((select: HTMLSelectElement) => {
          const style = getComputedStyle(select),
            ctx = document.createElement("canvas").getContext("2d")!;
          ctx.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
          const available =
            select.clientWidth -
            parseFloat(style.paddingLeft) -
            parseFloat(style.paddingRight) -
            24;
          return {
            text: ctx.measureText(select.selectedOptions[0].textContent!.trim())
              .width,
            available,
          };
        });
      expect(fits.text).toBeLessThanOrEqual(fits.available);
    }
  });
}

for (const theme of ["light", "dark"]) {
  test(`narrow desktop credential guide wraps its action without horizontal overflow ${theme}`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 1200, height: 800 });
    await fixture(page, true, true, {
      newSetup: true,
      executionUnavailable: true,
    });
    await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
    await page
      .locator("main")
      .getByRole("button", { name: "Connected accounts", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Set up a connection", exact: true })
      .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    const guide = page.locator(".oauth-wizard .wizard-guide");
    await page.evaluate(() => document.fonts.ready);
    const geometry = await guide.evaluate((e) => {
      const range = document.createRange();
      range.selectNodeContents(e.querySelector("h3")!);
      return {
        scrollWidth: e.scrollWidth,
        clientWidth: e.clientWidth,
        headingLines: new Set(Array.from(range.getClientRects(), (r) => r.top))
          .size,
      };
    });
    await testInfo.attach("guide-width", {
      body: JSON.stringify(geometry),
      contentType: "application/json",
    });
    expect(geometry.scrollWidth).toBe(geometry.clientWidth);
    expect(geometry.headingLines).toBeLessThanOrEqual(2);
    if (process.env.OAUTH_UI_PROOF) {
      mkdirSync(process.env.OAUTH_UI_PROOF, { recursive: true });
      await page.screenshot({
        path: join(
          process.env.OAUTH_UI_PROOF,
          `oauth-guide-initial-1200x800-${theme}.png`,
        ),
        fullPage: true,
      });
    }
    const action = guide.getByRole("button", {
      name: "Review external tool prerequisites",
      exact: true,
    });
    await action.scrollIntoViewIfNeeded();
    await action.focus();
    const hit = await action.evaluate((button) => {
      const rect = button.getBoundingClientRect(),
        parent = button.closest("aside")!.getBoundingClientRect(),
        target = document.elementFromPoint(
          rect.x + rect.width / 2,
          rect.y + rect.height / 2,
        );
      return {
        within:
          rect.left >= parent.left &&
          rect.right <= parent.right &&
          rect.top >= parent.top &&
          rect.bottom <= parent.bottom,
        hit: target === button || button.contains(target),
      };
    });
    expect(hit.within).toBe(true);
    expect(hit.hit).toBe(true);
    expect(await page.locator("main").evaluate((e) => e.scrollTop)).toBe(0);
    if (process.env.OAUTH_UI_PROOF) {
      mkdirSync(process.env.OAUTH_UI_PROOF, { recursive: true });
      await page.screenshot({
        path: join(
          process.env.OAUTH_UI_PROOF,
          `oauth-guide-action-1200x800-${theme}.png`,
        ),
        fullPage: true,
      });
    }
    await action.press("Enter");
    await expect(
      page.getByRole("heading", { name: "External tools", exact: true }),
    ).toBeVisible();
  });
}
