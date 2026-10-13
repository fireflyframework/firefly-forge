/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
test("API-only process references expose missing functional fields and cannot be acknowledged", async ({
  page,
}) => {
  const definition = {
    customer: "Operations",
    problem: "Reserve inventory",
    journey: ["Reserve stock"],
    user_stories: ["Reserve available stock"],
    acceptance: ["Reject excess quantity"],
    owners: ["Inventory team"],
    nonfunctional_requirements: [],
    security_requirements: [],
    architecture: {
      backend: "pyfly",
      frontend: "none",
      components: [],
      decisions: [],
      nonfunctional_requirements: [],
      reviewed: true,
    },
    processes: [
      {
        id: "reserve",
        name: "Reserve stock",
        actor: "Operator",
        trigger: "API request",
        journey_refs: ["Reserve stock"],
        acceptance_refs: ["acceptance-1"],
        steps: [
          {
            id: "check",
            action: "Validate quantity",
            owner: "Inventory service",
            inputs: [],
            outputs: [],
            failures: [],
          },
        ],
      },
    ],
  };
  const grid = {
    renderer: "fixed-reference-template-v1",
    geometry_configurable: false,
    baseline: 4,
    desktop: { viewport_width: 1440, columns: 12, gutter: 24, margin: 32 },
    mobile: { viewport_width: 390, columns: 4, gutter: 16, margin: 20 },
    shell: {
      header_height: 76,
      sidebar_width: 248,
      footer_height: 64,
      max_content_width: 1440,
    },
    scope: "diagram_document",
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/repositories"))
      json = [
        {
          id: "repo-1",
          name: "Inventory",
          provider: "git",
          url: "file:///inventory",
          default_branch: "main",
        },
      ];
    if (
      path.endsWith("/runs") ||
      path.endsWith("/providers") ||
      path.endsWith("/assistant/conversations") ||
      path.endsWith("/deployment-targets")
    )
      json = [];
    if (path.endsWith("/product"))
      json = {
        repository_id: "repo-1",
        revision: 1,
        definition,
        digest: "b".repeat(64),
        created_at: "2026-10-12T00:00:00Z",
      };
    if (path.endsWith("/brand")) json = null;
    if (path.endsWith("/design/preview")) {
      const svg =
        '<svg xmlns="http://www.w3.org/2000/svg" width="390" height="844"><title>Process reference</title><text x="20" y="40">Reserve stock</text></svg>';
      json = {
        digest: "a".repeat(64),
        definition_digest: "b".repeat(64),
        brand_digest: null,
        method: "process_map",
        font_family: "Manrope",
        functional_definition_status: "pending",
        functional_contract: {
          pending_fields: [
            { process_id: "reserve", step_id: "check", field: "inputs" },
          ],
        },
        requirements: { grid },
        coverage: { journeys: 1, screen_count: 1 },
        screens: [
          {
            id: "journey-01",
            title: "Reserve stock",
            desktop_svg: svg,
            mobile_svg: svg,
            desktop_dark_svg: svg,
            mobile_dark_svg: svg,
            interaction_contract: {
              journey: "Reserve stock",
              acceptance_ids: ["acceptance-1"],
              primary_action: "API request",
              states: ["default", "error"],
            },
          },
        ],
      };
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
  await navigate(page, "Product definition");
  for (let index = 0; index < 4; index++)
    await page
      .getByRole("button", { name: /^(Continue|Review definition)$/ })
      .click();
  await page
    .getByRole("button", { name: "Review reference views", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Generate process references", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "reserve · check: inputs",
  );
  await page.locator(".design-layout > summary").click();
  await expect(
    page.getByText(
      "Diagram document layout; no application frontend is selected.",
      { exact: false },
    ),
  ).toBeVisible();
  for (const viewport of ["Desktop", "Mobile"]) {
    await page
      .getByRole("group", { name: "Preview viewport" })
      .getByRole("button", { name: viewport, exact: true })
      .click();
    for (const tone of ["Light", "Dark"]) {
      await page
        .getByRole("group", { name: "Preview theme" })
        .getByRole("button", { name: tone, exact: true })
        .click();
      await expect(
        page.getByRole("img", {
          name: new RegExp(
            `${viewport.toLowerCase()} ${tone.toLowerCase()} process reference`,
          ),
        }),
      ).toBeVisible();
    }
  }
  await expect(
    page.getByLabel(
      "I reviewed this journey's desktop and mobile references in light and dark themes.",
      { exact: true },
    ),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Done reviewing journeys", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Save draft definition", exact: true })
    .click();
  await expect(page.getByRole("alert").last()).toContainText(
    "Review every screen",
  );
});
