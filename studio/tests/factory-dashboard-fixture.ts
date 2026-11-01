/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type {
  DashboardProject,
  DashboardRun,
  DashboardSnapshot,
} from "../src/FactoryDashboard";
import type { Page, Route } from "@playwright/test";

export type FactoryFixtureRun = Partial<DashboardRun> & {
  id: string;
  repository_id: string;
  brief: string;
  status: string;
};
export type FactoryFixtureTask = { id: string; status: string };
export type FactoryFixture = {
  projects: DashboardProject[];
  runs: FactoryFixtureRun[];
  plans?: Record<string, { tasks: FactoryFixtureTask[] }>;
  administrator?: boolean;
};

export async function mockFactoryDashboard(
  route: Route,
  fixture: FactoryFixture = { projects: [], runs: [] },
): Promise<boolean> {
  const json = factoryDashboardFixture(new URL(route.request().url()), fixture);
  if (json === undefined) return false;
  await route.fulfill({ json });
  return true;
}

export async function openFactoryRun(
  page: Page,
  brief: string | RegExp,
): Promise<void> {
  await page
    .locator(".factory-run-record")
    .filter({ hasText: brief })
    .getByRole("button", { name: "Open run", exact: true })
    .click();
}

const buckets = {
  queued: ["pending", "queued"],
  active: [
    "running",
    "cloning",
    "preparing",
    "generating",
    "developing",
    "executing",
    "verifying",
    "validating",
    "publishing",
    "deploying",
  ],
  needs_action: [
    "paused",
    "interrupted",
    "needs_reconciliation",
    "awaiting_input",
  ],
  completed: ["succeeded", "completed"],
  failed: ["failed"],
  cancelled: ["cancelled"],
  expired: ["expired"],
  quarantined: ["quarantined"],
};

// Presentation fixtures, never a replacement for native ACL or SQL acceptance.
// A project link must be explicit; a shared repository does not establish it.
export function factoryDashboardFixture(url: URL, fixture: FactoryFixture) {
  const query = url.searchParams;
  const limit = Math.min(200, Math.max(1, Number(query.get("limit") || 40)));
  const offset = Number(query.get("cursor") || 0);
  if (url.pathname.endsWith("/factory-dashboard/projects")) {
    const search = (query.get("search") || "").toLocaleLowerCase();
    const choices = fixture.projects.filter((row) =>
      row.name.toLocaleLowerCase().includes(search),
    );
    return {
      version: 1,
      items: choices.slice(offset, offset + limit),
      next_cursor:
        offset + limit < choices.length ? String(offset + limit) : null,
    };
  }
  if (!url.pathname.endsWith("/factory-dashboard")) return undefined;
  const kind = (query.get("kind") ||
    "factory") as DashboardSnapshot["scope"]["kind"];
  const projectId = query.get("project_id");
  const runId = query.get("run_id");
  const project = fixture.projects.find((row) => row.id === projectId) || null;
  const runs = fixture.runs.filter((row) =>
    kind === "project"
      ? row.project_id === projectId
      : kind === "run"
        ? row.id === runId
        : kind === "standalone"
          ? !row.project_id
          : true,
  );
  const counts: DashboardSnapshot["counts"] = {
    total: runs.length,
    active: 0,
    queued: 0,
    needs_action: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    expired: 0,
    quarantined: 0,
    unknown: 0,
  };
  const category = (status: string) =>
    Object.entries(buckets).find(([, values]) =>
      values.includes(status),
    )?.[0] as Exclude<keyof typeof counts, "total"> | undefined;
  for (const row of runs) counts[category(row.status) || "unknown"]++;
  const stages: DashboardSnapshot["stages"] = [
    "prepare",
    "implement",
    "verify",
    "checkpoint",
    "publish",
    "deploy",
    "health",
  ].map((id) => ({
    id,
    active: 0,
    done: 0,
    waiting: 0,
    skipped: 0,
    failed: 0,
    needs_action: 0,
    expired: 0,
    quarantined: 0,
    unknown: 0,
    observed_until: null,
    recorded_running: 0,
  }));
  for (const row of runs)
    for (const task of fixture.plans?.[row.id]?.tasks || []) {
      const stage = stages.find(
        (item) =>
          item.id === (task.id.startsWith("publish") ? "publish" : task.id),
      );
      if (!stage) continue;
      if (task.status === "running") stage.recorded_running!++;
      if (["done", "succeeded"].includes(task.status)) stage.done++;
      else if (["queued", "pending", "waiting"].includes(task.status))
        stage.waiting++;
      else if (task.status === "skipped") stage.skipped++;
      else if (task.status === "failed") stage.failed++;
      else if (
        ["paused", "interrupted", "needs_reconciliation"].includes(task.status)
      )
        stage.needs_action++;
      else stage.unknown++;
    }
  const filtered = runs.filter(
    (row) =>
      !query.get("bucket") ||
      (category(row.status) || "unknown") === query.get("bucket"),
  );
  const items: DashboardRun[] = filtered
    .slice(offset, offset + limit)
    .map((row) => ({
      project_id: null,
      project_name: null,
      repository_name: null,
      component_id: null,
      component_name: null,
      association: row.project_id ? "project" : "standalone",
      brief_truncated: false,
      branch: null,
      pr_url: null,
      created_at: null,
      issue_context: null,
      ...row,
    }));
  const value: DashboardSnapshot = {
    version: 1,
    scope: { kind, project_id: projectId, run_id: runId, project },
    observed_at: "2026-10-30T12:00:00Z",
    counts,
    associations: {
      projects:
        kind === "project"
          ? Number(Boolean(project))
          : kind === "run"
            ? new Set(runs.map((row) => row.project_id).filter(Boolean)).size
            : fixture.projects.length,
      standalone_runs: runs.filter((row) => !row.project_id).length,
      repositories_with_runs: new Set(runs.map((row) => row.repository_id))
        .size,
    },
    stages,
    unmapped_active_tasks: 0,
    runs: {
      items,
      next_cursor:
        offset + limit < filtered.length ? String(offset + limit) : null,
    },
    standalone_available: fixture.administrator ?? true,
    unavailable_metrics: [
      "worker_liveness",
      "queue_wait_duration",
      "deployment_health",
      "runtime_metrics",
    ],
  };
  return value;
}
