/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate, expectAuthenticated } from "./navigation";
import { mockIdentity } from "./auth-fixture";

test("bootstrap exchanges a token for an opaque session and reconnects without retaining browser credentials", async ({
  page,
}) => {
  let signedIn = false;
  const writes: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: {
          mode: "local_bootstrap",
          issuer: "",
          login_path: "/api/v1/auth/login",
        },
        headers: {
          "Set-Cookie": "XSRF-TOKEN=fixture%2Fcsrf; Path=/; SameSite=Lax",
        },
      });
    if (path.endsWith("/auth/login")) {
      expect(route.request().headers()["x-xsrf-token"]).toBe("fixture/csrf");
      expect(route.request().postDataJSON()).toEqual({
        token: "test-bootstrap-token-at-least-24-characters",
      });
      signedIn = true;
      writes.push("login");
      return route.fulfill({
        json: { authenticated: true },
        headers: {
          "Set-Cookie":
            "forge_session=opaque-test; HttpOnly; SameSite=Lax; Path=/",
        },
      });
    }
    if (path.endsWith("/auth/logout")) {
      signedIn = false;
      writes.push("logout");
      return route.fulfill({ json: { authenticated: false } });
    }
    if (path.endsWith("/auth/session"))
      return route.fulfill(
        signedIn
          ? {
              json: {
                authenticated: true,
                user: {
                  id: "a".repeat(32),
                  display_name: null,
                  email: null,
                  authentication_method: "local_bootstrap",
                },
                global_admin: true,
                memberships: [],
                roles: {},
              },
            }
          : { status: 401, json: { detail: "Sign in required" } },
      );
    expect(route.request().headers().authorization).toBeUndefined();
    return route.fulfill({
      json: path.endsWith("/health")
        ? { execution_ready: true }
        : path.endsWith("/profiles")
          ? {}
          : [],
    });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await navigate(page, "Connections");
  await expect(
    page.getByText("Local developer bootstrap", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Signed in as Local operator",
      exact: true,
    }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      JSON.stringify({
        local: { ...localStorage },
        tab: { ...sessionStorage },
      }),
    ),
  ).not.toMatch(/test-bootstrap-token|opaque-test/);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expectAuthenticated(page);
  await page
    .getByRole("button", { name: "Signed in as Local operator", exact: true })
    .click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Connect factory", exact: true }),
  ).toBeVisible();
  expect(writes).toEqual(["login", "logout"]);
});

test("organization sign-in navigates only to the server-built authorization URL and preserves a relative product goal", async ({
  page,
}) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: {
          mode: "oidc",
          issuer: "https://identity.example.test/realms/forge",
          login_path: "/api/v1/auth/login",
        },
      });
    if (path.endsWith("/auth/login")) {
      expect(route.request().postDataJSON()).toEqual({});
      return route.fulfill({
        json: {
          authorization_url:
            "https://identity.example.test/realms/forge/protocol/openid-connect/auth?state=server-state",
          expires_in: 600,
        },
      });
    }
    return route.fulfill({ status: 401, json: { detail: "Sign in required" } });
  });
  await page.route("https://identity.example.test/**", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: "<title>Identity provider</title><p>Provider sign-in</p>",
    }),
  );
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page
    .getByRole("button", { name: "Start a product", exact: true })
    .click();
  await expect(page.getByLabel("API token", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(
    /^https:\/\/identity\.example\.test\/realms\/forge\/protocol\/openid-connect\/auth/,
  );
});

