/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const studioPackage = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
);
const projectId = "a".repeat(32),
  repositoryId = "b".repeat(32),
  digest = "d".repeat(64);
const schema = {
  type: "object",
  properties: {
    owner: { type: "string" },
    repo: { type: "string" },
    path: { type: "string" },
  },
  required: ["owner", "repo"],
};
const discovery = {
  connector: "github",
  endpoint: "https://api.githubcopilot.com/mcp/readonly",
  credential_revision: "e".repeat(32),
  server: { name: "Owned fixture server", version: "fixture" },
  digest,
  tools: { get_file_contents: schema, get_commit: schema },
};
async function fixture(
  page: Page,
  options: {
    admin?: boolean;
    saved?: boolean;
    uncertain?: string;
    malformed?: string;
    reject?: boolean;
    readFailure?: boolean;
  } = {},
) {
  let configured = !!options.saved,
    enabled = !!options.saved,
    found = options.saved ? discovery : null,
    review: { digest: string; tools: string[] } | null = options.saved
      ? { digest, tools: ["get_file_contents"] }
      : null;
  let failRead = false;
  const calls: string[] = [],
    writes: { path: string; body: Record<string, unknown> | null }[] = [],
    grants: Record<string, unknown>[] = [];
  const github = () => ({
    id: "github",
    name: "GitHub",
    endpoint: discovery.endpoint,
    tools: ["get_file_contents", "get_commit", "list_commits", "list_branches"],
    prerequisite: null,
    enabled,
    credential_configured: configured,
    readiness: !enabled
      ? "disabled"
      : !configured
        ? "credential_required"
        : !found
          ? "discovery_required"
          : !review
            ? "schema_review_required"
            : "project_grant_required",
    discovery: found,
    review,
    live_tool_verified: false,
  });
  const catalog = () => [
    github(),
    ...[
      {
        id: "sentry",
        name: "Sentry",
        prerequisite:
          "Reviewed project-scoped adapter and server skill constraints are required; inference and issue enrichment are not admitted.",
      },
      {
        id: "figma",
        name: "Figma",
        prerequisite:
          "Vendor allowed-client approval or waitlist admission and an authorized OAuth connection are required.",
      },
      {
        id: "playwright",
        name: "Playwright",
        prerequisite:
          "Use the native isolated BrowserRunner; an external stdio launcher is not admitted.",
      },
      {
        id: "grafana",
        name: "Grafana",
        prerequisite:
          "An approved managed HTTPS deployment and project-scoped adapter are required.",
      },
    ].map((item) => ({
      ...item,
      endpoint:
        item.id === "sentry"
          ? "https://mcp.sentry.dev/mcp"
          : item.id === "figma"
            ? "https://mcp.figma.com/mcp"
            : null,
      tools: [],
      enabled: false,
      credential_configured: false,
      readiness: "prerequisite_required",
      discovery: null,
      review: null,
      live_tool_verified: false,
    })),
  ];
  await page.route("**/api/v1/**", async (route) => {
    const r = route.request(),
      path = new URL(r.url()).pathname,
      method = r.method();
    calls.push(method + " " + path);
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({
        json: { mode: "local_bootstrap", issuer: "", cookie_secure: false },
      });
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
          global_admin: options.admin !== false,
          memberships: [],
          roles: { owner: ["members"], viewer: ["read"] },
        },
      });
    if (path.endsWith("/projects"))
      return route.fulfill({
        json: [
          {
            id: projectId,
            definition: {
              name: "Owned Stock product",
              components: [{ repository_id: repositoryId }],
            },
          },
        ],
      });
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: repositoryId,
            name: "Stock API",
            url: "https://github.com/team/product.git",
          },
          {
            id: "c".repeat(32),
            name: "Another project's API",
            url: "https://github.com/other/project.git",
          },
        ],
      });
    if (path.endsWith("/tools/mcp") && method === "GET") {
      if (failRead) {
        failRead = false;
        return route.fulfill({
          status: 403,
          json: { detail: "Owned fixture read unavailable" },
        });
      }
      return route.fulfill({ json: catalog() });
    }
    if (path.includes("/tools/mcp") && method !== "GET") {
      const body = r.postData() ? r.postDataJSON() : null;
      writes.push({ path, body });
      if (options.reject)
        return route.fulfill({
          status: 422,
          json: {
            detail:
              "Rejected: " +
              String(body?.api_key ?? "") +
              " <img src=x onerror=alert(1)>" +
              "x".repeat(20000),
          },
        });
      let result: unknown;
      if (path.endsWith("/discover")) {
        found = discovery;
        review = null;
        result = discovery;
      } else if (path.endsWith("/review")) {
        review = body;
        result = github();
      } else if (path.endsWith("/revoke")) {
        const g = grants.find((g) => path.includes(String(g.id)))!;
        g.active = false;
        result = g;
      } else if (path.includes("/connectors/github/grants")) {
        const g = {
          id: "9".repeat(32),
          active: true,
          project_id: projectId,
          repository_id: body.repository_id,
          connector: "github",
          credential_revision: discovery.credential_revision,
          review_digest: digest,
          tools: Object.fromEntries(
            body.tools.map((name: string) => [name, schema]),
          ),
          roles: body.roles,
          stages: body.stages,
          scope: { owner: "team", repo: "product" },
        };
        grants.push(g);
        result = g;
      } else {
        if (body.clear_secret) {
          configured = false;
          found = null;
          review = null;
          grants.forEach((g) => (g.active = false));
        }
        if (body.api_key) {
          configured = true;
          found = null;
          review = null;
        }
        enabled = body.enabled;
        result = github();
      }
      if (options.uncertain && path.endsWith(options.uncertain))
        return route.abort("failed");
      if (options.malformed && path.endsWith(options.malformed))
        return route.fulfill({ json: { unsupported: true } });
      if (options.readFailure) failRead = true;
      return route.fulfill({ json: result });
    }
    if (path.includes("/tools/mcp/projects/") && path.endsWith("/grants"))
      return route.fulfill({ json: grants });
    return route.fulfill({
      json: [
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
  await page.evaluate(() => document.fonts.ready);
  await navigate(page, "Administration");
  return { calls, writes, grants };
}
async function open(page: Page, saved = false) {
  await page
    .locator("main")
    .getByRole("button", { name: "External tools", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: saved ? "Manage GitHub tools" : "Set up GitHub tools",
      exact: true,
    })
    .click();
}
async function connect(page: Page, saved = false) {
  await open(page, saved);
  if (!saved) {
    await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
    await page.getByLabel("Enable this connection").check();
  }
  await page
    .getByRole("button", { name: "Save connection and continue", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Discover available tool schemas",
      exact: true,
    }),
  ).toBeVisible();
}
async function reviewSchemas(page: Page, saved = false) {
  await connect(page, saved);
  await page
    .getByRole("button", {
      name: "Discover schemas (remote request)",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review the exact tool schemas",
      exact: true,
    }),
  ).toBeVisible();
  await acknowledgeTools(page);
  await page
    .getByRole("button", { name: "Record schema review", exact: true })
    .click();
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(projectId);
  await page
    .getByLabel("Project repository", { exact: true })
    .selectOption(repositoryId);
}
async function acknowledgeTools(page: Page) {
  await page.getByLabel("Read repository files", { exact: true }).check();
  await page.getByLabel("Read a commit", { exact: true }).check();
  await page.getByLabel("I reviewed these tool names and parameters").check();
}
test("project users cannot read or mutate platform external tools", async ({
  page,
}) => {
  const { calls } = await fixture(page, { admin: false });
  await expect(
    page
      .locator("main")
      .getByRole("button", { name: "External tools", exact: true }),
  ).toBeDisabled();
  expect(calls.some((c) => c.includes("/tools/mcp"))).toBe(false);
});
test("catalog retains disabled templates and actual unmet prerequisites", async ({
  page,
}) => {
  const { calls } = await fixture(page);
  await page
    .locator("main")
    .getByRole("button", { name: "External tools", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Figma", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Vendor allowed-client/)).toBeVisible();
  await expect(page.getByText(/external stdio launcher/)).toBeVisible();
  expect(calls.filter((c) => c.startsWith("POST"))).toEqual([]);
});
for (const provider of ["Figma", "Sentry", "Grafana", "Playwright"])
  test(`${provider} setup is a guided prerequisite review without invented connection calls`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 375, height: 600 });
    const { calls, writes } = await fixture(page);
    await page
      .locator("main")
      .getByRole("button", { name: "External tools", exact: true })
      .click();
    await expect(
      page.getByRole("button", {
        name: `View ${provider} setup steps`,
        exact: true,
      }),
    ).toBeVisible();
    const initialCatalogReads = calls.filter((call) =>
      call.includes("/tools/mcp"),
    ).length;
    await page
      .getByRole("button", {
        name: `View ${provider} setup steps`,
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("heading", {
        name: `${provider}: required setup`,
        exact: true,
      }),
    ).toBeVisible();
    await expect
      .poll(() =>
        page
          .locator(".external-tools-wizard")
          .evaluate((element) =>
            element.style.getPropertyValue("--wizard-frame-height"),
          ),
      )
      .not.toBe("");
    const routeHeading = await page
        .getByRole("heading", { name: "External tools", exact: true })
        .boundingBox(),
      headerBounds = await page.locator(".workspace-main>header").boundingBox();
    expect(routeHeading!.y).toBeGreaterThanOrEqual(
      headerBounds!.y + headerBounds!.height,
    );
    await expect(
      page.getByRole("button", { name: "Continue", exact: true }),
    ).toBeEnabled();
    for (let step = 0; step < 3; step++) {
      const next = page.getByRole("button", { name: "Continue", exact: true });
      await next.focus();
      const box = await next.boundingBox(),
        footer = await page.locator(".workspace-main>footer").boundingBox();
      expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
      expect(
        await next.evaluate((button) => {
          const r = button.getBoundingClientRect();
          return button.contains(
            document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
          );
        }),
      ).toBe(true);
      await page.keyboard.press("Enter");
      await expect(
        page.locator('.wizard-steps [aria-current="step"]'),
      ).toContainText(String(step + 2));
      await expect(page.locator(".wizard-heading h2")).toBeFocused();
    }
    await expect(
      page.getByRole("button", {
        name:
          provider === "Playwright"
            ? "External template unavailable"
            : "Connection unavailable",
        exact: true,
      }),
    ).toBeDisabled();
    await expect(page.locator(".external-prerequisites input")).toHaveCount(0);
    await expect(page.locator(".external-prerequisites")).toContainText(
      "Not checked by this guide.",
    );
    if (provider === "Playwright")
      await expect(
        page.getByRole("link", {
          name: "Native runner setup guide",
          exact: true,
        }),
      ).toHaveAttribute("href", /agent-tools\/#configure-real-browser-checks$/);
    expect(writes).toHaveLength(0);
    expect(calls.filter((call) => call.includes("/tools/mcp")).length).toBe(
      initialCatalogReads,
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
test("save, remote discovery, exact review, scoped grant and explicit revocation remain separate", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  await connect(page);
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({
    enabled: true,
    api_key: "fixture-owned-key",
  });
  await expect(page.getByLabel("GitHub provider key")).toHaveCount(0);
  await page
    .getByRole("button", {
      name: "Discover schemas (remote request)",
      exact: true,
    })
    .click();
  expect(writes).toHaveLength(2);
  await page
    .getByText("Inspect parameters and exact schema", { exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("cell", { name: "owner", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Record schema review", exact: true }),
  ).toBeDisabled();
  await acknowledgeTools(page);
  await page
    .getByRole("button", { name: "Record schema review", exact: true })
    .click();
  expect(writes[2].body).toEqual({
    digest,
    tools: ["get_file_contents", "get_commit"],
  });
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(projectId);
  await expect(
    page.getByLabel("Project repository", { exact: true }).locator("option"),
  ).not.toContainText(["Another project's API"]);
  await page
    .getByLabel("Project repository", { exact: true })
    .selectOption(repositoryId);
  await page
    .getByRole("button", { name: "Grant project access", exact: true })
    .click();
  await expect(page.getByText("Active", { exact: true })).toBeVisible();
  expect(writes[3].body).toEqual({
    repository_id: repositoryId,
    review_digest: digest,
    tools: ["get_file_contents", "get_commit"],
    roles: ["implementer"],
    stages: ["implement"],
  });
  await page
    .getByRole("button", { name: "Grant project access", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Revoke the overlapping");
  expect(writes).toHaveLength(4);
  await page
    .getByRole("button", { name: "Revoke access", exact: true })
    .click();
  expect(writes).toHaveLength(4);
  await page
    .getByRole("button", { name: "Confirm revoke", exact: true })
    .click();
  await expect(page.getByText("Revoked", { exact: true })).toBeVisible();
  await page.getByText("Grant identity", { exact: true }).click();
  await expect(
    page.getByText(
      "Revoked. This grant no longer authorizes calls, including queued runs.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(writes).toHaveLength(5);
});
test("blank saved keys retain credential and discovery without another remote request", async ({
  page,
}) => {
  const { writes } = await fixture(page, { saved: true });
  await connect(page, true);
  expect(writes[0].body).toEqual({ enabled: true });
  await page
    .getByRole("button", { name: "Review saved discovery", exact: true })
    .click();
  expect(writes).toHaveLength(1);
  await page
    .getByText("Inspect exact discovery digest", { exact: true })
    .click();
  await expect(page.getByText(digest, { exact: true })).toBeVisible();
});
test("one grant can contain both execution roles while disjoint-role duplicates cannot create ambiguous snapshots", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  await reviewSchemas(page);
  await page.getByLabel("Documentation", { exact: true }).check();
  await page
    .getByRole("button", { name: "Grant project access", exact: true })
    .click();
  await expect(page.getByText("Active", { exact: true })).toBeVisible();
  expect(writes[3].body?.roles).toEqual(["implementer", "docs"]);
  await page.getByLabel("Implementer", { exact: true }).uncheck();
  await page
    .getByRole("button", { name: "Grant project access", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "one grant with all intended roles",
  );
  expect(writes).toHaveLength(4);
  expect(writes.some((item) => item.path.endsWith("/revoke"))).toBe(false);
});
for (const suffix of ["github", "discover", "review", "grants"])
  test(`unknown ${suffix} response locks replay until explicit reload`, async ({
    page,
  }) => {
    const { writes } = await fixture(page, { uncertain: suffix });
    if (suffix === "github") await open(page);
    else if (suffix === "discover" || suffix === "review") await connect(page);
    else await reviewSchemas(page);
    if (suffix === "github") {
      await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
      await page.getByLabel("Enable this connection").check();
      await page
        .getByRole("button", {
          name: "Save connection and continue",
          exact: true,
        })
        .click();
    }
    if (suffix === "discover" || suffix === "review")
      await page
        .getByRole("button", {
          name: "Discover schemas (remote request)",
          exact: true,
        })
        .click();
    if (suffix === "review") {
      await acknowledgeTools(page);
      await page
        .getByRole("button", { name: "Record schema review", exact: true })
        .click();
    }
    if (suffix === "grants")
      await page
        .getByRole("button", { name: "Grant project access", exact: true })
        .click();
    await expect(page.getByRole("alert")).toContainText(
      "outcome is not confirmed",
    );
    const n = writes.length;
    await expect(page.locator(".wizard-actions .primary")).toBeDisabled();
    await page
      .getByRole("button", { name: "Reload current state", exact: true })
      .click();
    await expect(page.getByRole("status")).toContainText(
      "Current state reloaded",
    );
    expect(writes).toHaveLength(n);
  });
test("malformed successful save never admits a secret or retries automatically", async ({
  page,
}) => {
  const { writes } = await fixture(page, { malformed: "github" });
  await open(page);
  await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
  await page.getByLabel("Enable this connection").check();
  await page
    .getByRole("button", { name: "Save connection and continue", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "outcome is not confirmed",
  );
  await expect(page.getByLabel("GitHub provider key")).toHaveValue("");
  expect(writes).toHaveLength(1);
});
test("a failed read after an admitted mutation cannot unlock a duplicate submission", async ({
  page,
}) => {
  const { writes } = await fixture(page, { readFailure: true });
  await open(page);
  await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
  await page.getByLabel("Enable this connection").check();
  await page
    .getByRole("button", { name: "Save connection and continue", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "outcome is not confirmed",
  );
  await expect(
    page.getByRole("button", {
      name: "Save connection and continue",
      exact: true,
    }),
  ).toBeDisabled();
  expect(writes).toHaveLength(1);
});
test("rejected key text is redacted, bounded and never evaluated", async ({
  page,
}) => {
  await fixture(page, { reject: true });
  await open(page);
  await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
  await page.getByLabel("Enable this connection").check();
  await page
    .getByRole("button", { name: "Save connection and continue", exact: true })
    .click();
  const alert = page.getByRole("alert");
  await expect(alert).not.toContainText("fixture-owned-key");
  expect((await alert.innerText()).length).toBeLessThan(1000);
  await expect(alert.locator("img,script")).toHaveCount(0);
  await expect(page.getByLabel("GitHub provider key")).toHaveValue("");
});
for (const width of [1440, 375])
  for (const theme of ["light", "dark"])
    test(`guided tools fit ${width}px ${theme} with actions above footer`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await fixture(page);
      await reviewSchemas(page);
      for (const name of ["Grant project access", "Back"]) {
        const button = page.getByRole("button", { name, exact: true });
        await button.scrollIntoViewIfNeeded();
        const box = await button.boundingBox(),
          footer = await page.locator(".workspace-main>footer").boundingBox();
        expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
        expect(
          await button.evaluate((element) => {
            const r = element.getBoundingClientRect();
            return element.contains(
              document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
            );
          }),
        ).toBe(true);
      }
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    });
test("short phone keyboard flow keeps focused actions usable above the footer", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 560 });
  await fixture(page);
  await open(page);
  const key = page.getByLabel("GitHub provider key");
  await key.focus();
  await page.keyboard.insertText("fixture-owned-key");
  await page.getByLabel("Enable this connection").focus();
  await page.keyboard.press("Space");
  const button = page.getByRole("button", {
    name: "Save connection and continue",
    exact: true,
  });
  await button.focus();
  const box = await button.boundingBox(),
    footer = await page.locator(".workspace-main>footer").boundingBox();
  expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
  expect(
    await button.evaluate((element) => {
      const r = element.getBoundingClientRect();
      return element.contains(
        document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
      );
    }),
  ).toBe(true);
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("heading", {
      name: "Discover available tool schemas",
      exact: true,
    }),
  ).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
for (const scale of [1, 2])
  test(`review actions and selected scope remain usable at ${scale}× on a short display`, async ({
    browser,
  }) => {
    const context = await browser.newContext({
      viewport: { width: 1440 / scale, height: 600 / scale },
      deviceScaleFactor: scale,
    });
    const page = await context.newPage();
    try {
      await fixture(page);
      await connect(page);
      await page
        .getByRole("button", {
          name: "Discover schemas (remote request)",
          exact: true,
        })
        .click();
      for (const theme of ["light", "dark"]) {
        await page
          .getByLabel("Appearance", { exact: true })
          .selectOption(theme);
        await acknowledgeTools(page);
        const action = page.getByRole("button", {
          name: "Record schema review",
          exact: true,
        });
        await action.focus();
        const box = await action.boundingBox(),
          footer = await page.locator(".workspace-main>footer").boundingBox(),
          header = await page.locator(".workspace-main>header").boundingBox();
        expect(box!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
        expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
        expect(
          await action.evaluate((button) => {
            const r = button.getBoundingClientRect();
            return (
              button.contains(
                document.elementFromPoint(
                  r.x + r.width / 2,
                  r.y + r.height / 2,
                ),
              ) && button.scrollWidth <= button.clientWidth
            );
          }),
        ).toBe(true);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
        await expect(page.locator(".wizard-guide")).toContainText("2 of 2");
      }
    } finally {
      await context.close();
    }
  });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1440, height: 600 },
  { width: 390, height: 844 },
  { width: 375, height: 600 },
])
  for (const provider of ["GitHub", "Figma", "Sentry", "Grafana", "Playwright"])
    test(`initial ${provider} steps keep heading, progress, fields and actions allocated at ${viewport.width}×${viewport.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await fixture(page);
      const frame = page.locator(".external-tools-wizard");
      const verify = async (stage: number) => {
        await expect(
          page.getByRole("button", {
            name: "Reload current state",
            exact: true,
          }),
        ).toBeEnabled();
        await page.evaluate(() => document.fonts.ready);
        await expect
          .poll(() =>
            frame.evaluate((element) =>
              element.style.getPropertyValue("--wizard-frame-height"),
            ),
          )
          .not.toBe("");
        await expect(frame).not.toHaveAttribute("data-layout", "flow");
        await expect
          .poll(() =>
            frame
              .locator(".wizard-field-scroll")
              .evaluate((element) => element.getBoundingClientRect().height),
          )
          .toBeGreaterThanOrEqual(64);
        const geometry = await page.evaluate(() => {
          const rect = (selector: string) => {
            const element = document.querySelector<HTMLElement>(selector)!;
            const { top, bottom, height } = element.getBoundingClientRect();
            return {
              top,
              bottom,
              height,
              overflow: getComputedStyle(element).overflowY,
            };
          };
          return {
            header: rect(".workspace-main>header"),
            footer: rect(".workspace-main>footer"),
            page: rect(".page-heading"),
            steps: rect(".wizard-steps"),
            heading: rect(".wizard-heading"),
            fields: rect(".wizard-field-scroll"),
            actions: rect(".wizard-actions"),
          };
        });
        expect(geometry.page.top).toBeGreaterThanOrEqual(
          geometry.header.bottom,
        );
        expect(geometry.steps.top).toBeGreaterThanOrEqual(geometry.page.bottom);
        expect(geometry.heading.top).toBeGreaterThanOrEqual(
          geometry.steps.bottom,
        );
        expect(geometry.fields.top).toBeGreaterThanOrEqual(
          geometry.heading.bottom,
        );
        expect(geometry.fields.height).toBeGreaterThanOrEqual(64);
        expect(geometry.fields.overflow).toBe("auto");
        expect(geometry.fields.bottom).toBeLessThanOrEqual(
          geometry.actions.top,
        );
        expect(geometry.actions.bottom).toBeLessThanOrEqual(
          geometry.footer.top,
        );
        for (const action of await frame
          .locator(".wizard-actions button")
          .all()) {
          expect(
            await action.evaluate((element) => {
              const rect = element.getBoundingClientRect();
              const hit = document.elementFromPoint(
                rect.x + rect.width / 2,
                rect.y + rect.height / 2,
              );
              return element.contains(hit)
                ? "action"
                : `${hit?.tagName}.${hit?.className}`;
            }),
            `${await action.innerText()}: ${JSON.stringify(await action.boundingBox())}`,
          ).toBe("action");
        }
        expect(
          await page
            .locator(".workspace-main>main")
            .evaluate((element) => element.scrollTop),
        ).toBe(0);
        writeFileSync(
          test.info().outputPath(`initial-step-${stage}.json`),
          JSON.stringify(
            { provider, stage, viewport, geometry, fixture_only: true },
            null,
            2,
          ),
        );
        await page.screenshot({
          path: test.info().outputPath(`initial-step-${stage}.png`),
          animations: "disabled",
        });
      };
      if (provider === "GitHub") {
        await open(page);
        await verify(0);
        await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
        await page.getByLabel("Enable this connection").check();
        await page
          .getByRole("button", {
            name: "Save connection and continue",
            exact: true,
          })
          .click();
        await verify(1);
        await page
          .getByRole("button", {
            name: "Discover schemas (remote request)",
            exact: true,
          })
          .click();
        await verify(2);
        await acknowledgeTools(page);
        await page
          .getByRole("button", { name: "Record schema review", exact: true })
          .click();
        await verify(3);
      } else {
        await page
          .locator("main")
          .getByRole("button", { name: "External tools", exact: true })
          .click();
        await page
          .getByRole("button", {
            name: `View ${provider} setup steps`,
            exact: true,
          })
          .click();
        for (let stage = 0; stage < 4; stage++) {
          await verify(stage);
          if (stage < 3)
            await page
              .getByRole("button", { name: "Continue", exact: true })
              .click();
        }
      }
      await frame.locator(".wizard-field-scroll").evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      });
      await frame.getByRole("button", { name: "Back", exact: true }).click();
      await expect(frame.locator("h2")).toBeFocused();
      expect(
        await page
          .locator(".workspace-main>main")
          .evaluate((element) => element.scrollTop),
      ).toBe(0);
      expect(
        await frame
          .locator(".wizard-field-scroll")
          .evaluate((element) => element.scrollTop),
      ).toBe(0);
      await verify(2);
    });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 375, height: 600 },
])
  test(`initial tool setup never displaces the route heading behind the header during allocation at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => {
      const original = Object.getOwnPropertyDescriptor(
        Element.prototype,
        "scrollTop",
      )!;
      const observations: { headingTop: number; headerBottom: number }[] = [];
      Object.defineProperty(window, "initialRouteGeometry", {
        value: observations,
      });
      Object.defineProperty(Element.prototype, "scrollTop", {
        ...original,
        set(value: number) {
          original.set!.call(this, value);
          if (
            !(this instanceof HTMLElement) ||
            !this.matches(".workspace-main>main") ||
            !this.querySelector(".external-tools-wizard")
          )
            return;
          const heading = this.querySelector("h1"),
            header = document.querySelector(".workspace-main>header");
          if (heading && header)
            observations.push({
              headingTop: heading.getBoundingClientRect().top,
              headerBottom: header.getBoundingClientRect().bottom,
            });
        },
      });
    });
    await fixture(page);
    await page
      .locator("main")
      .getByRole("button", { name: "External tools", exact: true })
      .click();
    await page
      .getByRole("button", { name: "View Sentry setup steps", exact: true })
      .click();
    await expect
      .poll(() =>
        page
          .locator(".external-tools-wizard")
          .evaluate((element) =>
            element.style.getPropertyValue("--wizard-frame-height"),
          ),
      )
      .not.toBe("");
    const geometry = await page.evaluate(
      () =>
        (
          window as Window & {
            initialRouteGeometry: {
              headingTop: number;
              headerBottom: number;
            }[];
          }
        ).initialRouteGeometry,
    );
    expect(geometry.length).toBeGreaterThan(0);
    expect(
      geometry.filter((rect) => rect.headingTop < rect.headerBottom),
    ).toEqual([]);
  });

for (const width of [1440, 390])
  test(`overflow has a visible gutter and scrolls fields without losing route context at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    await fixture(page);
    await page
      .locator("main")
      .getByRole("button", { name: "External tools", exact: true })
      .click();
    await page
      .getByRole("button", { name: "View Figma setup steps", exact: true })
      .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(
      page.locator('.wizard-steps [aria-current="step"]'),
    ).toContainText("Account & scope");
    const fields = page.locator(".wizard-field-scroll");
    await expect
      .poll(() =>
        fields.evaluate((element) => element.getBoundingClientRect().height),
      )
      .toBeGreaterThanOrEqual(64);
    expect(
      await fields.evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      ),
    ).toBe(true);
    expect(
      await fields.evaluate(
        (element) => element.offsetWidth - element.clientWidth,
      ),
    ).toBeGreaterThanOrEqual(6);
    await fields.hover();
    await page.mouse.wheel(0, 160);
    await expect
      .poll(() => fields.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    expect(
      await page
        .locator(".workspace-main>main")
        .evaluate((element) => element.scrollTop),
    ).toBe(0);
  });

for (const width of [1440, 390])
  test(`changing a provider step after scrolling its fields preserves initial route context at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    await fixture(page);
    await page
      .locator("main")
      .getByRole("button", { name: "External tools", exact: true })
      .click();
    await page
      .getByRole("button", { name: "View Figma setup steps", exact: true })
      .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .locator(".external-requirements article")
      .last()
      .scrollIntoViewIfNeeded();
    const next = page.getByRole("button", { name: "Continue", exact: true });
    await next.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".wizard-heading h2")).toHaveText(
      "Review the remaining factory setup",
    );
    await expect
      .poll(() =>
        page
          .locator(".workspace-main>main")
          .evaluate((element) => element.scrollTop),
      )
      .toBe(0);
  });

for (const width of [1440, 390])
  test(`returning from scrolled grants preserves the next provider's initial heading and allocation at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    await fixture(page);
    await reviewSchemas(page);
    await page
      .getByRole("button", { name: "Grant project access", exact: true })
      .click();
    await expect(page.getByText("Active", { exact: true })).toBeVisible();
    await page
      .getByRole("region", { name: "Current project grants", exact: true })
      .scrollIntoViewIfNeeded();
    const main = page.locator(".workspace-main>main");
    expect(await main.evaluate((element) => element.scrollTop)).toBeGreaterThan(
      0,
    );
    await page
      .getByRole("button", { name: "All external tools", exact: true })
      .click();
    await page
      .getByRole("button", { name: "View Figma setup steps", exact: true })
      .click();
    const frame = page.locator(".external-tools-wizard");
    await expect
      .poll(() =>
        frame.evaluate((element) =>
          element.style.getPropertyValue("--wizard-frame-height"),
        ),
      )
      .not.toBe("");
    await expect
      .poll(() => main.evaluate((element) => element.scrollTop))
      .toBe(0);
    const heading = await page
        .getByRole("heading", { name: "External tools", exact: true })
        .boundingBox(),
      header = await page.locator(".workspace-main>header").boundingBox(),
      actions = await frame.locator(".wizard-actions").boundingBox(),
      footer = await page.locator(".workspace-main>footer").boundingBox();
    expect(heading!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
    expect(actions!.y + actions!.height).toBeLessThanOrEqual(footer!.y);
  });

if (process.env.MCP_STUDIO_PROOF === "1" || process.env.UPDATE_DOC_SCREENSHOTS)
  for (const width of [1440, 390])
    for (const theme of ["light", "dark"])
      test(`actual fixture review frames ${width}px ${theme}`, async ({
        page,
      }) => {
        await page.setViewportSize({
          width,
          height: width === 1440 ? 900 : 844,
        });
        await page.emulateMedia({
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        const { writes } = await fixture(page);
        const frames: Record<string, unknown>[] = [];
        const output =
          process.env.MCP_STUDIO_PROOF_DIR ?? "test-results/mcp-proof";
        const metadata = process.env.MCP_STUDIO_PROOF_METADATA_DIR ?? output;
        mkdirSync(output, { recursive: true });
        mkdirSync(metadata, { recursive: true });
        const capture = async (
          stage: string,
          composition:
            "initial" | "fields-detail" | "grants-detail" = "initial",
        ) => {
          await expect(
            page.getByRole("button", {
              name: "Reload current state",
              exact: true,
            }),
          ).toBeEnabled();
          await page.evaluate(() => document.fonts.ready);
          const wizard = page.locator(".external-tools-wizard");
          if ((await wizard.count()) && composition !== "grants-detail") {
            await expect(wizard).not.toHaveAttribute("data-layout", "flow");
            await expect
              .poll(() =>
                wizard
                  .locator(".wizard-field-scroll")
                  .evaluate(
                    (element) => element.getBoundingClientRect().height,
                  ),
              )
              .toBeGreaterThanOrEqual(64);
            if (composition === "initial")
              await wizard
                .locator(".wizard-field-scroll")
                .evaluate((element) => {
                  element.scrollTop = 0;
                });
            await expect
              .poll(() =>
                page
                  .locator(".workspace-main>main")
                  .evaluate((element) => element.scrollTop),
              )
              .toBe(0);
            const heading = await page
                .getByRole("heading", { name: "External tools", exact: true })
                .boundingBox(),
              header = await page
                .locator(".workspace-main>header")
                .boundingBox();
            expect(heading!.y).toBeGreaterThanOrEqual(
              header!.y + header!.height,
            );
            await expect
              .poll(() =>
                page.evaluate(() => {
                  const actions = document
                    .querySelector(".external-tools-wizard .wizard-actions")!
                    .getBoundingClientRect();
                  const footer = document
                    .querySelector(".workspace-main>footer")!
                    .getBoundingClientRect();
                  return actions.bottom <= footer.top;
                }),
              )
              .toBe(true);
          }
          expect(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth,
            ),
          ).toBe(true);
          await expect(page.locator(".workspace-main>footer")).toContainText(
            `v${studioPackage.version}`,
          );
          if (process.env.UPDATE_DOC_SCREENSHOTS) {
            await captureWalkthrough(
              page,
              `external-${stage}`,
              theme,
              width === 1440 ? "desktop" : "mobile",
              {
                producer: "studio/tests/external-tools.spec.ts",
                resetFields: false,
                actionSelector:
                  composition === "grants-detail"
                    ? ".external-grants button"
                    : ".wizard-actions button",
              },
            );
            return;
          }
          const filename = `forge-mcp-fixture-${stage}-${width}-${theme}-draft.png`;
          await page.screenshot({
            path: join(output, filename),
            animations: "disabled",
          });
          frames.push({
            stage,
            composition,
            path: filename,
            viewport: { width, height: width === 1440 ? 900 : 844 },
            fixture_only: true,
            studio_version: studioPackage.version,
            writes: writes.map((item) => ({
              path: item.path,
              secret_supplied: !!item.body?.api_key,
            })),
            footer: await page.locator(".workspace-main>footer").boundingBox(),
          });
        };
        await page
          .locator("main")
          .getByRole("button", { name: "External tools", exact: true })
          .click();
        await expect(
          page.getByRole("heading", { name: "GitHub", exact: true }),
        ).toBeVisible();
        await capture("catalog");
        await page
          .getByRole("button", { name: "Set up GitHub tools", exact: true })
          .click();
        await page.getByLabel("GitHub provider key").fill("fixture-owned-key");
        await page.getByLabel("Enable this connection").check();
        await capture("connection");
        await page
          .getByRole("button", {
            name: "Save connection and continue",
            exact: true,
          })
          .click();
        await expect(
          page.getByRole("heading", {
            name: "Discover available tool schemas",
            exact: true,
          }),
        ).toBeVisible();
        await capture("discovery");
        await page
          .getByRole("button", {
            name: "Discover schemas (remote request)",
            exact: true,
          })
          .click();
        await expect(
          page.getByRole("heading", {
            name: "Review the exact tool schemas",
            exact: true,
          }),
        ).toBeVisible();
        await capture("review-overview");
        await page
          .getByText("Inspect parameters and exact schema", { exact: true })
          .first()
          .click();
        await page
          .getByText("Inspect parameters and exact schema", { exact: true })
          .first()
          .scrollIntoViewIfNeeded();
        await capture("review-schema-detail", "fields-detail");
        await acknowledgeTools(page);
        await capture("review-confirmation-detail", "fields-detail");
        await page
          .getByRole("button", { name: "Record schema review", exact: true })
          .click();
        await page
          .getByLabel("Product project", { exact: true })
          .selectOption(projectId);
        await page
          .getByLabel("Project repository", { exact: true })
          .selectOption(repositoryId);
        await capture("project");
        await page
          .getByLabel("Project repository", { exact: true })
          .scrollIntoViewIfNeeded();
        await capture("project-fields", "fields-detail");
        const grant = page.getByRole("button", {
          name: "Grant project access",
          exact: true,
        });
        await page.locator(".external-role-fields").scrollIntoViewIfNeeded();
        await grant.scrollIntoViewIfNeeded();
        await capture("project-roles-detail", "fields-detail");
        await grant.click();
        await expect(page.getByText("Active", { exact: true })).toBeVisible();
        await page
          .getByRole("region", { name: "Current project grants", exact: true })
          .scrollIntoViewIfNeeded();
        await capture("granted", "grants-detail");
        for (const provider of ["Figma", "Sentry", "Grafana", "Playwright"]) {
          await page
            .getByRole("button", { name: "All external tools", exact: true })
            .click();
          await page
            .getByRole("button", {
              name: `View ${provider} setup steps`,
              exact: true,
            })
            .click();
          for (const state of [
            "requirements",
            "account-scope",
            "factory-setup",
            "availability",
          ]) {
            await expect(
              page.locator('.wizard-steps [aria-current="step"]'),
            ).toContainText(
              {
                requirements: "Requirements",
                "account-scope":
                  provider === "Playwright"
                    ? "Runner profile"
                    : "Account & scope",
                "factory-setup": "Factory setup",
                availability: "Availability",
              }[state]!,
            );
            await capture(`${provider.toLowerCase()}-${state}`);
            if (state === "account-scope") {
              await page
                .locator(".external-requirements article")
                .last()
                .scrollIntoViewIfNeeded();
              await capture(
                `${provider.toLowerCase()}-${state}-detail`,
                "fields-detail",
              );
            }
            if (state !== "availability") {
              const next = page.getByRole("button", {
                name: "Continue",
                exact: true,
              });
              await next.focus();
              const box = await next.boundingBox(),
                footer = await page
                  .locator(".workspace-main>footer")
                  .boundingBox();
              expect(box!.y + box!.height).toBeLessThanOrEqual(footer!.y);
              await page.keyboard.press("Enter");
            }
          }
        }
        writeFileSync(
          join(metadata, `frames-${width}-${theme}.json`),
          JSON.stringify(frames, null, 2) + "\n",
        );
      });

test("project grants loading preserves the route heading and allocated wizard", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
  const { writes } = await fixture(page);
  await connect(page);
  await page
    .getByRole("button", {
      name: "Discover schemas (remote request)",
      exact: true,
    })
    .click();
  await acknowledgeTools(page);
  await page
    .getByRole("button", { name: "Record schema review", exact: true })
    .click();
  await expect(
    page.locator('.wizard-steps [aria-current="step"]'),
  ).toContainText("Project access");
  await expect(
    page.getByRole("button", { name: "Reload current state", exact: true }),
  ).toBeEnabled();
  let release = () => {};
  let received = () => {};
  let heldRequests = 0;
  const responseHeld = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requestReceived = new Promise<void>((resolve) => {
    received = resolve;
  });
  await page.route(
    `**/api/v1/tools/mcp/projects/${projectId}/grants`,
    async (route) => {
      expect(route.request().method()).toBe("GET");
      heldRequests++;
      received();
      await responseHeld;
      await route.fallback();
    },
  );
  const verify = async (phase: string) => {
    const frame = page.locator(".external-tools-wizard");
    await expect(frame).not.toHaveAttribute("data-layout", "flow");
    await expect
      .poll(() =>
        frame.evaluate((element) => {
          const main = element.closest("main")!;
          const box = element.getBoundingClientRect();
          const offset =
            box.top - main.getBoundingClientRect().top + main.scrollTop;
          const remaining =
            main.clientHeight -
            offset -
            parseFloat(getComputedStyle(main).paddingBottom) -
            16;
          return Math.abs(box.height - remaining);
        }),
      )
      .toBeLessThanOrEqual(1);
    const geometry = await page.evaluate(() => {
      const main = document.querySelector(".workspace-main>main")!;
      const header = document
        .querySelector(".workspace-main>header")!
        .getBoundingClientRect();
      const footer = document
        .querySelector(".workspace-main>footer")!
        .getBoundingClientRect();
      const heading = document
        .querySelector(".external-page-heading")!
        .getBoundingClientRect();
      const actions = Array.from(
        document.querySelectorAll(
          ".external-tools-wizard .wizard-actions button",
        ),
      ).map((button) => {
        const box = button.getBoundingClientRect();
        return {
          top: box.top,
          bottom: box.bottom,
          hit: button.contains(
            document.elementFromPoint(box.x + box.width / 2, box.bottom - 2),
          ),
        };
      });
      return {
        mainScrollTop: main.scrollTop,
        headingTop: heading.top,
        headerBottom: header.bottom,
        footerTop: footer.top,
        actions,
      };
    });
    await info.attach(`project-grants-${phase}-geometry`, {
      body: JSON.stringify(geometry),
      contentType: "application/json",
    });
    await page.screenshot({
      path: info.outputPath(`project-grants-${phase}.png`),
      animations: "disabled",
    });
    expect(geometry.mainScrollTop).toBe(0);
    expect(geometry.headingTop).toBeGreaterThanOrEqual(geometry.headerBottom);
    expect(geometry.actions).toHaveLength(2);
    for (const action of geometry.actions) {
      expect(action.top).toBeGreaterThanOrEqual(geometry.headerBottom);
      expect(action.bottom).toBeLessThanOrEqual(geometry.footerTop);
      expect(action.hit).toBe(true);
    }
  };
  try {
    await page
      .getByLabel("Product project", { exact: true })
      .selectOption(projectId);
    await requestReceived;
    await expect(
      page.locator('.external-tools > .external-hint[role="status"]'),
    ).toHaveText("Reading current factory state…");
    await verify("loading");
    expect(heldRequests).toBe(1);
    release();
    await expect(
      page.locator('.external-tools > .external-hint[role="status"]'),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Reload current state", exact: true }),
    ).toBeEnabled();
    await verify("resolved");
    expect(heldRequests).toBe(1);
    expect(writes).toHaveLength(3);
  } finally {
    release();
  }
});
