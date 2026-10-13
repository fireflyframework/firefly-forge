/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { BrowserContext, Route } from "@playwright/test";
const sessions = new WeakMap<BrowserContext, boolean>();
export async function mockIdentity(route: Route): Promise<boolean> {
  const path = new URL(route.request().url()).pathname;
  if (!path.includes("/auth/")) return false;
  const context = route.request().frame().page().context();
  if (path.endsWith("/configuration")) {
    await route.fulfill({
      json: { mode: "local_bootstrap", issuer: "", cookie_secure: false },
    });
  } else if (path.endsWith("/login")) {
    sessions.set(context, true);
    await route.fulfill({
      json: { authenticated: true },
      headers: {
        "Set-Cookie":
          "forge_session=opaque-fixture; HttpOnly; SameSite=Lax; Path=/",
      },
    });
  } else if (path.endsWith("/logout")) {
    sessions.set(context, false);
    await route.fulfill({ json: { authenticated: false } });
  } else if (sessions.get(context)) {
    await route.fulfill({
      json: {
        authenticated: true,
        user: {
          id: "f".repeat(32),
          display_name: "Test operator",
          email: null,
          authentication_method: "local_bootstrap",
        },
        global_admin: true,
        memberships: [],
        roles: {
          owner: ["read", "edit", "execute", "review", "release", "members"],
          maintainer: ["read", "edit", "execute", "review", "release"],
          developer: ["read", "edit", "execute"],
          reviewer: ["read", "review"],
          viewer: ["read"],
        },
      },
    });
  } else {
    await route.fulfill({ status: 401, json: { detail: "Sign in required" } });
  }
  return true;
}
