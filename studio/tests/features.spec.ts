/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { expectProgressFits } from "./wizard-progress";
async function connect(page: Page) {
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
}
const repository = {
  id: "repo-1",
  name: "Inventory",
  url: "https://github.com/example/inventory",
  provider: "github",
  connection_provider: "github",
  remote_repository_id: "42",
  default_branch: "main",
};
const definition = {
  customer: "Warehouse operators",
  problem: "Prevent over-reservation",
  owners: ["Inventory team"],
  journey: ["Inspect stock", "Reserve stock"],
  user_stories: ["As an operator I reserve available stock"],
  acceptance: ["Over-reservation returns 409"],
  security_requirements: [],
  nonfunctional_requirements: [],
};
const architecture = {
  backend: "pyfly",
  frontend: "react",
  components: [
    {
      name: "Inventory",
      layer: "domain",
      responsibility: "Enforce stock invariants",
    },
  ],
  decisions: ["Use transaction boundaries"],
  nonfunctional_requirements: [],
  reviewed: false,
};
async function fallback(
  page: Page,
  handler: (
    path: string,
    method: string,
    body: Record<string, unknown> | null,
  ) => unknown | undefined,
) {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/knowledge/explore")) {
      await route.fallback();
      return;
    }
    const result = handler(
      path,
      request.method(),
      request.postData() ? request.postDataJSON() : null,
    );
    if (result !== undefined) {
      await route.fulfill({ json: result });
      return;
    }
    if (path.endsWith("/repositories"))
      await route.fulfill({ json: [repository] });
    else if (path.endsWith("/runs")) await route.fulfill({ json: [] });
    else if (path.endsWith("/providers"))
      await route.fulfill({ json: [{ id: "codex", ready: true }] });
    else if (path.endsWith("/deployment-targets"))
      await route.fulfill({ json: [] });
    else await route.fulfill({ json: {} });
  });
}
test("product wizard retains drafts, reviews architecture and freezes saved revision into a run", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 900 });
  let snapshot: unknown = null;
  let saved: Record<string, unknown> | null = null;
  let brandInput: Record<string, unknown> | null = null;
  let runInput: Record<string, unknown> | null = null;
  await fallback(page, (path, method, body) => {
    if (path.endsWith("/product")) {
      if (method === "PUT") {
        saved = body;
        snapshot = {
          repository_id: "repo-1",
          revision: 1,
          definition: body,
          digest: "product-digest",
          created_at: "2026-10-12T18:00:00Z",
        };
      }
      return snapshot;
    }
    if (path.endsWith("/design/preview")) {
      const svg =
        '<svg xmlns="http://www.w3.org/2000/svg" width="390" height="844"><text x="20" y="40">Wireframe reference</text></svg>';
      return {
        digest: "a".repeat(64),
        definition_digest: "definition",
        brand_digest: null,
        method: "wireframe",
        font_family: "Manrope",
        coverage: { journeys: 2, screen_count: 2 },
        screens: [1, 2].map((index) => ({
          id: `journey-0${index}`,
          title: `Journey ${index}`,
          desktop_svg: svg,
          mobile_svg: svg,
          desktop_dark_svg: svg,
          mobile_dark_svg: svg,
          interaction_contract: {
            journey: `Customer step ${index}`,
            acceptance_ids: ["acceptance-01"],
            primary_action: "Continue",
            states: ["default", "loading", "empty", "error"],
          },
        })),
      };
    }
    if (path.endsWith("/brand")) {
      if (method === "PUT") {
        brandInput = body;
        return {
          repository_id: "repo-1",
          revision: "brand-revision",
          name: body?.name,
          font_family: "Manrope",
          digest: "brand-digest",
          files: {
            "logo.svg":
              '<svg xmlns="http://www.w3.org/2000/svg"><text>Inventory</text></svg>',
            "tokens.json": "{}",
            "theme.css": "",
            "brand-guide.md": "Manrope",
          },
          tokens: {},
        };
      }
      return null;
    }
    if (path.endsWith("/architecture/propose"))
      return {
        method: "rule_based",
        recommendation: {
          backend: "pyfly",
          frontend: "react",
          rationale: "Transaction-oriented product",
          alternatives: [],
        },
        architecture,
      };
    if (path.endsWith("/skills")) return { skills: [] };
    if (path.endsWith("/policies")) return { skills: [] };
    if (path.endsWith("/runs") && method === "POST") {
      runInput = body;
      return {
        id: "run-1",
        repository_id: "repo-1",
        brief: "Implement inventory reservations",
        status: "queued",
      };
    }
  });
  await connect(page);
  await navigate(page, "Product definition");
  await page.getByLabel("Customer or user").fill(definition.customer);
  await page.getByLabel("Problem to solve").fill(definition.problem);
  await page.getByLabel("Product owners").fill("Inventory team");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "What does the customer need to do?",
      exact: true,
    }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Ask about this step", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByLabel("Customer journey", { exact: true }),
  ).toBeFocused();
  await page
    .getByLabel("Customer journey", { exact: true })
    .fill(definition.journey[0]);
  await page
    .getByRole("button", { name: "Add journey step", exact: true })
    .click();
  await page
    .getByLabel("Journey step 2", { exact: true })
    .fill(definition.journey[1]);
  await page.getByLabel("User stories").fill(definition.user_stories[0]);
  await page.getByRole("button", { name: "Back", exact: true }).click();
  await expect(page.getByLabel("Customer or user")).toHaveValue(
    definition.customer,
  );
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByLabel("Customer journey", { exact: true }),
  ).toHaveValue(definition.journey[0]);
  await expect(page.getByLabel("Journey step 2", { exact: true })).toHaveValue(
    definition.journey[1],
  );
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Acceptance criteria").fill(definition.acceptance[0]);
  await page
    .getByRole("button", { name: "Propose architecture", exact: true })
    .click();
  await expect(page.getByText("Transaction-oriented product")).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Design settings", exact: true })
    .click();
  await page
    .getByLabel("Configure a product brand kit", { exact: true })
    .check();
  await page
    .getByLabel("Product brand name", { exact: true })
    .fill("Inventory");
  await page.getByLabel("Spacing baseline").selectOption("8");
  await page.locator("#product-grid-mobile-viewport_width").fill("414");
  await page
    .getByRole("button", { name: "Save design settings", exact: true })
    .click();
  expect(brandInput).toMatchObject({
    name: "Inventory",
    font_family: "Manrope",
    primary: "#ffb347",
  });
  await page
    .getByLabel(
      "I reviewed the architecture layers, responsibilities and decisions.",
    )
    .check();
  await page
    .getByRole("button", { name: "Review reference views", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Generate wireframes", exact: true })
    .click();
  await expect(
    page.getByRole("img", { name: /wireframe reference/ }),
  ).toHaveCount(1);
  await page
    .getByRole("button", { name: "Done reviewing journeys", exact: true })
    .click();
  await page
    .getByRole("button", { name: /Save (?:draft|reviewed) definition/ })
    .click();
  await expect(page.getByRole("alert")).toContainText("Review every screen");
  await page.getByRole("button", { name: /Review journey references/ }).click();
  for (const index of [1, 2]) {
    await page
      .getByRole("button", {
        name: `Journey ${index}: Journey ${index}`,
        exact: true,
      })
      .click();
    for (const viewport of ["Desktop", "Mobile"]) {
      await page
        .getByRole("group", { name: "Preview viewport" })
        .getByRole("button", { name: viewport, exact: true })
        .click();
      for (const theme of ["Light", "Dark"]) {
        await page
          .getByRole("group", { name: "Preview theme" })
          .getByRole("button", { name: theme, exact: true })
          .click();
        await expect(
          page.getByRole("img", {
            name: new RegExp(
              `${viewport.toLowerCase()} ${theme.toLowerCase()} wireframe`,
            ),
          }),
        ).toBeVisible();
        await expect
          .poll(() => page.getByText(/Viewed [0-4]\/4 variants/).innerText())
          .not.toContain("Viewed 0");
      }
    }
    const acknowledgement = page.getByLabel(
      "I reviewed this journey's desktop and mobile references in light and dark themes.",
      { exact: true },
    );
    await expect(acknowledgement).toBeEnabled();
    await acknowledgement.check();
  }
  await page
    .getByRole("button", { name: "Done reviewing journeys", exact: true })
    .click();
  await page
    .getByRole("button", { name: /Save (?:draft|reviewed) definition/ })
    .click();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  expect(
    (
      saved as unknown as typeof definition & {
        architecture: typeof architecture;
      }
    ).architecture.reviewed,
  ).toBe(true);
  expect(saved).toMatchObject({
    visual_design: { baseline: 8, mobile: { viewport_width: 414 } },
  });
  await page.getByRole("button", { name: /Start run.*revision 1/ }).click();
  await page
    .getByLabel("What should this change accomplish?")
    .fill("Implement inventory reservations");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("button", { name: "Review run", exact: true }).click();
  await page
    .getByRole("button", { name: "Start building", exact: true })
    .click();
  await expect.poll(() => runInput?.product_definition).toEqual(snapshot);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
});
test("cloud recipe renders before explicit save and never represents rendering as provisioning", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 900 });
  let rendered = false;
  let saved = false;
  let target: unknown;
  const recipe = {
    id: "recipe-1",
    provider: "vps",
    content_digest: "digest",
    files: { "inventory.yml": "host: 192.0.2.10" },
    target: { name: "inventory", repository_id: "repo-1" },
    requirements: ["SSH access required"],
    validation: {
      state: "rendered",
      infrastructure_applied: false,
      cloud_account_verified: false,
    },
  };
  await fallback(page, (path, method, body) => {
    if (path.endsWith("/deployment-credentials"))
      return [{ id: "operator", configured_keys: ["SSH_AUTH_SOCK"] }];
    if (path.endsWith("/cloud/providers"))
      return [
        {
          id: "vps",
          name: "Existing VPS",
          tool: "Ansible",
          template_version: "1.0.0",
          fields: [{ key: "host", required: true, default: "" }],
          credential_names: ["SSH_AUTH_SOCK"],
        },
      ];
    if (path.endsWith("/recipes/render")) {
      rendered = true;
      return recipe;
    }
    if (path.endsWith("/recipes") && method === "POST") {
      saved = true;
      return recipe;
    }
    if (path.endsWith("/configure-target")) {
      target = body;
      return { id: "staging" };
    }
  });
  await connect(page);
  await navigate(page, "Deployment");
  await page.getByRole("button", { name: "Cloud or host recipe" }).click();
  await page.getByLabel("Cloud project").selectOption("repo-1");
  await page.getByLabel("Cloud or host provider").selectOption("vps");
  await page.getByLabel("Recipe name").fill("inventory");
  await page
    .getByLabel("Immutable image digest")
    .fill(`registry.example/inventory@sha256:${"a".repeat(64)}`);
  await page.getByLabel("Application domain").fill("inventory.example.com");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Dedicated host").fill("192.0.2.10");
  await page.getByLabel("Administration IPv4 CIDR").fill("203.0.113.1/32");
  await page
    .getByLabel("SSH public key")
    .fill("ssh-ed25519 operator-public-test-key");
  await page.getByLabel("Private credential profile ID").fill("operator");
  await page
    .getByRole("button", { name: "Render recipe", exact: true })
    .click();
  expect(rendered).toBe(true);
  expect(saved).toBe(false);
  await expect(
    page.getByText(
      "Rendered only · infrastructure not applied · cloud account not verified",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Deployment target ID").fill("staging");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
  await page.getByRole("button", { name: "Save recipe and target" }).click();
  await expect.poll(() => target).toEqual({ target_id: "staging" });
  await expect(
    page.getByText(/Infrastructure has not been applied/),
  ).toBeVisible();
});
test("knowledge browse keeps project and framework records separate and search shows canonical citations", async ({
  page,
}) => {
  const browseScopes: string[] = [];
  await page.route("**/api/v1/knowledge/explore?**", async (route) => {
    const url = new URL(route.request().url());
    const scope = url.searchParams.get("scope")!;
    browseScopes.push(scope);
    await route.fulfill({
      json: {
        workspace_id:
          scope === "project" ? "project-workspace" : "framework-workspace",
        scope,
        sources: [
          {
            id: `${scope}-source`,
            kind: "document",
            status: "indexed",
            filename: `${scope}.md`,
            content_sha256: "sha",
            content_bytes: 100,
            n_chunks: 2,
            created_at: "2026-10-12T18:00:00Z",
            updated_at: "2026-10-12T18:00:00Z",
          },
        ],
        knowledge: [],
        offset: 0,
        limit: 50,
        totals: { sources: 1, knowledge: 0 },
        browsing_available: true,
        warnings: [],
      },
    });
  });
  await fallback(page, (path) => {
    if (path.endsWith("/knowledge/configuration"))
      return {
        enabled: true,
        framework_workspace: "framework-workspace",
        readiness: { state: "configured" },
      };
    if (path.endsWith("/knowledge/search"))
      return {
        hits: [
          {
            workspace_id: "project-workspace",
            source_id: "project-source",
            chunk_id: "chunk-1",
            content: "Reject invalid reservation amounts.",
            source_uri:
              "https://github.com/example/inventory/blob/main/README.md",
          },
        ],
        warnings: [],
      };
  });
  await connect(page);
  await navigate(page, "Knowledge");
  await expect(page.getByText("project.md", { exact: true })).toBeVisible();
  await page.getByLabel("Browse scope").selectOption("framework");
  await expect(page.getByText("framework.md", { exact: true })).toBeVisible();
  await expect(page.getByText("project.md", { exact: true })).toHaveCount(0);
  await page.getByRole("tab", { name: "Search", exact: true }).click();
  await page.getByLabel("Knowledge query").fill("Reservation constraints");
  await page
    .getByRole("button", { name: "Search knowledge", exact: true })
    .click();
  await expect(
    page.getByText("Reject invalid reservation amounts."),
  ).toBeVisible();
  await page.getByText("Citation details").click();
  await expect(
    page.getByRole("link", { name: /github.com\/example/ }),
  ).toHaveAttribute(
    "href",
    "https://github.com/example/inventory/blob/main/README.md",
  );
  expect(browseScopes).toContain("framework");
});

