/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { ForgeRun } from "./api";
import type { DevelopmentPlan } from "./factory";
import { safeExternalLink } from "./FeatureLinks";
import "./FlowBoard.css";
const lanes = [
  { id: "queue", label: "Queued", statuses: ["queued", "pending"] },
  {
    id: "coding",
    label: "Developing",
    statuses: [
      "running",
      "cloning",
      "preparing",
      "generating",
      "developing",
      "executing",
      "publishing",
    ],
  },
  { id: "checks", label: "Validating", statuses: ["verifying", "validating"] },
  {
    id: "action",
    label: "Awaiting action",
    statuses: ["paused", "interrupted", "needs_reconciliation", "cancelled"],
  },
  { id: "done", label: "Completed", statuses: ["succeeded"] },
  { id: "failed", label: "Failed", statuses: ["failed"] },
];
export function FlowBoard({
  runs,
  selected,
  plan,
  onOpen,
  onSelect,
}: {
  runs: ForgeRun[];
  selected: ForgeRun | null;
  plan: DevelopmentPlan | null;
  onOpen: (run: ForgeRun) => void;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="flow-board">
      <p className="flow-explanation">
        Cards use recorded run states. Select a run to inspect its actual plan;
        branches and tickets appear only when recorded.
      </p>
      <div className="flow-lanes">
        {lanes.map((lane) => (
          <section className="flow-lane" key={lane.id} aria-label={lane.label}>
            <h3>
              {lane.label}
              <span>
                {
                  runs.filter(
                    (run) =>
                      (lanes.find((item) => item.statuses.includes(run.status))
                        ?.id ?? "action") === lane.id,
                  ).length
                }
              </span>
            </h3>
            {runs
              .filter(
                (run) =>
                  (lanes.find((item) => item.statuses.includes(run.status))
                    ?.id ?? "action") === lane.id,
              )
              .map((run) => (
                <article
                  className={`flow-card ${selected?.id === run.id ? "selected" : ""}`}
                  key={run.id}
                >
                  <button
                    className="flow-card-title"
                    aria-pressed={selected?.id === run.id}
                    onClick={() => onSelect(run.id)}
                  >
                    {run.brief.split("\n")[0]}
                  </button>
                  <span className="feature-status">
                    {run.status.replaceAll("_", " ")}
                  </span>
                  <dl>
                    <div>
                      <dt>Branch</dt>
                      <dd>{run.branch || "Not recorded yet"}</dd>
                    </div>
                    {run.issue_context && (
                      <div>
                        <dt>{run.issue_context.kind.replaceAll("_", " ")}</dt>
                        <dd>
                          {safeExternalLink(run.issue_context.url) ? (
                            <a
                              href={safeExternalLink(run.issue_context.url)!}
                              rel="noreferrer"
                              target="_blank"
                            >
                              #{run.issue_context.number}{" "}
                              {run.issue_context.title}
                            </a>
                          ) : (
                            `#${run.issue_context.number} ${run.issue_context.title ?? ""}`
                          )}
                        </dd>
                      </div>
                    )}
                  </dl>
                  {(run.pause?.message || run.error) && (
                    <p className="flow-blocker">
                      {run.pause?.message || run.error}
                    </p>
                  )}
                  <div className="feature-actions">
                    <button className="text-button" onClick={() => onOpen(run)}>
                      Inspect run
                    </button>
                    {safeExternalLink(run.pr_url) && (
                      <a
                        href={safeExternalLink(run.pr_url)!}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Pull request
                      </a>
                    )}
                  </div>
                </article>
              ))}
            {!runs.some(
              (run) =>
                (lanes.find((item) => item.statuses.includes(run.status))?.id ??
                  "action") === lane.id,
            ) && <p className="flow-empty">No recorded runs</p>}
          </section>
        ))}
      </div>
      <section className="flow-plan" aria-label="Selected run tasks">
        <h3>Selected run tasks</h3>
        {!selected ? (
          <p>Select a recorded run to inspect its plan.</p>
        ) : !plan ? (
          <p>No plan loaded for this run.</p>
        ) : !plan.tasks.length ? (
          <p>No plan tasks recorded.</p>
        ) : (
          <div className="flow-tasks">
            {plan.tasks.map((task) => (
              <article className="feature-record" key={task.id}>
                <div className="feature-toolbar">
                  <strong>{task.title || task.id}</strong>
                  <span className="feature-status">
                    {task.status.replaceAll("_", " ")}
                  </span>
                </div>
                {task.error?.message && (
                  <p className="flow-blocker">{task.error.message}</p>
                )}
                <p className="feature-meta">
                  {task.attempt === undefined
                    ? "No attempt reported"
                    : `Attempt ${task.attempt}`}
                </p>
                <details>
                  <summary>Task instructions</summary>
                  <p>{task.instructions || "No instructions recorded."}</p>
                </details>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