test("project membership grants and removals require a concrete reviewed person and role", async ({
  page,
}) => {
  const project = "a".repeat(32),
    user = "b".repeat(32);
  const writes: unknown[] = [];
  let members: object[] = [];
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/members/${user}`)) {
      const input = route.request().postDataJSON();
      writes.push(input);
      members = [
        {
          user_id: user,
          display_name: "Warehouse reviewer",
          email: "reviewer@example.test",
          role: input.role ?? "maintainer",
          active: input.role !== null,
          revision: writes.length,
        },
      ];
      return route.fulfill({
        json: { project_id: project, user_id: user, role: input.role },
      });
    }
    return route.fulfill({
      json: path.endsWith("/projects")
        ? [{ id: project, definition: { name: "Inventory product" } }]
        : path.endsWith("/identity/users")
          ? [
              {
                id: user,
                display_name: "Warehouse reviewer",
                email: "reviewer@example.test",
                enabled: true,
              },
            ]
          : path.endsWith("/members")
            ? members
            : path.endsWith("/health")
              ? { execution_ready: true }
              : path.endsWith("/profiles")
                ? {}
                : [],
    });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await page.setViewportSize({ width: 390, height: 844 });
  await navigate(page, "Project access");
  await page
    .getByRole("button", { name: "Add registered user", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Assign project access",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await page.getByLabel("Registered user", { exact: true }).selectOption(user);
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Project role", { exact: true })
    .selectOption("developer");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  const review = page.getByLabel(
    "I reviewed this person’s access to this project.",
    { exact: true },
  );
  await review.check();
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await page
    .getByLabel("Project role", { exact: true })
    .selectOption("maintainer");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(review).not.toBeChecked();
  await review.check();
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  const assign = dialog.getByRole("button", {
    name: "Assign project access",
    exact: true,
  });
  const box = await assign.boundingBox();
  expect(box!.y + box!.height).toBeLessThan(844);
  expect(writes).toEqual([]);
  await assign.click();
  await expect(dialog).not.toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Warehouse reviewer", exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([{ role: "maintainer" }]);
  await page.getByLabel("Project role", { exact: true }).selectOption("");
  await page
    .getByRole("button", { name: "Review change", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Keep current access", exact: true })
    .click();
  expect(writes).toHaveLength(1);
  await page
    .getByRole("button", { name: "Review change", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Confirm access change", exact: true })
    .click();
  await expect(
    page.getByText("Access removed · revision 2", { exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([{ role: "maintainer" }, { role: null }]);
});

test("a project viewer keeps a valid session when the private provider catalog is forbidden and cannot edit memberships", async ({
  page,
}) => {
  const id = "a".repeat(32),
    requests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: {
          mode: "oidc",
          issuer: "https://identity.example.test/realms/forge",
        },
      });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "b".repeat(32),
            display_name: "Project viewer",
            email: null,
            authentication_method: "oidc_session",
          },
          global_admin: false,
          memberships: [
            {
              project_id: id,
              role: "viewer",
              revision: 1,
              permissions: ["read"],
            },
          ],
          roles: { viewer: ["read"] },
        },
      });
    if (path.endsWith("/providers"))
      return route.fulfill({
        status: 403,
        json: { detail: "Factory administrator required" },
      });
    return route.fulfill({
      json: path.endsWith("/projects")
        ? [{ id, definition: { name: "Allowed product" } }]
        : path.endsWith("/profiles")
          ? {}
          : path.endsWith("/health")
            ? { execution_ready: true }
            : [],
    });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expectAuthenticated(page);
  await navigate(page, "Administration");
  await expect(
    page.locator("main").getByRole("button", { name: "People", exact: true }),
  ).toBeDisabled();
  await expect(
    page
      .locator("main")
      .getByRole("button", { name: "Connections", exact: true }),
  ).toBeDisabled();
  await page
    .locator("main")
    .getByRole("button", { name: "Project access", exact: true })
    .click();
  await expect(
    page.getByText("Your role: viewer.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add registered user", exact: true }),
  ).toHaveCount(0);
  expect(
    requests.some(
      (path) => path.endsWith("/identity/users") || path.endsWith("/members"),
    ),
  ).toBe(false);
});

test("project owners find registered people by name without entering account identifiers", async ({
  page,
}) => {
  const project = "a".repeat(32),
    user = "c".repeat(32);
  const searches: string[] = [],
    writes: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: {
          mode: "oidc",
          issuer: "https://identity.example.test/realms/forge",
        },
      });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "b".repeat(32),
            display_name: "Project owner",
            email: null,
            authentication_method: "oidc_session",
          },
          global_admin: false,
          memberships: [
            {
              project_id: project,
              role: "owner",
              revision: 1,
              permissions: ["read", "members"],
            },
          ],
          roles: { viewer: ["read"], owner: ["read", "members"] },
        },
      });
    if (path.endsWith("/eligible-users")) {
      searches.push(url.searchParams.get("query")!);
      return route.fulfill({
        json: [
          {
            id: user,
            display_name: "Warehouse reviewer",
            email: "reviewer@example.test",
            enabled: true,
          },
        ],
      });
    }
    if (path.endsWith(`/members/${user}`)) {
      writes.push(route.request().postDataJSON());
      return route.fulfill({ json: {} });
    }
    if (path.endsWith("/providers"))
      return route.fulfill({
        status: 403,
        json: { detail: "Administrator only" },
      });
    return route.fulfill({
      json: path.endsWith("/projects")
        ? [{ id: project, definition: { name: "Inventory product" } }]
        : path.endsWith("/health")
          ? { execution_ready: true }
          : path.endsWith("/profiles")
            ? {}
            : [],
    });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await navigate(page, "Project access");
  await page
    .getByRole("button", { name: "Add registered user", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Assign project access" });
  await expect(
    page.getByLabel("Registered user ID", { exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Find a person", { exact: true }).fill("Wa");
  await expect(
    dialog.getByRole("button", { name: "Continue", exact: true }),
  ).toBeDisabled();
  expect(searches).toEqual([]);
  await page.getByLabel("Find a person", { exact: true }).fill("Warehouse");
  await page.getByLabel("Registered user", { exact: true }).selectOption(user);
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page.getByLabel("Find a person", { exact: true })).toHaveValue(
    "Warehouse",
  );
  await expect(page.getByLabel("Registered user", { exact: true })).toHaveValue(
    user,
  );
  expect(searches).toEqual(["Warehouse"]);
  expect(writes).toEqual([]);
});
