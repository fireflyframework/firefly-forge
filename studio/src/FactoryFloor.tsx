/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronRight,
  FolderTree,
  LayoutGrid,
  List,
  RefreshCw,
} from "lucide-react";
import { request, type Connection } from "./api";
import { FactoryScene } from "./FactoryScene";
import {
  aggregateStations,
  recordedRunStations,
  type DashboardBucket,
  type DashboardRun,
  type FactorySelection,
} from "./FactoryDashboard";
import {
  FactoryRunLinks,
  FactoryRunRecords,
  bucketLabels,
} from "./FactoryRunRecords";
import { FactoryWorkDrawer } from "./FactoryWorkDrawer";
import { useFactoryDashboard } from "./useFactoryDashboard";
import type { DevelopmentPlan } from "./factory";
import "./FactoryFloor.css";
export type { FactorySelection } from "./FactoryDashboard";

export function FactoryFloor({
  connection,
  connected,
  theme,
  onOpen,
  selection,
  onSelectionChange,
}: {
  connection: Connection;
  connected: boolean;
  theme: "light" | "dark";
  onOpen: (id: string) => void;
  selection: FactorySelection;
  onSelectionChange: (selection: FactorySelection) => void;
}) {
  const [drawer, setDrawer] = useState(false);
  const [, tick] = useState(0);
  const [stationId, setStationId] = useState("prepare");
  const [paging, setPaging] = useState<{
    scope: string;
    cursor: string | null;
    history: (string | null)[];
    bucket: DashboardBucket | null;
  }>({ scope: "", cursor: null, history: [], bucket: null });
  const scope = JSON.stringify([
    selection.kind,
    selection.project,
    selection.run,
  ]);
  const page =
    paging.scope === scope
      ? paging
      : { scope, cursor: null, history: [], bucket: null };
  const data = useFactoryDashboard(
    connection,
    connected,
    selection,
    page.cursor,
    page.bucket,
  );
  const [plan, setPlan] = useState<{
    run: string;
    value: DevelopmentPlan | null;
    error: string;
  }>({ run: "", value: null, error: "" });
  const records = useRef<HTMLElement>(null);
  const run =
    selection.kind === "run"
      ? (data.snapshot?.runs.items.find((item) => item.id === selection.run) ??
        null)
      : null;
  useEffect(() => {
    if (!connected || selection.kind !== "run" || !run) return;
    const controller = new AbortController(),
      id = run.id;
    void request<DevelopmentPlan>(
      connection,
      `/runs/${encodeURIComponent(id)}/plan`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!Array.isArray(value.tasks))
          throw new Error("The current plan is unavailable.");
        if (!controller.signal.aborted) setPlan({ run: id, value, error: "" });
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setPlan({
            run: id,
            value: null,
            error:
              cause instanceof Error
                ? cause.message
                : "The current plan could not be read.",
          });
      });
    return () => controller.abort();
  }, [
    connection,
    connected,
    selection.kind,
    selection.run,
    run,
    data.snapshot?.observed_at,
  ]);
  const currentPlan = run && plan.run === run.id ? plan.value : null;
  useEffect(() => {
    if (
      !data.snapshot?.stages.some(
        (stage) => stage.active && stage.observed_until,
      )
    )
      return;
    const timer = window.setInterval(() => tick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [data.snapshot]);
  const asOf =
    data.snapshot && data.requestStartedAt !== null
      ? new Date(
          Date.parse(data.snapshot.observed_at) +
            Math.max(0, performance.now() - data.requestStartedAt),
        ).toISOString()
      : undefined;
  const stations =
    selection.kind === "run"
      ? recordedRunStations(run, currentPlan, data.snapshot?.stages, asOf)
      : aggregateStations(data.snapshot?.stages ?? [], asOf);
  const selected =
    stations.find((item) => item.id === stationId) ?? stations[0];
  const title =
    selection.kind === "factory"
      ? "Factory overview"
      : selection.kind === "standalone"
        ? "Standalone repository work"
        : selection.kind === "project"
          ? data.snapshot?.scope.project?.name || "Product project"
          : "Run " + selection.run.slice(0, 8);
  function choose(value: FactorySelection) {
    setDrawer(false);
    setPlan({ run: "", value: null, error: "" });
    setStationId("prepare");
    onSelectionChange(value);
  }
  function focus(value: DashboardRun) {
    choose({
      ...selection,
      kind: "run",
      project: value.project_id ?? "",
      run: value.id,
      parent: selection.kind === "run" ? selection.parent : selection.kind,
    });
  }
  function bucket(value: DashboardBucket | null) {
    setPaging({ scope, cursor: null, history: [], bucket: value });
    records.current?.scrollIntoView({ block: "start" });
  }
  return (
    <section
      className="production-floor production-dashboard"
      aria-label="Factory observations"
    >
      <header className="factory-scope-caption">
        <div>
          <h2>{title}</h2>
          <p>
            {selection.kind === "project" && data.snapshot?.scope.project
              ? `${data.snapshot.scope.project.component_count.toLocaleString()} ${data.snapshot.scope.project.component_count === 1 ? "component" : "components"} across ${data.snapshot.scope.project.repository_count.toLocaleString()} ${data.snapshot.scope.project.repository_count === 1 ? "repository" : "repositories"}`
              : selection.kind === "run"
                ? "One recorded execution, its plan and source-control links"
                : data.snapshot
                  ? `${data.snapshot.associations.projects.toLocaleString()} product ${data.snapshot.associations.projects === 1 ? "project" : "projects"} · ${data.snapshot.associations.standalone_runs.toLocaleString()} standalone ${data.snapshot.associations.standalone_runs === 1 ? "run" : "runs"}`
                  : "Reading project and run records"}
          </p>
        </div>
        <button
          className="quiet icon-button"
          aria-label="Refresh factory records"
          disabled={!connected || data.loading}
          onClick={data.refresh}
        >
          <RefreshCw size={18} className={data.loading ? "spin" : ""} />
        </button>
      </header>
      {selection.view === "floor" && (
        <FactoryScene
          theme={theme}
          stations={stations}
          selectedId={selected.id}
          onSelect={setStationId}
          aggregate={selection.kind !== "run" || !currentPlan}
          observationLabel={
            data.snapshot
              ? `${data.snapshot.unavailable_metrics.includes("worker_liveness") ? "Runner activity unavailable" : `${stations.reduce((sum, stage) => sum + (stage.counts?.active ?? (stage.state === "active" ? 1 : 0)), 0).toLocaleString()} observed active tasks`} · Updated ${new Date(data.snapshot.observed_at).toLocaleTimeString()}${data.error ? " · updates unavailable" : ""}`
              : data.loading
                ? "Reading factory records…"
                : "Observation unavailable"
          }
          suspended={
            !connected ||
            !data.snapshot ||
            Boolean(data.error) ||
            (selection.kind === "run" && !currentPlan)
          }
        />
      )}
      <div className="production-context-dock">
        <button
          className="secondary"
          onClick={() => setDrawer(true)}
          disabled={!connected}
        >
          <FolderTree size={18} />
          Browse work
        </button>
        <nav aria-label="Current factory context">
          {selection.kind === "factory" ? (
            <span>All projects and standalone work</span>
          ) : (
            <button
              className="quiet"
              onClick={() =>
                choose({
                  kind: "factory",
                  project: "",
                  run: "",
                  view: selection.view,
                })
              }
            >
              Factory
            </button>
          )}
          {selection.kind !== "factory" && (
            <>
              <ChevronRight size={14} />
              <span>
                {selection.kind === "project"
                  ? data.snapshot?.scope.project?.name || "Product project"
                  : selection.kind === "standalone"
                    ? "Standalone work"
                    : run?.project_name ||
                      (run?.association === "standalone"
                        ? "Standalone run"
                        : "Run")}
              </span>
            </>
          )}
          {selection.kind === "run" && (
            <code title={selection.run}>{selection.run.slice(0, 8)}</code>
          )}
        </nav>
        <div
          className="factory-view-toggle"
          role="group"
          aria-label="Production view"
        >
          <button
            className="quiet icon-button"
            aria-pressed={selection.view === "floor"}
            aria-label="Show production floor"
            onClick={() => onSelectionChange({ ...selection, view: "floor" })}
          >
            <LayoutGrid size={18} />
            <span>Floor</span>
          </button>
          <button
            className="quiet icon-button"
            aria-pressed={selection.view === "board"}
            aria-label="Show run board"
            onClick={() => onSelectionChange({ ...selection, view: "board" })}
          >
            <List size={18} />
            <span>Board</span>
          </button>
        </div>
      </div>
      {data.error && (
        <div className="alert" role="alert">
          {data.error}
          {data.snapshot && (
            <p>
              Showing the previous observation. Machinery is paused until a
              fresh read succeeds.
            </p>
          )}
          <button className="quiet" onClick={data.refresh}>
            Try reading again
          </button>
        </div>
      )}
      {!connected && (
        <p className="factory-empty">
          Sign in to read the factory's authorized work.
        </p>
      )}
      {data.loading && !data.snapshot && (
        <div className="factory-observation-loading" role="status">
          Reading factory records…
        </div>
      )}
      {data.snapshot && (
        <>
          <section
            className="factory-inspection"
            aria-labelledby="factory-stage-title"
          >
            <div>
              <h3 id="factory-stage-title">
                {selection.view === "floor"
                  ? `${selected.label} tasks`
                  : "Recorded work"}
              </h3>
              <p>
                {selection.view === "floor"
                  ? selected.detail
                  : "Board totals cover the selected scope. Cards are paged; opening a state reads its matching records."}
              </p>
              {run && <FactoryRunLinks run={run} />}
            </div>
            <div>
              <h4 className="factory-counts-title">Runs in this scope</h4>
              <dl className="factory-run-counts">
                {(["active", "queued", "needs_action", "unknown"] as const).map(
                  (key) => (
                    <div key={key}>
                      <dt>{bucketLabels[key]}</dt>
                      <dd>
                        <button
                          className="quiet"
                          onClick={() => bucket(key)}
                          aria-label={`Show ${bucketLabels[key].toLowerCase()} runs`}
                        >
                          {data.snapshot!.counts[key].toLocaleString()}
                        </button>
                      </dd>
                    </div>
                  ),
                )}
              </dl>
            </div>
          </section>
          {selection.kind === "run" && (
            <div className="factory-run-actions">
              <button
                className="quiet"
                onClick={() =>
                  choose({
                    kind: selection.parent ?? "factory",
                    project: selection.project,
                    run: "",
                    view: selection.view,
                  })
                }
              >
                <ArrowLeft size={16} />
                Back to work scope
              </button>
              <button
                className="secondary"
                onClick={() => onOpen(selection.run)}
              >
                Open this run
                <ChevronRight size={16} />
              </button>
              {plan.run === selection.run && plan.error && (
                <p role="status">
                  Plan unavailable: {plan.error}. No single-run carrier is
                  shown.
                </p>
              )}
            </div>
          )}
          <details className="production-guide">
            <summary>How to read this view</summary>
            <p>
              Counts cover every authorized record in this scope, not just the
              current page. A product project can span several component
              repositories; standalone work remains separate. Aggregate views
              use no single-run carrier. Open a run to inspect its exact plan,
              branch, ticket and PR.
            </p>
            <p>
              Prepare inspects source, Develop uses the selected provider, Test
              records checks and Checkpoint records source. Publish, Deploy and
              Health checks require separate recorded actions. A stored active
              state does not prove that a worker is alive. Per-run worker
              liveness is unavailable in this deployment, so machines stay still
              and running task records show unknown activity. Queue duration,
              live deployment health and runtime metrics are also unavailable.
            </p>
            {data.snapshot.unmapped_active_tasks > 0 && (
              <p>
                {data.snapshot.unmapped_active_tasks.toLocaleString()} recorded
                active tasks have no known station mapping.
              </p>
            )}
            <dl className="factory-terminal-counts">
              {(
                [
                  "completed",
                  "failed",
                  "cancelled",
                  "expired",
                  "quarantined",
                ] as const
              ).map((key) => (
                <div key={key}>
                  <dt>{bucketLabels[key]}</dt>
                  <dd>{data.snapshot!.counts[key].toLocaleString()}</dd>
                </div>
              ))}
            </dl>
          </details>
          <section ref={records} className="factory-records-anchor">
            <FactoryRunRecords
              snapshot={data.snapshot}
              board={selection.view === "board"}
              bucket={page.bucket}
              loading={data.loading}
              canPrevious={page.history.length > 0}
              onBucket={bucket}
              onPrevious={() =>
                setPaging({
                  ...page,
                  scope,
                  cursor: page.history.at(-1) ?? null,
                  history: page.history.slice(0, -1),
                })
              }
              onNext={() =>
                setPaging({
                  ...page,
                  scope,
                  cursor: data.snapshot!.runs.next_cursor,
                  history: [...page.history, page.cursor],
                })
              }
              onFocus={focus}
              onOpen={onOpen}
            />
          </section>
        </>
      )}
      <FactoryWorkDrawer
        connection={connection}
        open={drawer}
        standalone={data.snapshot?.standalone_available ?? false}
        selection={selection}
        onChoose={choose}
        onClose={() => setDrawer(false)}
      />
    </section>
  );
}
