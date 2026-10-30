/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
const identity = vi.hoisted(() => ({ admin: false }));
vi.mock("./Identity", async (original) => {
  const actual = await original<typeof import("./Identity")>();
  return {
    ...actual,
    useIdentity: () => ({
      session: {
        authenticated: true,
        user: {
          id: "a".repeat(32),
          display_name: "Project Owner",
          email: "owner@example.test",
          authentication_method: "oidc_session",
        },
        global_admin: identity.admin,
        memberships: [
          {
            project_id: "b".repeat(32),
            role: "owner",
            permissions: ["read", "members"],
          },
        ],
        roles: {},
      },
      configuration: { mode: "oidc" },
      loading: false,
      error: "",
      refresh: () => {},
      forgetSession: () => {},
    }),
  };
});
import App from "./App";
import { Administration, requiresFactoryAdministrator } from "./Administration";
import type { IdentitySession } from "./Identity";
afterEach(() => vi.unstubAllGlobals());
function markup() {
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
  return renderToStaticMarkup(<App />);
}
it("keeps the account in the sidebar and removes duplicate header actions and source filler", () => {
  identity.admin = false;
  const html = markup();
  expect(html).toContain('class="app" aria-busy="false"');
  expect(html).not.toContain("Source on GitHub");
  expect(
    html.split('<header class="topbar">')[1].split("</header>")[0],
  ).not.toContain("Signed in as");
  expect(html.split('<div class="sidebar-bottom">')[1]).toContain(
    "Signed in as Project Owner",
  );
  expect(html).toContain('aria-label="Appearance preference"');
});
it("does not advertise factory administration to a project owner", () => {
  identity.admin = false;
  expect(markup()).not.toContain("<span>Administration</span>");
  identity.admin = true;
  expect(markup()).toContain("<span>Administration</span>");
});
it("keeps API connection controls and status out of the navbar for every role", () => {
  identity.admin = false;
  for (const admin of [false, true]) {
    identity.admin = admin;
    const header = markup()
      .split('<header class="topbar">')[1]
      .split("</header>")[0];
    expect(header).not.toContain("connection-indicator");
    expect(header).not.toContain("API connected");
  }
});
it("guards every global administration page while preserving personal sign-in and project catalogs", () => {
  const owner = { global_admin: false } as IdentitySession;
  for (const page of [
    "admin",
    "users",
    "settings",
    "search",
    "learning",
    "runners",
    "browser-connections",
    "external-tools",
  ]) {
    expect(requiresFactoryAdministrator(page, owner)).toBe(true);
    expect(
      requiresFactoryAdministrator(page, { ...owner, global_admin: true }),
    ).toBe(false);
  }
  for (const page of [
    "workflow",
    "analytics",
    "access",
    "product",
    "knowledge",
  ])
    expect(requiresFactoryAdministrator(page, owner)).toBe(false);
  expect(requiresFactoryAdministrator("settings", null)).toBe(false);
});
it("does not expose factory setup cards when a project owner reaches the administration component", () => {
  const session = {
    global_admin: false,
    memberships: [{ permissions: ["members", "read"] }],
  } as IdentitySession;
  const html = renderToStaticMarkup(
    <Administration session={session} onNavigate={() => {}} />,
  );
  expect(html).not.toContain('aria-label="Administration areas"');
  expect(html).toContain(
    "Factory administration requires an administrator account.",
  );
});
it("groups administrator settings into readable rows rather than repeated cards", () => {
  const session = {
    global_admin: true,
    memberships: [],
  } as unknown as IdentitySession;
  const html = renderToStaticMarkup(
    <Administration session={session} onNavigate={() => {}} />,
  );
  for (const group of ["People &amp; access", "Connections", "Execution"])
    expect(html).toContain(group);
  expect(html).toContain('aria-label="Factory settings"');
  expect(html).not.toContain('class="administration-cards"');
});
