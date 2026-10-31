/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { ApiError, request, type Connection } from "./api";
import {
  dashboardPath,
  type DashboardBucket,
  type DashboardSnapshot,
  type FactorySelection,
} from "./FactoryDashboard";

export function useFactoryDashboard(
  connection: Connection,
  connected: boolean,
  selection: FactorySelection,
  cursor: string | null,
  bucket: DashboardBucket | null,
) {
  const path = dashboardPath(selection, cursor, bucket);
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<{
    path: string;
    value: DashboardSnapshot | null;
    error: string;
    loading: boolean;
  }>({ path: "", value: null, error: "", loading: false });
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let pending = false;
    function schedule() {
      timer = window.setTimeout(() => {
        if (controller.signal.aborted) return;
        if (document.hidden) schedule();
        else void update();
      }, 15000);
    }
    async function update() {
      if (controller.signal.aborted || pending) return;
      pending = true;
      setState((previous) => ({
        path,
        value: previous.path === path ? previous.value : null,
        error: "",
        loading: true,
      }));
      try {
        const value = await request<DashboardSnapshot>(connection, path, {
          signal: controller.signal,
        });
        const counts = [
          "active",
          "queued",
          "needs_action",
          "completed",
          "failed",
          "cancelled",
          "expired",
          "quarantined",
          "unknown",
        ] as const;
        if (
          value.version !== 1 ||
          value.scope?.kind !== selection.kind ||
          value.scope.project_id !==
            (selection.kind === "project" ? selection.project : null) ||
          value.scope.run_id !==
            (selection.kind === "run" ? selection.run : null) ||
          !value.counts ||
          counts.some(
            (key) =>
              !Number.isSafeInteger(value.counts[key]) || value.counts[key] < 0,
          ) ||
          counts.reduce((sum, key) => sum + value.counts[key], 0) !==
            value.counts.total ||
          !Array.isArray(value.stages) ||
          !Array.isArray(value.runs?.items)
        )
          throw new Error(
            "The server returned an incompatible factory observation. Refresh or check the server version.",
          );
        if (!controller.signal.aborted)
          setState({ path, value, error: "", loading: false });
      } catch (cause) {
        if (!controller.signal.aborted)
          setState((previous) => ({
            path,
            value:
              cause instanceof ApiError &&
              [401, 403, 404].includes(cause.status)
                ? null
                : previous.path === path
                  ? previous.value
                  : null,
            error:
              cause instanceof Error
                ? cause.message
                : "Factory records could not be read.",
            loading: false,
          }));
      } finally {
        pending = false;
        if (!controller.signal.aborted) schedule();
      }
    }
    const visible = () => {
      if (!document.hidden && !pending) {
        window.clearTimeout(timer);
        void update();
      }
    };
    void update();
    document.addEventListener("visibilitychange", visible);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [
    connection,
    connected,
    path,
    revision,
    selection.kind,
    selection.project,
    selection.run,
  ]);
  return {
    snapshot: connected && state.path === path ? state.value : null,
    error: state.path === path ? state.error : "",
    loading: connected && (state.path !== path || state.loading),
    refresh: () => setRevision((value) => value + 1),
  };
}
