/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import type { ProjectComponent, ProjectSnapshot, Repository } from "../src/api";
export const selectorRepositories: Repository[] = [
  {
    id: "a".repeat(32),
    name: "Inventory",
    provider: "git",
    url: "https://fixture:private-token@example.test/team/api?token=private-token",
    default_branch: "main",
  },
  {
    id: "b".repeat(32),
    name: "Inventory",
    provider: "git",
    url: "https://example.test/team/workers",
    default_branch: "main",
  },
];
export const selectorComponents: ProjectComponent[] = Array.from(
  { length: 60 },
  (_, index) => ({
    id: `component-${index + 1}`,
    name:
      index === 59
        ? "Inventory worker 60 — warehouse adjustment event reconciliation"
        : `Inventory ${index < 30 ? "API" : "worker"} ${String(index + 1).padStart(2, "0")}`,
    repository_id: selectorRepositories[index < 30 ? 0 : 1].id,
    path:
      index === 59
        ? "workers/warehouse/adjustments/events/reconciliation/component-60"
        : `${index < 30 ? "apps/api" : "workers"}/component-${index + 1}`,
    kind: index < 30 ? "api" : "worker",
    backend: "pyfly",
    frontend: "none",
    verification: [],
    depends_on: [],
    responsibility: "Controlled source inspection choice",
  }),
);
export const selectorProject: ProjectSnapshot = {
  id: "c".repeat(32),
  definition: {
    name: "Inventory source fixture",
    description: "Synthetic component scope",
    owners: ["Team"],
    topology: "multi_repo",
    components: selectorComponents,
  },
  revision: 1,
  digest: "d".repeat(64),
  updated_at: "2026-10-31T00:00:00Z",
  created_at: "2026-10-31T00:00:00Z",
};
export const otherSelectorProject: ProjectSnapshot = {
  ...selectorProject,
  id: "e".repeat(32),
  definition: {
    ...selectorProject.definition,
    name: "Separate source fixture",
    components: selectorComponents
      .slice(0, 2)
      .map((item) => ({ ...item, id: `other-${item.id}` })),
  },
};
export async function setupSourceSelector(page: Page, theme: "light" | "dark") {
  const inspections: {
    project: string;
    body: {
      component_ids: string[];
      expected_project_revision: number;
      expected_project_digest: string;
    };
  }[] = [];
  const reports = new Map<string, Record<string, unknown>>();
  const state = {
    projects: structuredClone([selectorProject, otherSelectorProject]),
    reads: [] as string[],
    failNext: null as { status: number; detail: string } | null,
    beforeInspection: null as (() => void) | null,
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const req = route.request(),
      path = new URL(req.url()).pathname;
    let json: unknown = [];
    if (req.method() === "GET") state.reads.push(path);
    if (
      req.method() === "POST" &&
      path.endsWith("/analyses") &&
      state.beforeInspection
    ) {
      const change = state.beforeInspection;
      state.beforeInspection = null;
      change();
    }
    const project = state.projects.find((item) =>
      path.startsWith(`/api/v1/projects/${item.id}`),
    );
    if (path.endsWith("/repositories")) json = selectorRepositories;
    else if (path.endsWith("/projects")) json = state.projects;
    else if (path.endsWith("/health"))
      json = { status: "ok", execution_ready: true };
    else if (project && path.endsWith("/analyses") && req.method() === "POST") {
      const body = req.postDataJSON();
      inspections.push({ project: project.id, body });
      if (state.failNext) {
        const failure = state.failNext;
        state.failNext = null;
        return route.fulfill({
          status: failure.status,
          json: { detail: failure.detail },
        });
      }
      if (
        body.expected_project_revision !== project.revision ||
        body.expected_project_digest !== project.digest
      )
        return route.fulfill({
          status: 409,
          json: { detail: "Project changed; review component scope again" },
        });
      json = {
        id: project.id[0] + "f".repeat(31),
        project_id: project.id,
        status: "ready_for_review",
        reviewed: false,
        method: "source_inspection",
        digest: "9".repeat(64),
        components: project.definition.components
          .filter((item) => body.component_ids.includes(item.id))
          .map((item) => ({
            id: item.id,
            name: item.name,
            repository_id: item.repository_id,
            path: item.path,
            commit: "1".repeat(40),
            facts: [],
            skipped_files: 0,
            execution: "source_only",
          })),
        documents: {},
        open_questions: [],
        created_at: "2026-10-31T00:00:00Z",
        functional_definition: null,
        project_snapshot: structuredClone(project),
      };
      reports.set(project.id, json as Record<string, unknown>);
    } else if (project && path.endsWith("/analyses"))
      json = reports.has(project.id) ? [reports.get(project.id)] : [];
    else if (project && path.includes("/analyses/"))
      json = reports.get(project.id);
    else if (project && path.endsWith(project.id))
      json = {
        ...project,
        components: project.definition.components.map((item) => ({
          ...item,
          latest_status: "not_started",
          repository_run_count: 0,
          runs: [],
        })),
        counts: { not_started: project.definition.components.length },
      };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fixture-selector-bootstrap-at-least-24-chars");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expectAuthenticated(page);
  await navigate(page, "Product projects");
  await page
    .getByLabel("Product project", { exact: true })
    .selectOption(selectorProject.id);
  await page
    .getByRole("button", { name: "Open source inspection", exact: true })
    .click();
  return {
    inspections,
    state,
    reports,
    view: page.getByRole("region", {
      name: "Source inspection",
      exact: true,
    }),
  };
}
