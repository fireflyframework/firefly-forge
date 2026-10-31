/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { writeFile } from "node:fs/promises";

test("large product components remain searchable and editable without truncation or a quadratic dependency form", async ({
  page,
}, info) => {
  const repository = {
    id: "a".repeat(32),
    name: "Services",
    provider: "github",
    url: "https://github.com/example/services",
    default_branch: "main",
  };
  const definition = {
    name: "Large product",
    description: "A complete project graph",
    topology: "monorepo",
    owners: ["Product team"],
    product_repository_id: repository.id,
    components: Array.from({ length: 5000 }, (_, index) => ({
      id: `component-${index}`,
      name: `Service ${index}`,
      path: `services/${index}`,
      repository_id: repository.id,
      kind: "api",
      backend: "pyfly",
      frontend: "none",
      verification: [],
      depends_on: [] as string[],
      responsibility: "Own this service",
    })),
  };
  let project = {
    id: "c".repeat(32),
    revision: 1,
    digest: "d".repeat(64),
    definition,
    created_at: "2026-10-24T00:00:00Z",
    updated_at: "2026-10-24T00:00:00Z",
  };
  const small = {
    ...project,
    id: "e".repeat(32),
    definition: {
      ...definition,
      name: "Small product",
      components: definition.components.slice(0, 2),
    },
  };
  let saved: typeof definition | undefined;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/repositories")) json = [repository];
    if (
      path.endsWith("/runs") ||
      path.endsWith("/analyses") ||
      path.endsWith("/deployment-targets")
    )
      json = [];
    if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
    if (path.endsWith("/projects")) json = [project, small];
    if (path.endsWith(`/projects/${project.id}`)) {
      if (req.method() === "PUT") {
        saved = req.postDataJSON();
        project = { ...project, revision: 2, definition: saved! };
        json = project;
      } else
        json = {
          ...project,
          components: project.definition.components.map((item) => ({
            ...item,
            latest_status: "not_started",
            repository_run_count: 0,
            runs: [],
          })),
          counts: { not_started: 5000 },
        };
    }
    if (path.endsWith(`/projects/${small.id}`))
      json = {
        ...small,
        components: small.definition.components.map((item) => ({
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
  await expect(
    page.getByRole("heading", { name: "Large product", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".project-topology-map article")).toHaveCount(25);
  await expect(page.locator(".project-components .feature-record")).toHaveCount(
    25,
  );
  await page
    .getByLabel("Search source topology", { exact: true })
    .fill("component-4999");
  await expect(page.locator(".project-topology-map article")).toHaveCount(1);
  await expect(page.locator(".project-topology-map article")).toContainText(
    "Service 4999",
  );
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(small.id);
  await expect(
    page.getByRole("heading", { name: "Small product", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Search source topology", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(project.id);
  await expect(
    page.getByLabel("Search source topology", { exact: true }),
  ).toHaveValue("");
  await expect(page.locator(".project-topology-map article")).toHaveCount(25);
  await page
    .getByRole("button", { name: "Edit component contracts", exact: true })
    .click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Search configured components", { exact: true })
    .fill("component-4999");
  const selectedComponent = page
    .getByRole("navigation", { name: "Configured components", exact: true })
    .getByRole("button", { name: "5000. Service 4999", exact: true });
  await selectedComponent.scrollIntoViewIfNeeded();
  const frames = await selectedComponent.evaluate(async (node) => {
    const main = node.closest("main")!,
      samples = [];
    for (let i = 0; i < 60; i++) {
      await new Promise(requestAnimationFrame);
      samples.push({
        mainHeight: main.clientHeight,
        mainScroll: main.scrollTop,
        buttonY: node.getBoundingClientRect().y,
      });
    }
    return samples;
  });
  await writeFile(
    info.outputPath("component-5000-stability.json"),
    JSON.stringify(frames, null, 2),
  );
  for (const key of ["mainHeight", "mainScroll", "buttonY"] as const)
    expect(
      Math.max(...frames.map((frame) => frame[key])) -
        Math.min(...frames.map((frame) => frame[key])),
      key,
    ).toBeLessThanOrEqual(1);
  await selectedComponent.click();
  await expect(page.getByLabel("Component name", { exact: true })).toHaveValue(
    "Service 4999",
  );
  await page
    .getByLabel("Search configured components", { exact: true })
    .fill("component-4999");
  await page
    .getByLabel("Component name", { exact: true })
    .fill("Renamed service 4999");
  await expect(
    page.getByLabel("Search configured components", { exact: true }),
  ).toHaveValue("component-4999");
  await expect(page.getByLabel("Component name", { exact: true })).toHaveValue(
    "Renamed service 4999",
  );
  await page
    .getByRole("button", { name: "Add component", exact: true })
    .click();
  await page.getByLabel("Component name", { exact: true }).fill("Late service");
  await page
    .getByLabel("Component responsibility", { exact: true })
    .fill("Keep the complete product graph");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const candidates = page.getByRole("group", {
    name: "Late service depends on",
    exact: true,
  });
  await expect(candidates.getByRole("checkbox")).toHaveCount(25);
  await page
    .getByLabel("Search available dependencies", { exact: true })
    .fill("Renamed service 4999");
  await candidates
    .getByRole("checkbox", { name: "Renamed service 4999", exact: true })
    .check();
  await page
    .getByLabel("Search available dependencies", { exact: true })
    .fill("");
  await candidates
    .getByRole("checkbox", { name: "Service 0", exact: true })
    .check();
  await candidates
    .getByRole("button", { name: "Next available dependencies", exact: true })
    .click();
  await candidates
    .getByRole("button", {
      name: "Previous available dependencies",
      exact: true,
    })
    .click();
  await expect(
    candidates.getByRole("checkbox", { name: "Service 0", exact: true }),
  ).toBeChecked();
  await page
    .getByLabel("Search available dependencies", { exact: true })
    .fill("Renamed service 4999");
  await expect(
    candidates.getByRole("checkbox", {
      name: "Renamed service 4999",
      exact: true,
    }),
  ).toBeChecked();
  await page
    .getByLabel("Search dependency components", { exact: true })
    .fill("component-123");
  await page
    .getByRole("navigation", {
      name: "Dependency component selection",
      exact: true,
    })
    .getByRole("button", { name: "Service 123", exact: true })
    .click();
  await expect(
    page.getByLabel("Search available dependencies", { exact: true }),
  ).toHaveValue("");
  await page
    .getByLabel("Search dependency components", { exact: true })
    .fill("Late service");
  await page
    .getByRole("navigation", {
      name: "Dependency component selection",
      exact: true,
    })
    .getByRole("button", { name: "Late service", exact: true })
    .click();
  await page
    .getByLabel("Search available dependencies", { exact: true })
    .fill("Renamed service 4999");
  await expect(
    page.getByRole("checkbox", { name: "Renamed service 4999", exact: true }),
  ).toBeChecked();
  const evidence = process.env.FORGE_PROJECT_UI_EVIDENCE;
  await expect(
    page.getByText("Review source topology (5,001 components)", {
      exact: true,
    }),
  ).toHaveCount(0);
  await page.locator(".wizard-field-scroll").evaluate((element) => {
    element.scrollTop = 0;
  });
  const fields = await page.locator(".wizard-field-scroll").boundingBox(),
    actions = await page.locator(".wizard-actions").boundingBox();
  expect(fields!.y + fields!.height).toBeLessThanOrEqual(actions!.y + 1);
  if (evidence)
    await page.screenshot({ path: `${evidence}/large-project-desktop.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("checkbox", { name: "Renamed service 4999", exact: true })
    .scrollIntoViewIfNeeded();
  if (evidence)
    await page.screenshot({ path: `${evidence}/large-project-mobile.png` });
  await page
    .getByRole("combobox", { name: "Appearance", exact: true })
    .selectOption("dark");
  if (evidence)
    await page.screenshot({
      path: `${evidence}/large-project-mobile-dark.png`,
    });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator(".wizard-field-scroll").evaluate((element) => {
    element.scrollTop = 0;
  });
  if (evidence)
    await page.screenshot({
      path: `${evidence}/large-project-desktop-dark.png`,
    });
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Appearance", exact: true })
    .selectOption("light");
  const topologyReview = page.getByText(
    "Review source topology (5,001 components)",
    { exact: true },
  );
  await topologyReview.click();
  await page
    .getByLabel("Search source topology", { exact: true })
    .fill("component-5001");
  await expect(page.locator(".project-topology-map article")).toHaveCount(1);
  await expect(page.locator(".project-topology-map article")).toContainText(
    "Late service",
  );
  await page.locator(".project-topology-map article").scrollIntoViewIfNeeded();
  if (evidence)
    await page.screenshot({
      path: `${evidence}/large-project-expanded-desktop.png`,
    });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".project-topology-map article").scrollIntoViewIfNeeded();
  const finalCard = await page
      .locator(".project-topology-map article")
      .boundingBox(),
    footer = await page.locator(".workspace-main > footer").boundingBox();
  expect(finalCard!.y + finalCard!.height).toBeLessThanOrEqual(footer!.y + 1);
  if (evidence)
    await page.screenshot({
      path: `${evidence}/large-project-expanded-mobile.png`,
    });
  await topologyReview.click();
  await page
    .getByRole("button", { name: "Save product project", exact: true })
    .click();
  await expect.poll(() => saved?.components.length).toBe(5001);
  expect(saved?.components.at(-1)?.depends_on.sort()).toEqual([
    "component-0",
    "component-4999",
  ]);
  expect(saved?.components[4999].name).toBe("Renamed service 4999");
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(small.id);
  await expect(
    page.getByRole("heading", { name: "Small product", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(project.id);
  await page
    .getByLabel("Search component activity", { exact: true })
    .fill("component-5001");
  await expect(page.locator(".project-components .feature-record")).toHaveCount(
    1,
  );
  await expect(
    page.locator(".project-components .feature-record"),
  ).toContainText("Renamed service 4999");
  await expect(
    page.locator(".project-components .feature-record"),
  ).toContainText("Service 0");
});
