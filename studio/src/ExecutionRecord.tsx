/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { post, request, type Connection, type ForgeRun } from "./api";
import type { DevelopmentPlan } from "./factory";

type Journal = {
  valid: boolean;
  entries: {
    sequence: number;
    task_id: string;
    kind: string;
    created_at: string;
    digest: string;
    payload: Record<string, unknown>;
  }[];
};
const message = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The execution record could not be loaded.";

export function ContinueRun({
  run,
  connection,
  onUpdated,
}: {
  run: ForgeRun;
  connection: Connection;
  onUpdated: (run: ForgeRun) => void;
}) {
  const [agent, setAgent] = useState(run.agent ?? "codex");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function resume(handoff: boolean) {
    setBusy(true);
    setError("");
    try {
      onUpdated(
        await post<ForgeRun>(
          connection,
          `/runs/${run.id}/${handoff ? "handoff" : "resume"}`,
          handoff ? { agent } : {},
        ),
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  if (!["paused", "needs_reconciliation", "interrupted"].includes(run.status))
    return null;
  return (
    <section className="continuation-panel" aria-label="Continue retained work">
      <div>
        <h2>Continue this run</h2>
        <p>
          {run.pause?.message ??
            "The worker stopped. Inspect the retained source and task record before continuing."}
        </p>
        {run.pause?.automatic && run.pause.retry_at && (
          <p>
            The server will retry after{" "}
            {new Date(run.pause.retry_at * 1000).toLocaleString()}.
          </p>
        )}
        <small>
          The workspace, completed tasks and frozen project policies remain
          attached to this run.
        </small>
      </div>
      <div className="integration-actions">
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void resume(false)}
        >
          Resume run
        </button>
      </div>
      <div className="continuation-handoff">
        <label htmlFor="handoff-agent">Continue with another provider</label>
        <select
          id="handoff-agent"
          value={agent}
          onChange={(event) => setAgent(event.target.value)}
        >
          <option value="codex">Codex</option>
          <option value="claude">Claude Code</option>
          <option value="bridge">Firefly Agent Bridge</option>
          <option value="agentic">Firefly Agentic</option>
        </select>
        <button
          className="secondary"
          disabled={busy || agent === run.agent}
          onClick={() => void resume(true)}
        >
          Hand off run
        </button>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

export function ExecutionRecord({
  run,
  connection,
}: {
  run: ForgeRun;
  connection: Connection;
}) {
  const [plan, setPlan] = useState<DevelopmentPlan | null>(null);
  const [journal, setJournal] = useState<Journal | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    async function update() {
      try {
        const [currentPlan, currentJournal] = await Promise.all([
          request<DevelopmentPlan>(connection, `/runs/${run.id}/plan`, {
            signal: controller.signal,
          }),
          request<Journal>(connection, `/runs/${run.id}/journal`, {
            signal: controller.signal,
          }),
        ]);
        if (!controller.signal.aborted) {
          setPlan(currentPlan);
          setJournal(currentJournal);
          setError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(message(cause));
      }
    }
    void update();
    const timer = window.setInterval(() => void update(), 2500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [connection, run.id]);
  return (
    <div className="execution-record">
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      <div className="section-heading">
        <div>
          <h2>Development plan</h2>
          <p className="subtle">
            Each task records its intent before execution and its result when
            completed.
          </p>
        </div>
        {plan?.revision && (
          <span className="badge">Revision {plan.revision}</span>
        )}
      </div>
      <ol className="execution-tasks">
        {plan?.tasks?.map((task) => (
          <li key={task.id}>
            <div>
              <strong>{task.title ?? task.id}</strong>
              <span className={`station-state ${task.status}`}>
                {task.status.replaceAll("_", " ")}
              </span>
            </div>
            <p>{task.instructions}</p>
            <small>Attempt {task.attempt ?? 1}</small>
            {task.error?.message && (
              <p className="task-error">{task.error.message}</p>
            )}
            {task.evidence && (
              <details>
                <summary>Recorded result</summary>
                <pre>{JSON.stringify(task.evidence, null, 2)}</pre>
              </details>
            )}
          </li>
        ))}
      </ol>
      {(run.checkpoint_revision || run.source_tree) && (
        <dl className="wizard-summary">
          <div>
            <dt>Source tree</dt>
            <dd>{run.source_tree}</dd>
          </div>
          {run.checkpoint_revision && (
            <div>
              <dt>Checkpoint commit</dt>
              <dd>{run.checkpoint_revision}</dd>
            </div>
          )}
        </dl>
      )}
      <section className="integration-section">
        <div className="section-heading">
          <h2>Execution journal</h2>
          {journal && (
            <span className={"badge " + (journal.valid ? "success" : "failed")}>
              {journal.valid ? "Chain verified" : "Chain verification failed"}
            </span>
          )}
        </div>
        <ol className="journal-entries">
          {journal?.entries?.map((entry) => (
            <li key={entry.sequence}>
              <div>
                <span>{entry.sequence}</span>
                <strong>{entry.task_id}</strong>
                <span>{entry.kind}</span>
              </div>
              <time>{new Date(entry.created_at).toLocaleString()}</time>
              <details>
                <summary>Evidence and record hash</summary>
                <pre>{JSON.stringify(entry.payload, null, 2)}</pre>
                <small>{entry.digest}</small>
              </details>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
