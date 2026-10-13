/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { manufacturingStations } from "./factory";

describe("production floor reflects durable execution", () => {
  it("does not fabricate progress from a run being succeeded", () => {
    const stages = manufacturingStations(
      { status: "succeeded", operation: "build" },
      null,
      null,
    );
    expect(stages.every((stage) => stage.state === "waiting")).toBe(true);
  });
  it("stops the active machine when its provider is paused and retains receipted work", () => {
    const stages = manufacturingStations(
      { status: "paused", operation: "build" },
      {
        tasks: [
          { id: "prepare", status: "done", instructions: "Clone source" },
          {
            id: "implement",
            status: "paused",
            instructions: "Build alerts",
            error: { message: "Credits exhausted" },
          },
          { id: "verify", status: "queued", instructions: "Run checks" },
        ],
      },
      null,
    );
    expect(stages.find((stage) => stage.id === "prepare")?.state).toBe("done");
    expect(stages.find((stage) => stage.id === "implement")?.state).toBe(
      "paused",
    );
    expect(stages.find((stage) => stage.id === "implement")?.detail).toContain(
      "Credits exhausted",
    );
    expect(stages.filter((stage) => stage.state === "active")).toHaveLength(0);
  });
  it("shows deployment health only while its actual verification command runs", () => {
    const stages = manufacturingStations(
      { status: "succeeded", operation: "build" },
      null,
      {
        id: "deployment-1",
        run_id: "run-1",
        status: "running",
        active_stage: "verify",
        evidence: [{ stage: "deploy", exit_code: 0, output: "Deployed" }],
      },
    );
    expect(stages.find((stage) => stage.id === "deploy")?.state).toBe("done");
    expect(stages.find((stage) => stage.id === "health")?.state).toBe("active");
  });
  it("marks the coding station skipped for a direct test run and leaves publication waiting", () => {
    const stages = manufacturingStations(
      { status: "running", operation: "test" },
      { tasks: [] },
      null,
    );
    expect(stages.find((stage) => stage.id === "implement")?.state).toBe(
      "skipped",
    );
    expect(stages.find((stage) => stage.id === "publish")?.state).toBe(
      "waiting",
    );
  });
});
