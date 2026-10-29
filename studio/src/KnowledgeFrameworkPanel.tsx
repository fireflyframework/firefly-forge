/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { Connection } from "./api";
import { FrameworkReleaseRow } from "./KnowledgeFrameworks";
import {
  checkFrameworkUpdates,
  loadFrameworkStatus,
  pendingFrameworkKey,
  refreshBody,
  submitRetainedFrameworkRefresh,
  readPendingFrameworkRefresh,
  readFrameworkOperation,
  submitFrameworkControl,
  generationTargetBody,
  submitRetainedGenerationTarget,
  readGenerationTarget,
  type FrameworkActivationAttempt,
  type FrameworkControl,
  type FrameworkBudget,
  type FrameworkPendingAttempt,
  type FrameworkCorpus,
  type FrameworkDiscovery,
  type FrameworkName,
  type FrameworkRefreshAttempt,
  type FrameworkStatus,
} from "./FrameworkKnowledgeData";

export function KnowledgeFrameworkPanel({
  connection,
  connected,
  userId,
  onBrowse,
  onSearch,
}: {
  connection: Connection;
  connected: boolean;
  userId: string;
  onBrowse: (corpus: FrameworkCorpus) => void;
  onSearch: (corpus: FrameworkCorpus) => void;
}) {
  const [rows, setRows] = useState<FrameworkStatus[]>([]);
  const [discoveries, setDiscoveries] = useState<
    Partial<Record<FrameworkName, FrameworkDiscovery>>
  >({});
  const [pending, setPending] = useState<
    Partial<Record<FrameworkName, FrameworkPendingAttempt>>
  >({});
  const [storageReady, setStorageReady] = useState(false);
  const [busy, setBusy] = useState<FrameworkName | "loading" | null>(null);
  const [error, setError] = useState("");
  const [statusRefresh, setStatusRefresh] = useState(0);
  const epoch = useRef(0);
  const actionActive = useRef(false);

  useEffect(() => {
    const generation = ++epoch.current;
    actionActive.current = false;
    setRows([]);
    setDiscoveries({});
    setPending({});
    setError("");
    setStorageReady(false);
    if (!connected) return;
    const controller = new AbortController();
    setBusy("loading");
    const retained: Partial<Record<FrameworkName, FrameworkPendingAttempt>> =
      {};
    try {
      for (const framework of ["pyfly", "larafly"] as const) {
        const saved = readPendingFrameworkRefresh(
          localStorage,
          pendingFrameworkKey(connection, userId, framework),
          framework,
        );
        if (saved) retained[framework] = saved;
      }
      setPending(retained);
      setStorageReady(true);
    } catch {
      setError(
        "The retained refresh request could not be read. Restore browser storage before submitting another refresh.",
      );
    }
    loadFrameworkStatus(connection, controller.signal)
      .then((response) => {
        if (!controller.signal.aborted && epoch.current === generation)
          setRows(response.frameworks);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted && epoch.current === generation)
          setError(
            cause instanceof Error
              ? cause.message
              : "Framework status could not be read.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted && epoch.current === generation)
          setBusy(null);
      });
    return () => {
      controller.abort();
      epoch.current = generation + 1;
    };
  }, [connection, connected, userId, statusRefresh]);

  async function act(
    value: FrameworkStatus,
    kind:
      "check" | "refresh" | "read" | "observe" | "activate" | FrameworkControl,
    totalBudget?: FrameworkBudget,
    activation?: { corpus: FrameworkCorpus; validationRunId: string },
  ) {
    if (actionActive.current) return;
    actionActive.current = true;
    const generation = epoch.current;
    const retained = pending[value.framework];
    setBusy(value.framework);
    setError("");
    try {
      if (kind === "check") {
        const discovery = await checkFrameworkUpdates(
          connection,
          value.framework,
        );
        if (epoch.current !== generation) return;
        if (
          discovery.framework !== value.framework ||
          discovery.status.framework !== value.framework
        )
          throw new Error("The update response belongs to another framework.");
        setRows((items) =>
          items.map((item) =>
            item.framework === value.framework ? discovery.status : item,
          ),
        );
        setDiscoveries((items) => ({ ...items, [value.framework]: discovery }));
      } else if (
        kind === "activate" ||
        (kind === "read" && retained && "activation" in retained)
      ) {
        let target;
        if (kind === "activate") {
          if (!activation || !userId || !storageReady || retained)
            throw new Error(
              "Read the current activation and retain its exact review before another write.",
            );
          const attempt: FrameworkActivationAttempt = {
            activation: true,
            framework: value.framework,
            request_id: crypto.randomUUID().replaceAll("-", ""),
            corpus: activation.corpus,
            body: generationTargetBody(
              value,
              activation.corpus,
              activation.validationRunId,
            ),
          };
          target = await submitRetainedGenerationTarget(
            connection,
            attempt,
            localStorage,
            pendingFrameworkKey(connection, userId, value.framework),
            (saved) =>
              setPending((items) => ({ ...items, [value.framework]: saved })),
          );
        } else {
          if (!retained || !("activation" in retained))
            throw new Error("No retained activation is available.");
          target = await readGenerationTarget(connection, retained);
        }
        if (epoch.current !== generation) return;
        localStorage.removeItem(
          pendingFrameworkKey(connection, userId, value.framework),
        );
        setPending((items) => ({ ...items, [value.framework]: undefined }));
        setDiscoveries((items) => ({ ...items, [value.framework]: undefined }));
        setRows((items) =>
          items.map((item) =>
            item.framework === value.framework
              ? { ...item, generation_target: target }
              : item,
          ),
        );
        setStatusRefresh((value) => value + 1);
      } else {
        const operation = await runOperation(value, kind, totalBudget);
        if (epoch.current !== generation) return;
        localStorage.removeItem(
          pendingFrameworkKey(connection, userId, value.framework),
        );
        setPending((items) => ({ ...items, [value.framework]: undefined }));
        setDiscoveries((items) => ({ ...items, [value.framework]: undefined }));
        setRows((items) =>
          items.map((item) =>
            item.framework === value.framework
              ? { ...item, active_operation: operation }
              : item,
          ),
        );
        if (
          operation.state === "ready" ||
          ["renew_budget", "cancel", "resume"].includes(kind)
        )
          setStatusRefresh((value) => value + 1);
      }
    } catch (cause) {
      if (epoch.current === generation)
        setError(
          cause instanceof Error ? cause.message : "Framework request failed.",
        );
    } finally {
      if (epoch.current === generation) {
        actionActive.current = false;
        setBusy(null);
      }
    }
  }

  async function runOperation(
    value: FrameworkStatus,
    kind: "refresh" | "read" | "observe" | FrameworkControl,
    totalBudget?: FrameworkBudget,
  ) {
    if (kind === "renew_budget" || kind === "cancel" || kind === "resume") {
      if (
        !value.active_operation ||
        !userId ||
        !storageReady ||
        pending[value.framework]
      )
        throw new Error(
          "Read the current operation before changing its reviewed work.",
        );
      return submitFrameworkControl(
        connection,
        value.active_operation,
        kind,
        localStorage,
        pendingFrameworkKey(connection, userId, value.framework),
        totalBudget,
        (retained) =>
          setPending((items) => ({ ...items, [value.framework]: retained })),
      );
    }
    if (kind === "refresh") {
      const discovery = discoveries[value.framework];
      if (!discovery || !userId || !storageReady || pending[value.framework])
        throw new Error(
          "Read the current operation before starting a refresh.",
        );
      const attempt: FrameworkRefreshAttempt = {
        framework: value.framework,
        request_id: crypto.randomUUID().replaceAll("-", ""),
        body: refreshBody(discovery, value),
      };
      return submitRetainedFrameworkRefresh(
        connection,
        attempt,
        localStorage,
        pendingFrameworkKey(connection, userId, value.framework),
        (retained) =>
          setPending((items) => ({ ...items, [value.framework]: retained })),
      );
    }
    const id =
      pending[value.framework]?.request_id ??
      value.active_operation?.request_id;
    if (!id) throw new Error("No retained operation is available.");
    return readFrameworkOperation(
      connection,
      value.framework,
      id,
      kind === "observe",
    );
  }

  return (
    <div className="framework-releases">
      <div className="framework-release-intro">
        <div>
          <h2>Framework releases</h2>
          <p>
            Compare indexed references with official stable releases. Refreshing
            knowledge keeps project dependencies and frozen run pins unchanged.
          </p>
        </div>
        <button
          className="secondary"
          disabled={!connected || busy !== null}
          onClick={() => setStatusRefresh((value) => value + 1)}
        >
          <RefreshCw size={14} aria-hidden="true" /> Refresh status
        </button>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {busy === "loading" && (
        <p role="status">Reading cached framework status…</p>
      )}
      {!connected && <p>Connect to a factory to inspect framework releases.</p>}
      {connected && !busy && !rows.length && !error && (
        <p>No framework status was returned.</p>
      )}
      {rows.map((value) => {
        const retained = pending[value.framework];
        const canMutate = Boolean(userId && storageReady && !retained);
        return (
          <FrameworkReleaseRow
            key={value.framework}
            value={value}
            busy={busy !== null}
            pendingRequest={retained?.request_id}
            pendingIsChange={retained ? "control" in retained : false}
            pendingIsActivation={retained ? "activation" in retained : false}
            connection={connection}
            discovery={discoveries[value.framework]}
            onCheck={() => void act(value, "check")}
            onRefresh={
              userId && storageReady
                ? () => void act(value, "refresh")
                : undefined
            }
            onRead={() => void act(value, "read")}
            onObserve={
              !pending[value.framework]
                ? () => void act(value, "observe")
                : undefined
            }
            onBrowse={onBrowse}
            onSearch={onSearch}
            onRenew={
              canMutate
                ? (total) => void act(value, "renew_budget", total)
                : undefined
            }
            onCancel={canMutate ? () => void act(value, "cancel") : undefined}
            onResume={canMutate ? () => void act(value, "resume") : undefined}
            onActivate={
              canMutate
                ? (corpus, validationRunId) =>
                    void act(value, "activate", undefined, {
                      corpus,
                      validationRunId,
                    })
                : undefined
            }
          />
        );
      })}
    </div>
  );
}
