/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  aggregateStations,
  dashboardPath,
  type FactorySelection,
} from "./FactoryDashboard";

it("requests real product scope without inferring a repository identity", () => {
  const selection: FactorySelection = {
    kind: "project",
    project: "a".repeat(32),
    run: "",
    view: "floor",
  };
  expect(dashboardPath(selection)).toBe(
    "/factory-dashboard?kind=project&project_id=" + "a".repeat(32),
  );
  expect(
    dashboardPath({ ...selection, kind: "run", run: "b".repeat(32) }),
  ).toBe("/factory-dashboard?kind=run&run_id=" + "b".repeat(32));
  expect(dashboardPath({ ...selection, kind: "factory" })).not.toContain(
    "project_id",
  );
});

it("animates only current observed leases and keeps missing or expired observation unknown", () => {
  const stages = aggregateStations(
    [
      {
        id: "prepare",
        active: 1,
        done: 3,
        failed: 0,
        waiting: 0,
        needs_action: 0,
        skipped: 0,
        unknown: 0,
        observed_until: "2026-10-30T00:00:30Z",
      },
      {
        id: "implement",
        active: 7,
        done: 0,
        failed: 2,
        waiting: 3,
        needs_action: 4,
        skipped: 0,
        unknown: 0,
        observed_until: "2026-10-30T00:00:30Z",
      },
      {
        id: "verify",
        active: 0,
        done: 0,
        failed: 0,
        waiting: 0,
        needs_action: 0,
        skipped: 0,
        unknown: 5,
      },
    ],
    "2026-10-30T00:00:00Z",
  );
  expect(
    stages.filter((stage) => stage.state === "active").map((stage) => stage.id),
  ).toEqual(["prepare", "implement"]);
  expect(stages.find((stage) => stage.id === "verify")?.state).toBe("unknown");
  expect(stages.find((stage) => stage.id === "deploy")?.state).toBe("unknown");
  expect(stages.find((stage) => stage.id === "implement")?.detail).toContain(
    "7 active",
  );
  expect(
    aggregateStations(
      [
        {
          id: "implement",
          active: 2,
          done: 0,
          failed: 0,
          waiting: 0,
          needs_action: 0,
          skipped: 0,
          unknown: 0,
        },
      ],
      "2026-10-30T00:00:00Z",
    )[1].state,
  ).toBe("unknown");
  expect(
    aggregateStations(
      [
        {
          id: "implement",
          active: 2,
          done: 0,
          failed: 0,
          waiting: 0,
          needs_action: 0,
          skipped: 0,
          unknown: 0,
          observed_until: "2026-10-30T00:00:30Z",
        },
      ],
      "2026-10-30T00:00:30Z",
    )[1].state,
  ).toBe("unknown");
});
