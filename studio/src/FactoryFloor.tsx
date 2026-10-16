/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { repositoryLabels } from "./RepositoryLabels";
import { useEffect, useState } from "react";
import { ArrowRight, Hammer, RefreshCw } from "lucide-react";
import {
  request,
  type Connection,
  type ForgeRun,
  type Repository,
} from "./api";
import { FlowBoard } from "./FlowBoard";
import "./FactoryFloor.css";
import { FactoryScene } from "./FactoryScene";
import {
  manufacturingStations,
  type DevelopmentPlan,
  type DeploymentJob,
} from "./factory";

export function FactoryFloor({
  connection,
  connected,
  repositories,
  runs,
  theme,
  onOpen,
  onCreate,
  onProjectChange,
}: {
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
  runs: ForgeRun[];
  theme: "light" | "dark";
  onOpen: (run: ForgeRun) => void;
  onCreate: () => void;
  onProjectChange: (id: string) => void;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [project, setProject] = useState("");
  const [view, setView] = useState("floor");
  const activeProject =
    repositories.find((item) => item.id === project)?.id ??
    repositories.find((item) => item.id === runs[0]?.repository_id)?.id ??
    repositories[0]?.id ??
    "";
  useEffect(() => {
    if (activeProject) {
      if (project !== activeProject) setProject(activeProject);
      onProjectChange(activeProject);
    }
  }, [activeProject, project, onProjectChange]);
  const [chosenRun, setChosenRun] = useState("");
  const [selectedStation, setStation] = useState("prepare");
  const [planRun, setPlanRun] = useState("");
  const [plan, setPlan] = useState<DevelopmentPlan | null>(null);
  const [deployments, setDeployments] = useState<DeploymentJob[]>([]);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const projectRuns = runs.filter(
    (run) => Boolean(activeProject) && run.repository_id === activeProject,
  );
  const run =
    projectRuns.find((item) => item.id === chosenRun) ??
    projectRuns.find(
      (item) => !["succeeded", "failed", "cancelled"].includes(item.status),
    ) ??
    projectRuns[0] ??
    null;
  const runId = run?.id;
  useEffect(() => {
    if (!connected || !runId) return;
    const controller = new AbortController();
    const identifier = runId;
    async function update() {
      try {
        const [currentPlan, jobs] = await Promise.all([
          request<DevelopmentPlan>(connection, `/runs/${identifier}/plan`, {
            signal: controller.signal,
          }),
          request<DeploymentJob[]>(connection, "/deployments", {
            signal: controller.signal,
          }),
        ]);
        if (!controller.signal.aborted) {
          setPlanRun(identifier);
          setPlan(Array.isArray(currentPlan.tasks) ? currentPlan : null);
          setDeployments(
            Array.isArray(jobs)
              ? jobs.filter((job) => job.run_id === identifier)
              : [],
          );
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "The production record could not be loaded.",
          );
      }
    }
    void update();
    const timer = window.setInterval(() => void update(), 2500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [connected, connection, runId, refresh]);
  const currentPlan = planRun === runId ? plan : null;
  const stations = manufacturingStations(
    run,
    currentPlan,
    deployments.find((job) => job.run_id === runId) ?? null,
  );
  const selected =
    stations.find((station) => station.id === selectedStation) ?? stations[0];
  const completed =
    currentPlan?.tasks.filter((task) => task.status === "done").length ?? 0;
  return (
    <section className="production-floor" aria-labelledby="production-title">
      <div className="production-heading">
        <h2 id="production-title">
          <Hammer size={19} />
          Production floor
        </h2>
        <button
          className="quiet"
          disabled={!connected || !run}
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={15} />
          Refresh
        </button>
      </div>
      <div className="production-filters">
        <label>
          Project
          <select
            aria-label="Project"
            value={activeProject}
            onChange={(event) => {
              setProject(event.target.value);
              setChosenRun("");
              setPlan(null);
              setDeployments([]);
            }}
          >
            {!repositories.length && (
              <option value="">No connected projects</option>
            )}
            {repositories.map((item) => (
              <option key={item.id} value={item.id}>
                {projectLabels.get(item.id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Manufacturing run
          <select
            aria-label="Manufacturing run"
            title={run?.brief ?? "No recorded run"}
            value={run?.id ?? ""}
            disabled={!projectRuns.length}
            onChange={(event) => {
              setChosenRun(event.target.value);
              setPlan(null);
              setDeployments([]);
            }}
          >
            <option value="" disabled>
              {projectRuns.length ? "Choose a run" : "No recorded runs"}
            </option>
            {projectRuns.map((item) => (
              <option key={item.id} value={item.id}>
                {item.id.slice(0, 8)} ·{" "}
                {item.brief.split("\n")[0].length > 52
                  ? `${item.brief
                      .split("\n")[0]
                      .slice(0, 48)
                      .replace(/\s+\S*$/, "")}…`
                  : item.brief.split("\n")[0]}
              </option>
            ))}
          </select>
        </label>
        <p className="production-status" role="status">
          {run
            ? `${run.status.replaceAll("_", " ")} · ${completed}/${currentPlan?.tasks.length ?? 0} tasks recorded`
            : connected
              ? "Ready for your first run"
              : "Connect your factory to see live work"}
        </p>
      </div>
      <p className="production-explanation">
        Choose a project and run, then select a stage.
      </p>
      <div
        className="production-view-switch feature-tabs"
        role="group"
        aria-label="Production view"
      >
        <button
          className="quiet"
          aria-pressed={view === "floor"}
          onClick={() => setView("floor")}
        >
          Floor
        </button>
        <button
          className="quiet"
          aria-pressed={view === "board"}
          onClick={() => setView("board")}
        >
          Board
        </button>
      </div>
      {view === "board" ? (
        <FlowBoard
          runs={projectRuns}
          selected={run}
          plan={currentPlan}
          onOpen={onOpen}
          onSelect={setChosenRun}
        />
      ) : (
        <>
          <FactoryScene
            key={runId ?? "empty"}
            theme={theme}
            stations={stations}
            selectedId={selected.id}
            onSelect={setStation}
            suspended={Boolean(error) || !connected}
          />
          <div className="production-inspector">
            <div>
              <strong>{selected.label}</strong>
              <span className={`station-state ${selected.state}`}>
                {selected.state}
              </span>
              <p>
                {run
                  ? selected.detail
                  : "Stations become active when the factory records work. No production is running in this view."}
              </p>
            </div>
            {run ? (
              <button className="secondary" onClick={() => onOpen(run)}>
                Inspect run
                <ArrowRight size={15} />
              </button>
            ) : (
              <button className="secondary" onClick={onCreate}>
                Start a run
                <ArrowRight size={15} />
              </button>
            )}
          </div>
        </>
      )}
      <details className="production-guide">
        <summary>How stages work</summary>
        <p>
          Prepare inspects source, Develop uses the coding provider, Test
          records executable checks, and Checkpoint freezes verified source.
          Publish, Deploy and Health checks require separate recorded actions.
        </p>
      </details>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}
