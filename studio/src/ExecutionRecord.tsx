/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { post, request, type Connection, type ForgeRun } from "./api";
import type { DevelopmentPlan } from "./factory";
import {
  EvidenceView,
  ToolEvidence,
  journalDisposition,
} from "./ExecutionPresentation";

import { ProviderRecoveryAllowance } from "./ProviderRecoveryAllowance";
import { CodingCheckRecovery } from "./CodingCheckRecovery";
import { RunnerContinuation } from "./RunnerContinuation";
import { AgentCheckpointRecovery } from "./AgentCheckpointRecovery";
import { RunGitDeliveries } from "./RunGitDeliveries";
import type { IdentitySession } from "./Identity";
import { asRecord } from "./ExecutionFormat";
import {
  activeVerificationRequest,
  VerificationHandoff,
} from "./VerificationHandoff";

type Journal = {
  valid: boolean;
  entries: {
    sequence: number;
    task_id: string;
    kind: string;
    created_at: string;
    digest: string;
    previous_hash?: string;
    idempotency_key?: string;
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
  const unknownOutcome =
    run.pause?.kind === "provider_outcome_unknown" ||
    (run.agent === "bridge" && !!run.bridge_completion_hold);
  async function resume(handoff: boolean) {
    if (unknownOutcome || run.worker_request?.status === "pending") return;
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
  if (activeVerificationRequest(run)) return null;
  if (
    !unknownOutcome &&
    !["paused", "needs_reconciliation", "interrupted"].includes(run.status)
  )
    return null;
  if (unknownOutcome)
    return (
      <section
        className="continuation-panel"
        aria-label="Review the original Bridge execution"
      >
        <div>
          <h2>Execution result needs review</h2>
          <p>
            Bridge may have completed the original request. Open the Plan tab
            and review Original model result to check the saved response and any
            existing continuation request.
          </p>
          <p>
            Resume and provider handoff are held to prevent duplicate work.
            Automatic recovery of that Bridge job is not available here.
          </p>
          <small>
            The workspace, original intent and recorded results remain attached
            to this run.
          </small>
        </div>
      </section>
    );
  if (run.worker_request?.status === "pending")
    return (
      <section
        className="continuation-panel"
        aria-label="Waiting for your answer"
      >
        <div>
          <h2>Waiting for your answer</h2>
          <p>
            The agent needs your decision. Read the context and options, then
            send your answer before resuming the run.
          </p>
        </div>
        <button
          className="secondary"
          onClick={() => {
            document.getElementById("tab-questions")?.click();
            document.getElementById("tab-questions")?.focus();
          }}
        >
          Open agent questions
        </button>
      </section>
    );
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
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession | null;
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
      {run.verification_handoff !== undefined && (
        <VerificationHandoff value={run.verification_handoff} />
      )}
      {!activeVerificationRequest(run) &&
        run.worker_request?.status === "pending" &&
        asRecord(run.verification_refusal)?.request_id ===
          run.worker_request.id && (
          <VerificationHandoff
            kind="verification_handoff_refused"
            value={run.verification_refusal}
          />
        )}
      {session && (
        <RunnerContinuation
          key={JSON.stringify([
            "runner-continuation",
            connection.base,
            connection.token,
            session.user.id,
            session.global_admin,
            session.memberships,
            run.id,
            run.repository_id,
            run.project_id,
            run.publish,
            run.status,
            run.cancel_request?.status,
          ])}
          run={run}
          connection={connection}
          session={session}
        />
      )}
      {session && (
        <AgentCheckpointRecovery
          key={JSON.stringify([
            "agent-checkpoint-recovery",
            connection.base,
            connection.token,
            session.user.id,
            session.global_admin,
            session.memberships,
            run.id,
            run.repository_id,
            run.project_id,
            run.publish,
            run.status,
            run.cancel_request?.status,
          ])}
          run={run}
          connection={connection}
          session={session}
        />
      )}
      {session && (
        <CodingCheckRecovery
          key={JSON.stringify([
            "coding-check-recovery",
            connection.base,
            connection.token,
            session,
            run.id,
            run.repository_id,
            run.project_id,
            run.agent,
            run.attempt,
            run.provider_recovery,
            run.active_task,
            run.status,
            run.cancel_request?.status,
            run.bridge_completion_hold,
          ])}
          run={run}
          connection={connection}
          session={session}
        />
      )}
      {session && (
        <ProviderRecoveryAllowance
          key={JSON.stringify([
            "provider-final-allowance",
            connection.base,
            connection.token,
            session.user.id,
            session.user.authentication_method,
            session.global_admin,
            session.memberships,
            run.id,
            run.repository_id,
            run.project_id,
            run.agent,
            run.attempt,
            run.provider_recovery,
            run.active_task,
            run.status,
            run.cancel_request?.status,
            run.publish,
            run.bridge_completion_hold,
          ])}
          run={run}
          connection={connection}
          session={session}
        />
      )}
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
            <div className="record-task-heading">
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
              <EvidenceView value={task.evidence} title="Recorded result" />
            )}
          </li>
        ))}
      </ol>
      {session && (run.git_delivery_policy || run.git_delivery_summary) && (
        <RunGitDeliveries
          key={JSON.stringify([
            connection.base,
            connection.token,
            session.user.id,
            session.global_admin,
            session.memberships,
            run.id,
          ])}
          connection={connection}
          runId={run.id}
        />
      )}
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
          <h2 id={`run-journal-${run.id}`} tabIndex={-1}>
            Execution journal
          </h2>
          {journal && (
            <span className={"badge " + (journal.valid ? "success" : "failed")}>
              {journal.valid
                ? "Chain verified by server"
                : "Chain verification failed"}
            </span>
          )}
        </div>
        <p className="record-ledger-integrity">
          Ordered write-ahead intents and receipts. Chain verification checks
          record integrity; a receipt records the outcome separately.
        </p>
        {journal && !journal.entries.length && (
          <p className="subtle">No journal records have been reported.</p>
        )}
        <ol className="journal-entries">
          {journal?.entries?.map((entry) => (
            <li key={entry.sequence}>
              <div className="record-ledger-heading">
                <span
                  className="record-sequence"
                  aria-label={`Sequence ${entry.sequence}`}
                >
                  {entry.sequence}
                </span>
                <strong>
                  {plan?.tasks.find((task) => task.id === entry.task_id)
                    ?.title ?? entry.task_id}
                </strong>
                <span className="badge">{entry.kind.replaceAll("_", " ")}</span>
              </div>
              <div className="record-ledger-meta">
                <time dateTime={entry.created_at}>
                  {new Date(entry.created_at).toLocaleString()}
                </time>
                {journalDisposition(entry, journal.entries) && (
                  <span className="record-outcome">
                    {journalDisposition(entry, journal.entries)}
                  </span>
                )}
              </div>
              {[
                "tool_intent",
                "tool_receipt",
                "tool_unknown",
                "tool_reconciled",
              ].includes(entry.kind) ? (
                <ToolEvidence
                  kind={entry.kind}
                  value={entry.payload}
                  disposition={journalDisposition(entry, journal.entries)}
                />
              ) : (
                <EvidenceView kind={entry.kind} value={entry.payload} />
              )}
              <div className="record-ledger-hash">
                <span>Record hash</span>
                <code>{entry.digest}</code>
                {entry.previous_hash && (
                  <>
                    <span>Previous hash</span>
                    <code>{entry.previous_hash}</code>
                  </>
                )}
                {entry.idempotency_key && (
                  <>
                    <span>Intent key</span>
                    <code>{entry.idempotency_key}</code>
                  </>
                )}
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
