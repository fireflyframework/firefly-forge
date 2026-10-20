/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { FactoryStation } from "./FactoryScene";
type StationState = FactoryStation["state"];

export type PlannedTask = {
  id: string;
  title?: string;
  status: string;
  instructions: string;
  attempt?: number;
  evidence?: Record<string, unknown>;
  error?: { message: string; kind?: string };
};
export type DevelopmentPlan = {
  tasks: PlannedTask[];
  revision?: number;
  digest?: string;
  checkpoint?: Record<string, unknown>;
};
export type DeploymentJob = {
  id: string;
  run_id: string;
  status: string;
  operation?: string;
  target_id?: string;
  active_stage?: string;
  error?: string;
  evidence: {
    stage: string;
    exit_code: number;
    output?: string;
    argv?: string[];
  }[];
};
type RunState = {
  status: string;
  operation?: string;
  pr_url?: string | null;
  active_task?: string;
  error?: string | null;
};
const state = (status: string): StationState => {
  if (status === "done" || status === "succeeded") return "done";
  if (status === "running") return "active";
  if (["paused", "needs_reconciliation", "interrupted"].includes(status))
    return "paused";
  if (status === "failed") return "failed";
  return "waiting";
};
export function manufacturingStations(
  run: RunState | null,
  plan: DevelopmentPlan | null,
  deployment: DeploymentJob | null,
) {
  const labels = [
    ["prepare", "Prepare", "Clone and inspect the project source."],
    ["implement", "Develop", "Execute the selected coding provider."],
    ["verify", "Test", "Run the project's checks."],
    ["checkpoint", "Checkpoint", "Record the verified source and commit."],
    ["publish", "Publish", "Publish the reviewed source as a pull request."],
    ["deploy", "Deploy", "Deploy the verified source to an explicit target."],
    ["health", "Health checks", "Verify the deployment target."],
  ];
  return labels.map(([id, label, description]) => {
    const task = plan?.tasks.find(
      (item) =>
        item.id === id || (id === "publish" && item.id.startsWith("publish")),
    );
    let stationState: StationState = task ? state(task.status) : "waiting";
    let detail = task ? task.error?.message || task.instructions : description;
    if (
      run?.status === "failed" &&
      (stationState === "active" ||
        (run.active_task === id && stationState !== "done"))
    ) {
      stationState = "failed";
      detail =
        run.error || task?.error?.message || "The run failed at this stage.";
    } else if (
      stationState === "active" &&
      run &&
      [
        "paused",
        "needs_reconciliation",
        "interrupted",
        "cancelled",
        "succeeded",
      ].includes(run.status)
    ) {
      stationState = state(run.status === "succeeded" ? "queued" : run.status);
    }
    if (id === "implement" && run?.operation === "test") {
      stationState = "skipped";
      detail = "This test run does not use a coding provider.";
    }
    if (id === "publish" && run?.pr_url) {
      stationState = "done";
      detail = "A pull request was published. Review it in source control.";
    }
    if (deployment && (id === "deploy" || id === "health")) {
      const stage =
        id === "health"
          ? "verify"
          : deployment.operation === "rollback"
            ? "rollback"
            : "deploy";
      const evidence = deployment.evidence.filter(
        (item) => item.stage === stage,
      );
      if (deployment.status === "succeeded") stationState = "done";
      else if (
        deployment.status === "running" &&
        deployment.active_stage === stage
      )
        stationState = "active";
      else if (
        deployment.status === "interrupted" &&
        deployment.active_stage === stage
      )
        stationState = "paused";
      else if (
        deployment.status === "failed" &&
        (deployment.active_stage === stage ||
          evidence.some((item) => item.exit_code))
      )
        stationState = "failed";
      else if (
        evidence.length &&
        evidence.every((item) => item.exit_code === 0) &&
        deployment.active_stage !== stage
      )
        stationState = "done";
      else stationState = "waiting";
      detail =
        deployment.error && ["failed", "paused"].includes(stationState)
          ? deployment.error
          : `${deployment.operation === "rollback" ? "Rollback" : "Deployment"} ${deployment.id}: ${stage}. ${evidence.length} command results recorded.`;
    }
    return { id, label, state: stationState, detail };
  });
}