test("repository workflow saves write-only signing settings and disables Bitbucket issue authoring", async ({
  page,
}) => {
  let input: Record<string, unknown> | null = null;
  const initial = {
    repository_id: "repo-1",
    enabled: false,
    trusted_actors: [],
    webhook_secret_configured: false,
    backend: "pyfly",
    frontend: "react",
    agent: "codex",
    publish: false,
    allowed_commands: ["run", "test", "maintain", "status"],
    test_commands: [],
    deploy_enabled: false,
    deploy_targets: [],
    capabilities: {
      issue_authoring: false,
      issue_comments: false,
      pull_request_comments: true,
    },
  };
  await fallback(page, (path, method, body) => {
    if (path.endsWith("/repositories"))
      return [
        {
          ...repository,
          provider: "bitbucket",
          connection_provider: "bitbucket",
        },
      ];
    if (path.endsWith("/collaboration")) {
      if (method === "PUT") {
        input = body;
        return { ...initial, ...body, webhook_secret_configured: true };
      }
      return initial;
    }
    if (path.endsWith("/collaboration/receipts")) return [];
  });
  await connect(page);
  await navigate(page, "Repository workflow");
  await page.getByLabel("Enable repository commands").check();
  await page
    .getByLabel("Trusted provider actor IDs")
    .fill("{provider-actor-id}");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Operator verification commands")
    .fill('[["make","check"]]');
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("New webhook signing secret")
    .fill("test-signing-secret");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Save workflow", exact: true })
    .click();
  await expect.poll(() => input?.webhook_secret).toBe("test-signing-secret");
  await expect(
    page.getByText(
      "Workflow settings saved. Incoming deliveries are recorded as receipts.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Draft an issue" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: /Webhook$/ }).click();
  await expect(page.getByLabel("New webhook signing secret")).toHaveValue("");
});

