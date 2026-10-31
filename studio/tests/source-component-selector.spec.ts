/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import {
  setupSourceSelector,
  selectorComponents,
  selectorRepositories,
  otherSelectorProject,
  selectorProject,
} from "./source-component-selector-fixture";

test("filtered bulk scope spans pages, retains outside selections and sends only exact selected IDs", async ({
  page,
}) => {
  const { view, inspections } = await setupSourceSelector(page, "light");
  const rows = view.getByRole("list", { name: "Component results" });
  await expect(rows.getByRole("checkbox")).toHaveCount(25);
  await view.locator(".source-component-filters > summary").click();
  await view.getByLabel("Component type", { exact: true }).selectOption("api");
  await view
    .getByLabel("Repository", { exact: true })
    .selectOption(selectorRepositories[0].id);
  await view
    .getByRole("button", { name: "Clear matching", exact: true })
    .click();
  await expect(
    view.getByRole("status", { name: "Component selection" }),
  ).toHaveText("30 of 60 selected · 30 results");
  await view
    .getByRole("button", { name: "Select matching", exact: true })
    .click();
  await expect(
    view.getByRole("status", { name: "Component selection" }),
  ).toHaveText("60 of 60 selected · 30 results");
  await view
    .getByRole("button", { name: "Next component results", exact: true })
    .click();
  await expect(rows.getByRole("checkbox")).toHaveCount(5);
  await expect(
    rows.getByRole("checkbox", { name: /Inventory API 30/ }),
  ).toBeChecked();
  await view
    .getByRole("button", { name: "Clear matching", exact: true })
    .click();
  expect(inspections).toHaveLength(0);
  await view
    .getByRole("button", { name: /^Inspect \d+ components?$/, exact: true })
    .click();
  await expect.poll(() => inspections.length).toBe(1);
  expect(inspections[0].body).toEqual({
    component_ids: selectorComponents.slice(30).map((item) => item.id),
    expected_project_revision: 1,
    expected_project_digest: "d".repeat(64),
  });
  expect(inspections[0].body.component_ids).toEqual(
    selectorComponents.slice(30).map((item) => item.id),
  );
  await view.getByRole("button", { name: "Back", exact: true }).click();
  await view.locator(".source-component-filters > summary").click();
  await expect(view.getByLabel("Component type", { exact: true })).toHaveValue(
    "api",
  );
  await expect(view.getByLabel("Repository", { exact: true })).toHaveValue(
    selectorRepositories[0].id,
  );
  await expect(view.getByText("Page 2 of 2", { exact: true })).toBeVisible();
  await expect(rows.getByRole("checkbox").first()).not.toBeChecked();
  await expect(view).not.toContainText("private-token");
  expect(inspections).toHaveLength(1);
});

