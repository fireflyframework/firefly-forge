/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { FactoryStation } from "./FactoryScene";
import { manufacturingStations, type DevelopmentPlan } from "./factory";

export type FactorySelection = {
  kind: "factory" | "project" | "standalone" | "run";
  project: string;
  run: string;
  view: "floor" | "board";
  parent?: "factory" | "project" | "standalone";
};
export type DashboardStage = {
  id: string;
  active: number;
  done: number;
  waiting: number;
  skipped: number;
  failed: number;
  needs_action: number;
  unknown: number;
  expired?: number;
  quarantined?: number;
  observed_until?: string | null;
  recorded_running?: number;
};
export type DashboardRun = {
  id: string;
  repository_id: string;
  project_id: string | null;
  project_name: string | null;
  repository_name: string | null;
  component_id: string | null;
  component_name: string | null;
  association: "project" | "standalone" | "project_unavailable";
  brief: string;
  brief_truncated: boolean;
  status: string;
  active_task?: string | null;
  branch: string | null;
  pr_url: string | null;
  created_at: string | null;
  issue_context: {
    url: string | null;
    title: string | null;
    number: number | null;
  } | null;
};
export type DashboardProject = {
  id: string;
  name: string;
  component_count: number;
  repository_count: number;
};
export type DashboardSnapshot = {
  version: 1;
  scope: {
    kind: FactorySelection["kind"];
    project_id: string | null;
    run_id: string | null;
    project: DashboardProject | null;
  };
  observed_at: string;
  counts: Record<
    | "total"
    | "active"
    | "queued"
    | "needs_action"
    | "completed"
    | "failed"
    | "cancelled"
    | "expired"
    | "quarantined"
    | "unknown",
    number
  >;
  associations: {
    projects: number;
    standalone_runs: number;
    repositories_with_runs: number;
  };
  stages: DashboardStage[];
  unmapped_active_tasks: number;
  runs: { items: DashboardRun[]; next_cursor: string | null };
  standalone_available: boolean;
  unavailable_metrics: string[];
};
export type DashboardBucket = keyof Omit<DashboardSnapshot["counts"], "total">;
export const stageLabels: Record<string, string> = {
  prepare: "Prepare",
  implement: "Develop",
  verify: "Test",
  checkpoint: "Checkpoint",
  publish: "Publish",
  deploy: "Deploy",
  health: "Health checks",
};

export function dashboardPath(
  selection: FactorySelection,
  cursor?: string | null,
  bucket?: DashboardBucket | null,
): string {
  const query = new URLSearchParams({ kind: selection.kind });
  if (selection.kind === "project") query.set("project_id", selection.project);
  if (selection.kind === "run") query.set("run_id", selection.run);
  if (cursor) query.set("cursor", cursor);
  if (bucket) query.set("bucket", bucket);
  return "/factory-dashboard?" + query;
}
export function aggregateStations(
  stages: DashboardStage[],
  asOf?: string,
): FactoryStation[] {
  return Object.entries(stageLabels).map(([id, label]) => {
    const row = stages.find((item) => item.id === id);
    const observed = Boolean(
      row?.active &&
      row.observed_until &&
      asOf &&
      Date.parse(row.observed_until) > Date.parse(asOf),
    );
    return {
      id,
      label,
      counts: row
        ? {
            active: observed ? row.active : 0,
            waiting: row.waiting,
            needs_action: row.needs_action,
            unknown: row.unknown + (observed ? 0 : row.active),
            done: row.done,
          }
        : undefined,
      state: observed
        ? "active"
        : row?.failed
          ? "failed"
          : row?.needs_action
            ? "paused"
            : row?.unknown || row?.active || row?.expired || row?.quarantined
              ? "unknown"
              : row?.done
                ? "done"
                : row?.waiting
                  ? "waiting"
                  : row?.skipped
                    ? "skipped"
                    : "unknown",
      detail: row
        ? [
            observed
              ? `${row.active.toLocaleString()} active leases observed`
              : null,
            row.recorded_running && !observed
              ? `${row.recorded_running.toLocaleString()} tasks last recorded running; current worker activity unknown`
              : null,
            row.waiting ? `${row.waiting.toLocaleString()} queued` : null,
            row.done ? `${row.done.toLocaleString()} completed` : null,
            row.failed ? `${row.failed.toLocaleString()} failed` : null,
            row.needs_action
              ? `${row.needs_action.toLocaleString()} need action`
              : null,
            row.unknown && !row.recorded_running && !observed
              ? `${row.unknown.toLocaleString()} task activity unknown`
              : null,
            row.expired
              ? `${row.expired.toLocaleString()} expired leases`
              : null,
            row.quarantined
              ? `${row.quarantined.toLocaleString()} quarantined`
              : null,
          ]
            .filter(Boolean)
            .join(" · ") || "No task records in this scope."
        : "No stage records in this scope.",
    };
  });
}

export function recordedRunStations(
  run: DashboardRun | null,
  plan: DevelopmentPlan | null,
  observations: DashboardStage[] = [],
  asOf?: string,
): FactoryStation[] {
  if (!run || !plan) return aggregateStations([]);
  const observed = aggregateStations(observations, asOf);
  return manufacturingStations(
    {
      ...run,
      pr_url: run.pr_url ?? undefined,
      active_task: run.active_task ?? undefined,
    },
    plan,
    null,
  ).map((station) => {
    const recorded = plan.tasks.some(
      (task) =>
        task.id === station.id ||
        (station.id === "publish" && task.id.startsWith("publish")),
    );
    if (
      !recorded ||
      (station.state === "active" &&
        observed.find((item) => item.id === station.id)?.state !== "active")
    )
      return {
        ...station,
        state: "unknown",
        detail:
          "The task is recorded as running; a current owned runner lease is not available.",
      };
    return station;
  });
}