test("project board shows only actual project runs, branches, tickets and plan task states", async ({
  page,
}) => {
  const run = {
    id: "run-1",
    repository_id: "repo-1",
    brief: "Verify reservation boundaries",
    status: "succeeded",
    branch: "forge/actual-checkpoint",
    issue_context: {
      provider: "github",
      receipt_id: "receipt-1",
      actor: "1",
      kind: "issue",
      number: 9,
      title: "Reservation behavior",
      url: "https://github.com/example/inventory/issues/9",
    },
  };
  await fallback(page, (path) => {
    if (path.endsWith("/runs"))
      return [
        run,
        {
          id: "other-run",
          repository_id: "other-repository",
          brief: "Unrelated project work",
          status: "failed",
        },
      ];
    if (path.endsWith("/runs/run-1/plan"))
      return {
        tasks: [
          {
            id: "verify",
            title: "Run project checks",
            status: "running",
            instructions: "Execute recorded checks",
            attempt: 1,
          },
        ],
      };
    if (path.endsWith("/deployments")) return [];
  });
  await connect(page);
  await navigate(page, "Factory");
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue(
    "repo-1",
  );
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await expect(
    page.getByText("forge/actual-checkpoint", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Unrelated project work", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "#9 Reservation behavior" }),
  ).toHaveAttribute("href", "https://github.com/example/inventory/issues/9");
  const task = page.getByRole("region", { name: "Selected run tasks" });
  await expect(task.getByText("running", { exact: true })).toBeVisible();
  await expect(task.getByText("done", { exact: true })).toHaveCount(0);
});

test("credential profile wizard sends write-only keys and clears values after save", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 900 });
  let configured: string[] = [];
  let submitted: Record<string, unknown> | null = null;
  await fallback(page, (path, method, body) => {
    if (path.endsWith("/cloud/providers"))
      return [
        {
          id: "aws",
          name: "AWS EC2",
          tool: "OpenTofu + Ansible",
          template_version: "1.0.0",
          fields: [],
          credential_names: ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
        },
      ];
    if (path.endsWith("/deployment-credentials"))
      return configured.length
        ? [{ id: "operator", configured_keys: configured }]
        : [];
    if (path.endsWith("/deployment-credentials/operator") && method === "PUT") {
      submitted = body;
      configured = Object.keys(body?.values ?? {});
      return { id: "operator", configured_keys: configured };
    }
  });
  await connect(page);
  await navigate(page, "Deployment");
  await page.getByRole("button", { name: "Cloud or host recipe" }).click();
  await page.getByLabel("Cloud project").selectOption("repo-1");
  await page.getByLabel("Cloud or host provider").selectOption("aws");
  await page.getByLabel("Recipe name").fill("inventory");
  await page
    .getByLabel("Immutable image digest")
    .fill(`registry.example/inventory@sha256:${"a".repeat(64)}`);
  await page.getByLabel("Application domain").fill("inventory.example.com");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expectProgressFits(
    page.getByRole("list", { name: "Setup progress" }),
    "Infrastructure",
  );
  await page
    .getByRole("button", { name: "Configure private credentials" })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Deployment credential profile",
  });
  await dialog.getByLabel("Credential profile ID").fill("operator");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog
    .getByLabel("AWS_ACCESS_KEY_ID", { exact: true })
    .fill("test-key-id");
  await dialog
    .getByLabel("AWS_SECRET_ACCESS_KEY", { exact: true })
    .fill("test-private-value");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByText("test-private-value", { exact: true }),
  ).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Save private profile", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
  expect(submitted).toEqual({
    values: {
      AWS_ACCESS_KEY_ID: "test-key-id",
      AWS_SECRET_ACCESS_KEY: "test-private-value",
    },
    clear_keys: [],
  });
  await expect(page.getByLabel("Private credential profile ID")).toHaveValue(
    "operator",
  );
  await page
    .getByRole("button", { name: "Configure private credentials" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Continue", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByLabel("AWS_SECRET_ACCESS_KEY", { exact: true }),
  ).toHaveValue("");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Close credential profile" })
    .click();
});