test("selected-only empty results retain keyboard recovery without sending an inspection", async ({
  page,
}) => {
  const { view, inspections } = await setupSourceSelector(page, "light");
  await view
    .getByLabel("Search components", { exact: true })
    .fill("component-60");
  await view.getByLabel("Selected only", { exact: true }).check();
  const choice = view.getByRole("checkbox", { name: /Inventory worker 60/ });
  await choice.focus();
  await page.keyboard.press("Space");
  await expect(view.getByLabel("Selected only", { exact: true })).toBeFocused();
  await expect(
    view.getByText("No components match these filters.", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(
    view.getByRole("button", { name: "Reset filters", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    view.getByRole("status", { name: "Component selection" }),
  ).toHaveText("59 of 60 selected · 60 results");
  expect(inspections).toHaveLength(0);
});

test("new revisions prune removed IDs and obsolete filters while rebinding selected contracts requires review", async ({
  page,
}) => {
  const { view, state, inspections } = await setupSourceSelector(page, "light");
  await view
    .getByLabel("Search components", { exact: true })
    .fill("Inventory worker");
  await view.locator(".source-component-filters > summary").click();
  await view.getByLabel("Component type", { exact: true }).selectOption("api");
  await view
    .getByLabel("Repository", { exact: true })
    .selectOption(selectorRepositories[0].id);
  const workers = structuredClone(selectorComponents.slice(30));
  state.projects[0] = {
    ...selectorProject,
    revision: 2,
    digest: "8".repeat(64),
    definition: {
      ...selectorProject.definition,
      components: [
        ...workers,
        { ...workers[0], id: "added-worker", name: "Added worker" },
      ],
    },
  };
  await view
    .getByRole("button", { name: "Refresh project scope", exact: true })
    .click();
  await expect(view.getByLabel("Component type", { exact: true })).toHaveValue(
    "",
  );
  await expect(view.getByLabel("Repository", { exact: true })).toHaveValue("");
  await expect(
    view.getByLabel("Search components", { exact: true }),
  ).toHaveValue("Inventory worker");
  await expect(
    view.getByRole("status", { name: "Component selection" }),
  ).toHaveText("30 of 31 selected · 30 results");
  state.projects[0] = {
    ...state.projects[0],
    revision: 3,
    digest: "7".repeat(64),
    definition: {
      ...state.projects[0].definition,
      components: state.projects[0].definition.components.map((item, index) =>
        index === 0
          ? {
              ...item,
              path: "workers/rebound",
              backend: "larafly",
              verification: [{ argv: ["php", "artisan", "test"], timeout: 30 }],
              depends_on: [workers[1].id],
            }
          : item,
      ),
    },
  };
  await view
    .getByRole("button", { name: "Refresh project scope", exact: true })
    .click();
  await expect(
    view.getByRole("heading", {
      name: "Review current source scope",
      exact: true,
    }),
  ).toBeVisible();
  const inspect = view.getByRole("button", {
    name: "Inspect 30 components",
    exact: true,
  });
  await expect(inspect).toBeDisabled();
  await expect(
    view.getByText("Source contract changed", { exact: true }),
  ).toBeVisible();
  expect(inspections).toHaveLength(0);
  await view
    .getByRole("button", { name: "Confirm current source scope", exact: true })
    .click();
  expect(inspections).toHaveLength(0);
  await inspect.click();
  await expect.poll(() => inspections.length).toBe(1);
  expect(inspections[0].body).toEqual({
    component_ids: workers.map((item) => item.id),
    expected_project_revision: 3,
    expected_project_digest: "7".repeat(64),
  });
});

test("a server snapshot race holds the draft until a fresh snapshot and explicit scope confirmation", async ({
  page,
}) => {
  const { view, state, reports, inspections } = await setupSourceSelector(
    page,
    "dark",
  );
  state.beforeInspection = () => {
    state.projects[0] = {
      ...state.projects[0],
      revision: 2,
      digest: "8".repeat(64),
    };
  };
  await view
    .getByRole("button", { name: "Inspect 60 components", exact: true })
    .click();
  await expect(
    view.getByText("Project changed; review component scope again", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    view.getByRole("button", {
      name: "Confirm current source scope",
      exact: true,
    }),
  ).toBeDisabled();
  expect(reports.size).toBe(0);
  await view
    .getByRole("button", { name: "Refresh project scope", exact: true })
    .click();
  await expect(
    view.getByRole("button", {
      name: "Confirm current source scope",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    view.getByText("Project changed; review component scope again", {
      exact: true,
    }),
  ).toBeVisible();
  await view
    .getByRole("button", { name: "Confirm current source scope", exact: true })
    .click();
  expect(inspections).toHaveLength(1);
  await view
    .getByRole("button", { name: "Inspect 60 components", exact: true })
    .click();
  await expect.poll(() => inspections.length).toBe(2);
  expect(inspections[1].body.expected_project_revision).toBe(2);
  expect(inspections[1].body.expected_project_digest).toBe("8".repeat(64));
});

test("failed inspection details survive successful report reads and never cause an automatic POST", async ({
  page,
}) => {
  const { view, state, inspections } = await setupSourceSelector(page, "light");
  state.failNext = { status: 502, detail: "Inspection fixture unavailable" };
  await view
    .getByRole("button", { name: "Inspect 60 components", exact: true })
    .click();
  await expect(
    view.getByText("Inspection fixture unavailable", { exact: true }),
  ).toBeVisible();
  await view
    .getByRole("button", { name: "Read recorded reports", exact: true })
    .click();
  const reads = state.reads.length;
  await view
    .getByRole("button", { name: "Refresh reports", exact: true })
    .click();
  await expect.poll(() => state.reads.length).toBeGreaterThan(reads);
  await view.getByRole("button", { name: "Back", exact: true }).click();
  await expect(
    view.getByText("Inspection fixture unavailable", { exact: true }),
  ).toBeVisible();
  await expect(
    view.getByRole("button", { name: "Inspect 60 components", exact: true }),
  ).toBeDisabled();
  expect(inspections).toHaveLength(1);
});

test("empty scope cannot POST and a different project starts with its own selection and filters", async ({
  page,
}) => {
  const { view, inspections } = await setupSourceSelector(page, "dark");
  await view
    .getByRole("button", { name: "Clear matching", exact: true })
    .click();
  await expect(
    view.getByRole("button", {
      name: /^Inspect \d+ components?$/,
      exact: true,
    }),
  ).toBeDisabled();
  await view.getByLabel("Search components", { exact: true }).press("Enter");
  await view
    .locator("form")
    .evaluate((form) => (form as HTMLFormElement).requestSubmit());
  await view.getByLabel("Selected only", { exact: true }).check();
  await expect(
    view.getByText("No components match these filters.", { exact: true }),
  ).toBeVisible();
  await view
    .getByRole("button", { name: "Reset filters", exact: true })
    .click();
  await expect(
    view.getByRole("list", { name: "Component results" }).getByRole("checkbox"),
  ).toHaveCount(25);
  await view
    .getByLabel("Search components", { exact: true })
    .fill("component-60");
  expect(inspections).toHaveLength(0);
  await view
    .getByRole("button", { name: "Back to project", exact: true })
    .first()
    .click();
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(otherSelectorProject.id);
  await page
    .getByRole("button", { name: "Open source inspection", exact: true })
    .click();
  await expect(
    view.getByLabel("Search components", { exact: true }),
  ).toHaveValue("");
  await expect(
    view.getByRole("status", { name: "Component selection" }),
  ).toHaveText("2 of 2 selected · 2 results");
  await view
    .getByRole("button", { name: /^Inspect \d+ components?$/, exact: true })
    .click();
  await expect.poll(() => inspections.length).toBe(1);
  expect(inspections[0].project).toBe(otherSelectorProject.id);
  expect(inspections[0].body.component_ids).toEqual(
    otherSelectorProject.definition.components.map((item) => item.id),
  );
});

for (const snapshot of ["missing", "earlier"] as const) {
  test(`reviewed report with ${snapshot} project snapshot cannot continue to definition`, async ({
    page,
  }) => {
    const { view, reports, inspections } = await setupSourceSelector(
      page,
      "light",
    );
    await view
      .getByRole("button", { name: "Inspect 60 components", exact: true })
      .click();
    await expect.poll(() => reports.size).toBe(1);
    const report = reports.get(selectorProject.id)!;
    reports.set(selectorProject.id, {
      ...report,
      status: "reviewed",
      reviewed: true,
      project_snapshot:
        snapshot === "missing"
          ? undefined
          : { ...selectorProject, digest: "8".repeat(64) },
    });
    await view.getByRole("button", { name: "Back", exact: true }).click();
    await view.locator(".source-inspection-back").click();
    await expect(
      page.getByText("1 recorded inspection · reviewed", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Open source inspection", exact: true })
      .click();
    await view.getByRole("button", { name: "Continue", exact: true }).click();
    await view.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(
      view.getByText(
        "This reviewed report belongs to an earlier or unavailable project snapshot. Inspect and review the current project scope before continuing to product definition.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      view.getByRole("button", {
        name: "Continue to product definition",
        exact: true,
      }),
    ).toBeDisabled();
    expect(inspections).toHaveLength(1);
  });
}
