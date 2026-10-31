/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { factoryDashboardFixture } from "./factory-dashboard-fixture";

async function openReviewedSource(
  page: Page,
  outcome: "confirmed" | "unknown" | "foreign",
  theme: "light" | "dark" = "light",
) {
  const repository = "a".repeat(32),
    projectId = "b".repeat(32),
    reportId = "c".repeat(32);
  const component = {
    id: "stock-api",
    name: "Stock API",
    repository_id: repository,
    path: "services/stock",
    kind: "api",
    backend: "pyfly",
    frontend: "none",
    verification: [],
    depends_on: [],
    responsibility: "Reserve stock",
  };
  const project = {
    id: projectId,
    revision: 3,
    digest: "d".repeat(64),
    created_at: "2026-10-31T10:00:00Z",
    updated_at: "2026-10-31T10:00:00Z",
    definition: {
      name: "Inventory",
      description: "Reserve available stock",
      topology: "monorepo",
      owners: ["Inventory team"],
      product_repository_id: repository,
      components: [component],
    },
  };
  const facts = [
    {
      kind: "http_route",
      value: "/stock/reservations",
      confidence: "observed",
      source: {
        path: "services/stock/api.py",
        line: 8,
        excerpt: "@post_mapping('/stock/reservations')",
        commit: "f".repeat(40),
        blob: "7".repeat(40),
      },
    },
    {
      kind: "declared_test",
      value: "test_insufficient_stock",
      confidence: "observed",
      source: {
        path: "services/stock/test_api.py",
        line: 12,
        excerpt: "def test_insufficient_stock():",
        commit: "f".repeat(40),
        blob: "8".repeat(40),
      },
    },
  ];
  const report = {
    id: reportId,
    project_id: projectId,
    project_snapshot: project,
    status: "reviewed",
    reviewed: true,
    reviewed_at: "2026-10-31T10:30:00Z",
    method: "source_inspection",
    digest: "e".repeat(64),
    created_at: "2026-10-31T10:00:00Z",
    documents: {},
    open_questions: [],
    functional_definition: null,
    components: [
      {
        ...component,
        commit: "f".repeat(40),
        facts,
        skipped_files: 0,
        execution: "source_only",
      },
    ],
  };
  const context = {
    project_id: projectId,
    analysis_id: reportId,
    analysis_digest: report.digest,
    analysis_component_ids: [component.id],
  };
  const sourceSnapshot = {
    project_id: projectId,
    analysis_id: reportId,
    analysis_digest: report.digest,
    context_digest: "1".repeat(64),
    source_reviewed: true,
    business_journeys_confirmed: false,
    live_behavior_verified: false,
    components: [
      {
        ...report.components[0],
        facts: [{ ...facts[0], citation_id: "as-is-" + "9".repeat(32) }],
      },
    ],
    included_facts: 1,
    omitted_facts: 1,
    truncated: true,
    citations: [
      {
        id: "as-is-" + "9".repeat(32),
        path: "services/stock/api.py",
        line: 8,
        commit: "f".repeat(40),
        blob: "7".repeat(40),
        excerpt: facts[0].source.excerpt,
        component_id: component.id,
        repository_id: repository,
      },
    ],
  };
  const older = {
    id: "2".repeat(32),
    repository_id: repository,
    project_id: projectId,
    agent: "bridge",
    task: "product",
    created_at: "2026-10-30T10:00:00Z",
    turns: [],
    analysis_snapshot: { ...sourceSnapshot, analysis_id: "3".repeat(32) },
  };
  let created: Record<string, unknown> | null = null;
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname;
    let json: unknown = [];
    if (request.method() !== "GET")
      writes.push({ path, body: request.postDataJSON() });
    const dashboard = factoryDashboardFixture(url, { projects: [], runs: [] });
    if (dashboard) json = dashboard;
    else if (path.endsWith("/repositories"))
      json = [
        {
          id: repository,
          name: "Inventory",
          provider: "git",
          url: "/fixture/inventory",
          default_branch: "main",
        },
      ];
    else if (path.endsWith("/projects")) json = [project];
    else if (path.endsWith(`/projects/${projectId}`))
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
    else if (path.endsWith("/analyses")) json = [report];
    else if (path.endsWith(`/analyses/${reportId}`)) json = report;
    else if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    else if (path.endsWith("/assistant/conversations")) {
      if (request.method() === "POST") {
        const body = request.postDataJSON();
        expect(body).toEqual(
          body.project_id
            ? { task: "product", ...context }
            : { task: "product" },
        );
        created = {
          ...older,
          id: (body.project_id ? "4" : "6").repeat(32),
          project_id: body.project_id ?? null,
          analysis_snapshot: !body.project_id
            ? null
            : outcome === "foreign"
              ? { ...sourceSnapshot, analysis_digest: "5".repeat(64) }
              : sourceSnapshot,
        };
        if (outcome === "unknown") return route.abort("failed");
        json = created;
      } else json = created ? [created, older] : [older];
    } else if (path.endsWith(`/assistant/conversations/${older.id}`))
      json = older;
    else if (created && path.endsWith(`/assistant/conversations/${created.id}`))
      json = created;
    else if (path.endsWith("/capabilities"))
      json = { image_input: { status: "unverified" } };
    else if (path.endsWith("/health"))
      json = { status: "ok", execution_ready: true };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await page
    .getByRole("combobox", { name: "Appearance", exact: true })
    .selectOption(theme);
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fixture-source-handoff-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product projects");
  await expect(
    page.getByText("1 recorded inspection · reviewed", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Open source inspection", exact: true })
    .click();
  const inspection = page.getByRole("region", {
    name: "Source inspection",
    exact: true,
  });
  await inspection
    .getByRole("button", { name: "Continue", exact: true })
    .click();
  await inspection
    .getByRole("button", { name: "Continue", exact: true })
    .click();
  await inspection
    .getByRole("button", {
      name: "Continue to product definition",
      exact: true,
    })
    .click();
  const assistant = page.locator(".product-assistant");
  await expect(
    assistant.getByRole("button", {
      name: "Create conversation with reviewed source",
      exact: true,
    }),
  ).toBeEnabled();
  expect(writes).toEqual([]);
  return { assistant, writes, context, older };
}

for (const [width, theme] of [
  [1440, "light"],
  [390, "dark"],
] as const) {
  test(`reviewed source visual at ${width}px ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    const { assistant, writes } = await openReviewedSource(
      page,
      "confirmed",
      theme,
    );
    await page.evaluate(() => document.fonts.ready);
    const create = assistant.getByRole("button", {
      name: "Create conversation with reviewed source",
      exact: true,
    });
    const composer = assistant.getByLabel("Product design message", {
      exact: true,
    });
    await create.focus();
    await expect(create).toBeFocused();
    await expect(composer).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const pending = process.env.FORGE_HANDOFF_ATTACHED_ONLY
      ? null
      : await page.screenshot();
    await create.click();
    const record = assistant
      .locator("details")
      .filter({ hasText: "Recorded source report cccccccc" });
    await record.locator("summary").click();
    await expect(record).toContainText("1 included · 1 omitted");
    await expect(record).toContainText("services/stock/api.py:8");
    await composer.focus();
    await expect(composer).toBeFocused();
    await expect(composer).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const attached = await page.screenshot();
    const { writeFile } = await import("node:fs/promises");
    if (pending)
      await writeFile(
        info.outputPath(`source-pending-${width}-${theme}.png`),
        pending,
      );
    await writeFile(
      info.outputPath(`source-attached-${width}-${theme}.png`),
      attached,
    );
    expect(writes).toHaveLength(1);
  });
}

test("reviewed source opens a new draft and attaches once without automatically asking AI", async ({
  page,
}) => {
  const { assistant, writes, older } = await openReviewedSource(
    page,
    "confirmed",
  );
  await assistant
    .getByRole("button", {
      name: "Create conversation with reviewed source",
      exact: true,
    })
    .click();
  const record = assistant
    .locator("details")
    .filter({ hasText: "Recorded source report cccccccc" });
  await record.locator("summary").click();
  await expect(record).toContainText("1 included · 1 omitted");
  await expect(record).toContainText("services/stock/api.py:8");
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toMatch(/\/assistant\/conversations$/);
  await assistant
    .getByRole("button", { name: "Conversation history", exact: true })
    .click();
  await assistant
    .getByLabel("Recorded conversation", { exact: true })
    .selectOption(older.id);
  await expect(
    assistant
      .locator("summary")
      .filter({ hasText: "Recorded source report 33333333" }),
  ).toBeVisible();
  await expect(
    assistant.getByRole("region", {
      name: "Pending reviewed source",
      exact: true,
    }),
  ).toContainText("cccccccc");
  expect(writes).toHaveLength(1);
});

test("Factory navigation keeps source context while explicit New product starts without it", async ({
  page,
}) => {
  const { assistant, writes } = await openReviewedSource(page, "confirmed");
  await navigate(page, "Factory");
  await navigate(page, "Product definition");
  await expect(
    assistant.getByRole("button", {
      name: "Create conversation with reviewed source",
      exact: true,
    }),
  ).toBeEnabled();
  expect(writes).toEqual([]);
  await navigate(page, "Factory");
  await page.getByRole("button", { name: "New product", exact: true }).click();
  await expect(
    assistant.getByRole("region", {
      name: "Pending reviewed source",
      exact: true,
    }),
  ).toHaveCount(0);
  await assistant.getByRole("button", { name: "Files", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "Conversation files", exact: true }),
  ).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toEqual({ task: "product" });
});

for (const outcome of ["unknown", "foreign"] as const) {
  test(`${outcome} source conversation creation stays held across navigation with no second POST`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 320, height: 568 });
    const { assistant, writes } = await openReviewedSource(page, outcome);
    await assistant
      .getByRole("button", {
        name: "Create conversation with reviewed source",
        exact: true,
      })
      .click();
    await expect(assistant).toContainText(
      "new conversation could not be confirmed",
    );
    await expect(
      assistant.getByLabel("Product design message", { exact: true }),
    ).toBeDisabled();
    await assistant
      .getByRole("button", { name: "Close design assistant", exact: true })
      .click();
    await navigate(page, "Factory");
    await navigate(page, "Product definition");
    await expect(assistant).toContainText(
      "new conversation could not be confirmed",
    );
    await expect(
      assistant.getByLabel("Product design message", { exact: true }),
    ).toBeDisabled();
    await assistant
      .locator("form")
      .evaluate((form: HTMLFormElement) => form.requestSubmit());
    expect(writes).toHaveLength(1);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}
