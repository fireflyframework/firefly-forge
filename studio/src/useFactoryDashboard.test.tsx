// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useFactoryDashboard } from "./useFactoryDashboard";
import type { FactorySelection } from "./FactoryDashboard";

const connection = { base: "/api/v1", token: "" };
const scope = (project: string): FactorySelection => ({
  kind: "project",
  project,
  run: "",
  view: "floor",
});
function observation(project: string) {
  return {
    version: 1,
    scope: {
      kind: "project",
      project_id: project,
      run_id: null,
      project: { id: project, name: project },
    },
    observed_at: "2026-10-30T00:00:00Z",
    counts: {
      total: 2,
      active: 2,
      queued: 0,
      needs_action: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
      expired: 0,
      quarantined: 0,
      unknown: 0,
    },
    stages: [],
    runs: { items: [], next_cursor: null },
    associations: { projects: 1 },
    standalone_available: false,
  };
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("discards a late former-project response even when the transport ignores abort", async () => {
  let complete: (response: Response) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      url.includes("a".repeat(32))
        ? new Promise<Response>((resolve) => {
            complete = resolve;
          })
        : Promise.resolve(Response.json(observation("b".repeat(32)))),
    ),
  );
  const result = renderHook(
    ({ selection }) =>
      useFactoryDashboard(connection, true, selection, null, null),
    { initialProps: { selection: scope("a".repeat(32)) } },
  );
  result.rerender({ selection: scope("b".repeat(32)) });
  await waitFor(() =>
    expect(result.result.current.snapshot?.scope.project_id).toBe(
      "b".repeat(32),
    ),
  );
  await act(async () => complete(Response.json(observation("a".repeat(32)))));
  expect(result.result.current.snapshot?.scope.project_id).toBe("b".repeat(32));
});

it("clears the retained observation after explicit project-access denial", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(Response.json(observation("a".repeat(32))))
    .mockResolvedValue(
      Response.json(
        { detail: "Current project access was removed" },
        { status: 403 },
      ),
    );
  vi.stubGlobal("fetch", fetcher);
  const result = renderHook(() =>
    useFactoryDashboard(connection, true, scope("a".repeat(32)), null, null),
  );
  await waitFor(() =>
    expect(result.result.current.snapshot?.counts.active).toBe(2),
  );
  act(() => result.result.current.refresh());
  await waitFor(() =>
    expect(result.result.current.error).toContain(
      "Current project access was removed",
    ),
  );
  expect(result.result.current.snapshot).toBeNull();
});

it("does not display an incompatible or differently scoped server response", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(Response.json(observation("a".repeat(32)))),
  );
  const result = renderHook(() =>
    useFactoryDashboard(connection, true, scope("b".repeat(32)), null, null),
  );
  await waitFor(() =>
    expect(result.result.current.error).toContain("incompatible"),
  );
  expect(result.result.current.snapshot).toBeNull();
});
