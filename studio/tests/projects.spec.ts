/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { expectProgressFits } from "./wizard-progress";
import { retainedRunCreation } from "./run-creation-cancellation-fixture";
const repositories = [
  {
    id: "a".repeat(32),
    name: "Inventory API",
    provider: "github",
    url: "https://github.com/example/api",
    default_branch: "main",
  },
  {
    id: "b".repeat(32),
    name: "Customer portal",
    provider: "github",
    url: "https://github.com/example/portal",
    default_branch: "main",
  },
];
test("logical product preserves independent component contracts and canonical design source in component runs", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let stored: Record<string, unknown> | null = null,
    run: Record<string, unknown> | null = null;
  const creation = retainedRunCreation("e".repeat(32));
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname,
      body = req.postData() ? req.postDataJSON() : null;
    let json: unknown = {};
    if (path.endsWith("/repositories")) json = repositories;
    if (/\/run-creation-requests\/[a-f0-9]{32}$/.test(path)) {
      expect(req.method()).toBe("POST");
      json = await creation.admit(path, body);
      run = creation.input;
    }
    if (path.endsWith("/runs")) {
      expect(req.method()).toBe("GET");
      json = creation.run ? [creation.run] : [];
    }
    if (path.endsWith(`/runs/${"e".repeat(32)}`)) json = creation.run;
    if (path.endsWith("/providers")) json = [{ id: "codex", ready: true }];
    if (path.endsWith("/deployment-targets")) json = [];
    if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    if (path.endsWith("/skills") || path.endsWith("/policies"))
      json = { skills: [] };
    if (path.endsWith("/analyses")) json = [];
    if (path.endsWith("/projects")) {
      if (req.method() === "POST") {
        stored = {
          id: "c".repeat(32),
          revision: 1,
          digest: "d".repeat(64),
          definition: body,
          created_at: "2026-10-12T00:00:00Z",
          updated_at: "2026-10-12T00:00:00Z",
        };
        json = stored;
      } else json = stored ? [stored] : [];
    }
    if (path.endsWith("/projects/" + "c".repeat(32)) && stored)
      json = {
        ...stored,
        components: (
          stored.definition as { components: Record<string, unknown>[] }
        ).components.map((item) => ({
          ...item,
          latest_status: "not_started",
          repository_run_count: 0,
          runs: [],
        })),
        counts: { not_started: 2 },
      };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product projects");
  await page
    .getByRole("button", { name: "Create product project", exact: true })
    .click();
  await page.getByLabel("Product project name").fill("Inventory product");
  await page.getByLabel("Product scope").fill("API plus customer portal");
  await page.getByLabel("Project owners").fill("Product team");
  await page.getByLabel("Repository topology").selectOption("multi_repo");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Component name").fill("Inventory API");
  await page
    .getByLabel("Component responsibility")
    .fill("Enforce inventory invariants");
  await page
    .getByLabel("Component verification commands")
    .fill('[["uv","run","pytest"]]');
  await page
    .getByRole("button", { name: "Add component", exact: true })
    .click();
  await page.getByLabel("Component name").fill("Portal");
  await page.getByLabel("Component kind").selectOption("web");
  await page.getByLabel("Component frontend").selectOption("vue");
  await page.getByLabel("Source root").fill(".");
  await page
    .getByLabel("Component responsibility")
    .fill("Accessible inventory workflow");
  await page
    .getByLabel("Component verification commands")
    .fill('[["npm","run","check"]]');
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const sourceBounds = await page
    .getByLabel("Product definition and design system", { exact: true })
    .boundingBox();
  const scrollBounds = await page.locator(".wizard-field-scroll").boundingBox();
  expect(sourceBounds!.y).toBeGreaterThanOrEqual(scrollBounds!.y);
  expect(sourceBounds!.y + sourceBounds!.height).toBeLessThanOrEqual(
    scrollBounds!.y + scrollBounds!.height,
  );
  await page.setViewportSize({ width: 320, height: 568 });
  await expectProgressFits(
    page.getByRole("list", { name: "Setup progress" }),
    "Dependencies",
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page
    .getByLabel("Product definition and design system", { exact: true })
    .selectOption(repositories[0].id);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Save product project", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Inventory product", exact: true }),
  ).toBeVisible();
  expect(stored?.definition).toMatchObject({
    topology: "multi_repo",
    product_repository_id: repositories[0].id,
    components: [
      { id: "api", backend: "pyfly", frontend: "none" },
      { id: "component-2", backend: "none", frontend: "vue" },
    ],
  });
  await page
    .getByRole("button", { name: "Start component run", exact: true })
    .nth(1)
    .click();
  await page
    .getByLabel("What should this change accomplish?")
    .fill("Improve portal reservation validation");
  await expect(page.getByLabel("Source repository")).toBeDisabled();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByLabel("Backend", { exact: true })).toHaveValue("none");
  await expect(page.getByLabel("Frontend", { exact: true })).toHaveValue("vue");
  await expect(page.getByLabel("Backend", { exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("button", { name: "Review run", exact: true }).click();
  await page
    .getByRole("button", { name: "Start building", exact: true })
    .click();
  await expect.poll(() => run?.project_id).toBe("c".repeat(32));
  expect(run).toMatchObject({
    component_id: "component-2",
    repository_id: repositories[1].id,
    backend: "none",
    frontend: "vue",
  });
  expect(run).not.toHaveProperty("commands");
  expect(run).not.toHaveProperty("product_definition");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
  ).toBe(false);
});
test("AS-IS source evidence requires an explicit digest review and never claims runtime verification", async ({
  page,
}, testInfo) => {
  const project = {
    id: "c".repeat(32),
    definition: {
      name: "Inventory",
      description: "Product",
      owners: ["Team"],
      topology: "monorepo",
      components: [
        {
          id: "api",
          name: "Inventory API",
          repository_id: repositories[0].id,
          path: ".",
          kind: "api",
          backend: "pyfly",
          frontend: "none",
          verification: [],
          depends_on: [],
          responsibility: "Stock invariants",
        },
      ],
    },
    revision: 1,
    digest: "d".repeat(64),
    updated_at: "2026-10-12T00:00:00Z",
    created_at: "2026-10-12T00:00:00Z",
  };
  let report: Record<string, unknown> | null = null,
    review: unknown;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/repositories")) json = [repositories[0]];
    if (
      path.endsWith("/runs") ||
      path.endsWith("/providers") ||
      path.endsWith("/deployment-targets")
    )
      json = [];
    if (path.endsWith("/projects")) json = [project];
    if (path.endsWith("/projects/" + project.id))
      json = {
        ...project,
        components: [
          {
            ...project.definition.components[0],
            latest_status: "not_started",
            repository_run_count: 0,
            runs: [],
          },
        ],
        counts: { not_started: 1 },
      };
    if (path.endsWith("/analyses")) {
      if (req.method() === "POST") {
        report = {
          id: "analysis-1",
          project_id: project.id,
          status: "ready_for_review",
          reviewed: false,
          method: "source_inspection",
          digest: "e".repeat(64),
          created_at: project.created_at,
          components: [
            {
              ...project.definition.components[0],
              commit: "f".repeat(40),
              execution: "source_only",
              skipped_files: 2,
              facts: [
                {
                  kind: "http_route",
                  value: "GET /stock",
                  confidence: "observed",
                  source: {
                    path: "app/api.py",
                    line: 12,
                    excerpt: '@get_mapping("/stock")',
                    blob: "0".repeat(40),
                    commit: "f".repeat(40),
                  },
                },
              ],
            },
          ],
          documents: { "functional-as-is.md": "# Source-only declarations" },
          open_questions: ["Which customers reserve stock?"],
          functional_definition: null,
        };
        json = report;
      } else json = report ? [report] : [];
    }
    if (path.endsWith("/analyses/analysis-1")) json = report;
    if (path.endsWith("/review")) {
      review = req.postDataJSON();
      report = { ...report, status: "reviewed", reviewed: true };
      json = report;
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-operator-token-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product projects");
  await page
    .getByRole("button", { name: "Open source inspection", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Inspect configured source", exact: true })
    .click();
  await expect(
    page.getByText("1 inspected component · 1 observed declaration", {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByText("GET /stock", { exact: true })).toBeVisible();
  await expect(page.getByText("app/api.py:12", { exact: true })).toBeVisible();
  await expect(
    page.getByText("1 observed declaration · 2 skipped files · source only", {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Acknowledge this report digest",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "I reviewed the source evidence and open questions. This does not claim observed runtime behavior.",
    )
    .check();
  await page
    .getByRole("button", {
      name: "Acknowledge this report digest",
      exact: true,
    })
    .click();
  expect(review).toEqual({ digest: "e".repeat(64), reviewed: true });
  await expect(
    page.getByText("reviewed · source inspection", { exact: true }),
  ).toBeVisible();
  const fieldGeometry = [];
  for (const theme of ["light", "dark"]) {
    for (const [width, height] of [
      [1920, 1080],
      [1440, 768],
      [390, 844],
      [320, 844],
    ]) {
      await page.setViewportSize({ width, height });
      fieldGeometry.push(
        await page.locator("dialog[open] .wizard-field-scroll").evaluate(
          (element, context) => ({
            ...context,
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
          }),
          { width, theme },
        ),
      );
      const geometry = await page
        .locator("dialog[open] .wizard-actions")
        .evaluate((element) => {
          const actions = element.getBoundingClientRect();
          const dialog = element.closest("dialog")!.getBoundingClientRect();
          return {
            inside:
              actions.top >= dialog.top && actions.bottom <= dialog.bottom,
            buttons: [...element.querySelectorAll("button")].every((button) => {
              const box = button.getBoundingClientRect();
              return (
                box.top >= actions.top &&
                box.bottom <= actions.bottom &&
                box.left >= dialog.left &&
                box.right <= dialog.right
              );
            }),
          };
        });
      expect(geometry).toEqual({ inside: true, buttons: true });
      if (width <= 390)
        await page.screenshot({
          path: testInfo.outputPath(`source-review-${width}-${theme}.png`),
        });
      await page.getByRole("button", { name: "Done", exact: true }).click();
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page.locator(".workspace-main > main").evaluate((element) => {
        element.scrollTop = 0;
      });
      const left = await page.locator(".projects-page").evaluate((element) => {
        const main = element.closest("main")!;
        return Math.abs(
          element.getBoundingClientRect().left -
            main.getBoundingClientRect().left -
            parseFloat(getComputedStyle(main).paddingLeft),
        );
      });
      expect(left).toBeLessThanOrEqual(1);
      const open = page.getByRole("button", {
        name: "Open source inspection",
        exact: true,
      });
      await open.scrollIntoViewIfNeeded();
      const visible = await open.evaluate((element) => {
        const r = element.getBoundingClientRect(),
          owner = element.closest("main")!.getBoundingClientRect();
        return r.top >= owner.top && r.bottom <= owner.bottom;
      });
      expect(visible).toBe(true);
      await open.click();
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page.getByRole("button", { name: "Continue", exact: true }).click();
    }
  }
  expect(
    fieldGeometry.filter((field) => field.scrollWidth > field.clientWidth),
  ).toEqual([]);
});
