/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ForgeRun,
} from "./api";
import type { IdentitySession } from "./Identity";
import {
  candidate,
  recoveryInput,
  recoveryStatus,
  readonlyRecoveryStatus,
  markerKey,
  readMarker,
  sendOnce,
  type Candidate,
  type RecoveryContext,
  type RecoveryMarker,
  type RecoveryStatus,
  type ReadonlyRecoveryStatus,
} from "./InvalidReadRecoveryData";
import {
  checkpointCandidate,
  checkpointInput,
  checkpointStatus,
  checkpointMarkerKey,
  readCheckpointMarker,
  sendCheckpointOnce,
  type CheckpointCandidate,
  type CheckpointMarker,
  type CheckpointStatus,
} from "./CheckpointContinuationData";
import "./AgentCheckpointRecovery.css";
const labels: Record<string, string> = {
  queued: "Continuation queued",
  executing: "Worker claimed the continuation",
  completed: "Continuation completed",
  failed: "Continuation failed",
  needs_reconciliation: "Continuation needs reconciliation",
};
const readonlyLabels: Record<string, [string, string]> = {
  queued: [
    "Continuation queued",
    "The saved continuation is waiting for a worker. Execution has not started.",
  ],
  executing: [
    "Continuation in progress",
    "A worker has claimed the saved continuation. Check the execution record for its results.",
  ],
  completed: [
    "Continuation completed",
    "The worker reported completion. Check the execution record for the retained results.",
  ],
  failed: [
    "Continuation failed",
    "The worker reported a failure. Check the execution record before deciding what to do next.",
  ],
  needs_reconciliation: [
    "Continuation needs review",
    "The saved continuation needs reconciliation. Review its execution record before taking another action.",
  ],
};
const holdLabels: Record<string, string> = {
  checkpoint_continuation_disabled:
    "Checkpoint continuation is disabled. A factory administrator can enable it in Connections.",
  current_profile_unqualified:
    "The installed source needs current qualification.",
  lease_policy_disabled: "The finite continuity lease policy is disabled.",
  unavailable_observation_policy_disabled:
    "The source observation policy is disabled.",
  active_execution_budget_expired: "The remaining execution time has expired.",
};
type Props = {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
};
export function InvalidReadRecovery(props: Props) {
  const value = props.run.invalid_read_continuity,
    requestId =
      value && typeof value === "object" && !Array.isArray(value)
        ? (value as Record<string, unknown>).request_id
        : null;
  if (typeof requestId !== "string" || !/^[a-f0-9]{32}$/.test(requestId))
    return null;
  return <Recovery {...props} requestId={requestId} />;
}
function Recovery({
  run,
  connection,
  session,
  requestId,
}: Props & { requestId: string }) {
  const context: RecoveryContext = {
    base: new URL(
      normalizeBase(connection.base),
      typeof window === "undefined"
        ? "https://localhost"
        : window.location.href,
    ).href,
    actor: session.user.id,
    authentication: session.user.authentication_method,
    project: run.project_id ?? null,
    repository: run.repository_id,
    run: run.id,
    request: requestId,
  };
  const [saved] = useState(() => {
    try {
      return {
        marker: readMarker(localStorage.getItem(markerKey(context)), context),
        checkpoint: readCheckpointMarker(
          localStorage.getItem(checkpointMarkerKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { marker: null, checkpoint: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<RecoveryMarker | null>(saved.marker),
    [checkpointMarker, setCheckpointMarker] = useState<CheckpointMarker | null>(
      saved.checkpoint,
    ),
    [checkpointCurrent, setCheckpointCurrent] =
      useState<CheckpointCandidate | null>(null),
    [checkpointReceipt, setCheckpointReceipt] =
      useState<CheckpointStatus | null>(null),
    [seconds, setSeconds] = useState(""),
    [blocked, setBlocked] = useState(saved.blocked),
    [current, setCurrent] = useState<Candidate | null>(null),
    [receipt, setReceipt] = useState<RecoveryStatus | null>(null),
    [readonlyReceipt, setReadonlyReceipt] =
      useState<ReadonlyRecoveryStatus | null>(null),
    [busy, setBusy] = useState(false),
    [review, setReview] = useState(false),
    [acks, setAcks] = useState([false, false, false, false]),
    [error, setError] = useState("");
  const live = useRef(true),
    active = useRef<AbortController | null>(null),
    inflight = useRef(false),
    heading = useRef<HTMLHeadingElement | null>(null),
    receiptHeading = useRef<HTMLHeadingElement | null>(null),
    opener = useRef<HTMLButtonElement | null>(null),
    reviewed = useRef(false);
  const allowed =
    !!context.project &&
    session.user.authentication_method !== "scm_mapping" &&
    session.global_admin &&
    run.cancel_request?.status !== "requested";
  const root = `/runs/${run.id}/invalid-read-continuity/requests/${requestId}`;
  const checkpointRoot = `/runs/${run.id}/checkpoint-continuations/requests/${requestId}`;
  const checkpointEligible =
    allowed &&
    !blocked &&
    !checkpointMarker &&
    checkpointCurrent?.admission === "READY";
  const validSeconds =
    !!checkpointCurrent &&
    /^\d+$/.test(seconds) &&
    Number.isSafeInteger(Number(seconds)) &&
    Number(seconds) >= 1 &&
    Number(seconds) <= checkpointCurrent.maximum_window_seconds;
  const eligible =
    allowed &&
    !blocked &&
    !marker &&
    !checkpointMarker &&
    current?.admission === "READY" &&
    receipt?.status === "not_recovered";
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (review) {
      reviewed.current = true;
      heading.current?.focus();
    } else if (reviewed.current) {
      opener.current?.focus();
      reviewed.current = false;
    }
  }, [review]);
  useEffect(() => {
    if (
      checkpointReceipt ||
      readonlyReceipt ||
      (receipt && receipt.status !== "not_recovered")
    )
      receiptHeading.current?.focus();
  }, [receipt, readonlyReceipt, checkpointReceipt]);
  function failure(cause: unknown) {
    if (!live.current) return;
    setCheckpointCurrent(null);
    setCheckpointReceipt(null);
    setCurrent(null);
    setReceipt(null);
    setReadonlyReceipt(null);
    setReview(false);
    setAcks([false, false, false, false]);
    setError(
      cause instanceof ApiError &&
        (cause.status === 401 || cause.status === 403)
        ? "Current authority could not be verified. Private recovery details are hidden. Any saved action remains held."
        : marker || checkpointMarker
          ? "The saved action has not been confirmed. Check its receipt; do not submit another recovery."
          : "The saved continuation status could not be verified. Check again before taking another action.",
    );
  }
  async function inspect() {
    if (inflight.current || !allowed || blocked) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    setCheckpointCurrent(null);
    setCheckpointReceipt(null);
    setCurrent(null);
    setReceipt(null);
    setReadonlyReceipt(null);
    setReview(false);
    setAcks([false, false, false, false]);
    const controller = new AbortController();
    active.current = controller;
    try {
      const retained = checkpointStatus(
        await request<unknown>(connection, `${checkpointRoot}/status`, {
          signal: controller.signal,
        }),
        context,
        checkpointMarker?.action,
        checkpointMarker?.seal ?? marker?.body.expected_admission_seal,
      );
      if (!live.current || controller.signal.aborted) return;
      if (retained.status !== "not_recovered") {
        setCheckpointReceipt(retained);
        return;
      }
      try {
        const next = checkpointCandidate(
          await request<unknown>(connection, `${checkpointRoot}/candidate`, {
            signal: controller.signal,
          }),
          context,
          retained.original_admission_seal,
        );
        if (!live.current || controller.signal.aborted) return;
        setCheckpointCurrent(next);
        setSeconds(String(next.maximum_window_seconds));
        return;
      } catch (cause) {
        if (
          !(cause instanceof ApiError) ||
          cause.status !== 409 ||
          checkpointMarker
        )
          throw cause;
      }
      const observation = readonlyRecoveryStatus(
        await request<unknown>(
          connection,
          `${root}/readonly-observation-recovery`,
          {
            signal: controller.signal,
          },
        ),
        context,
        retained.original_admission_seal,
      );
      if (!live.current || controller.signal.aborted) return;
      if (observation.status !== "not_recovered") {
        setReadonlyReceipt(observation);
        return;
      }
      const value = await request<unknown>(connection, `${root}/recovery`, {
        signal: controller.signal,
      });
      const s = recoveryStatus(
        value,
        context,
        marker?.recovery,
        marker?.body.expected_admission_seal,
      );
      if (s.original_admission_seal !== observation.original_admission_seal)
        throw Error("Changed admission");
      if (!live.current) return;
      setReceipt(s);
      if (!marker && s.status === "not_recovered") {
        const c = candidate(
          await request<unknown>(connection, `${root}/recovery-candidate`, {
            signal: controller.signal,
          }),
          context,
        );
        if (!live.current) return;
        if (c.original_admission_seal !== s.original_admission_seal)
          throw Error("Changed admission");
        setCurrent(c);
      }
    } catch (cause) {
      if (!controller.signal.aborted) failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function submitCheckpoint() {
    if (
      inflight.current ||
      !checkpointEligible ||
      !checkpointCurrent ||
      !validSeconds ||
      !acks.every(Boolean)
    )
      return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const next: CheckpointMarker = {
      version: 1,
      context,
      action: crypto.randomUUID().replaceAll("-", ""),
      seal: checkpointCurrent.original_admission_seal,
      body: checkpointInput(checkpointCurrent, Number(seconds)),
    };
    setCheckpointMarker(next);
    setCheckpointCurrent(null);
    setReview(false);
    setAcks([false, false, false, false]);
    try {
      if (!navigator.locks) throw Error("Exclusive browser lock unavailable");
      await navigator.locks.request(
        checkpointMarkerKey(context),
        { mode: "exclusive" },
        async () => {
          const value = await sendCheckpointOnce(localStorage, next, () =>
            request<unknown>(
              connection,
              `${checkpointRoot}/actions/${next.action}`,
              { method: "POST", body: JSON.stringify(next.body) },
            ),
          );
          const result = checkpointStatus(
            value,
            context,
            next.action,
            next.seal,
          );
          if (live.current) setCheckpointReceipt(result);
        },
      );
    } catch {
      if (live.current) {
        setCheckpointReceipt(null);
        setError(
          "The saved continuation has not been confirmed. Check its status; do not submit another action.",
        );
        try {
          const stored = readCheckpointMarker(
            localStorage.getItem(checkpointMarkerKey(context)),
            context,
          );
          if (stored) setCheckpointMarker(stored);
        } catch {
          setBlocked(true);
        }
      }
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function submit() {
    if (inflight.current || !eligible || !current || !acks.every(Boolean))
      return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const next: RecoveryMarker = {
      version: 1,
      context,
      recovery: crypto.randomUUID().replaceAll("-", ""),
      body: recoveryInput(current),
    };
    setMarker(next);
    setCurrent(null);
    setReview(false);
    setAcks([false, false, false, false]);
    try {
      if (!navigator.locks) throw Error("Exclusive browser lock unavailable");
      await navigator.locks.request(
        markerKey(context),
        { mode: "exclusive" },
        async () => {
          const value = await sendOnce(localStorage, next, () =>
            request<unknown>(
              connection,
              `${root}/recoveries/${next.recovery}`,
              { method: "POST", body: JSON.stringify(next.body) },
            ),
          );
          const s = recoveryStatus(
            value,
            context,
            next.recovery,
            next.body.expected_admission_seal,
          );
          if (live.current) setReceipt(s);
        },
      );
    } catch {
      if (live.current) {
        setReceipt(null);
        setError(
          "The saved action has not been confirmed. Check its receipt; do not submit another recovery.",
        );
        try {
          const retained = readMarker(
            localStorage.getItem(markerKey(context)),
            context,
          );
          if (retained) setMarker(retained);
        } catch {
          setBlocked(true);
        }
      }
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  return (
    <section
      className="agent-checkpoint-recovery"
      aria-label="Saved continuation recovery"
    >
      <h3>Saved continuation</h3>
      <p>
        Check the saved continuation and its worker receipts. Checking status
        does not start work.
      </p>
      {!allowed ? (
        <p>
          Current execution access as a factory administrator, the original
          execution actor, and an uncancelled run are required to inspect
          recovery.
        </p>
      ) : blocked ? (
        <p role="alert">
          The saved action identity cannot be verified in this browser. Recovery
          is held.
        </p>
      ) : (
        <>
          <div className="card-actions">
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => void inspect()}
            >
              {busy
                ? "Checking…"
                : checkpointMarker
                  ? "Check saved continuation"
                  : marker
                    ? "Check saved recovery receipt"
                    : "Check current admission"}
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
          {checkpointReceipt && (
            <div role="status" className="checkpoint-continuation-result">
              <h4 ref={receiptHeading} tabIndex={-1}>
                {readonlyLabels[checkpointReceipt.status][0]}
              </h4>
              <p>{readonlyLabels[checkpointReceipt.status][1]}</p>
              <p>
                Completed changes stay in place. The saved response is used
                once, and existing usage and limits stay attached to the same
                task.
              </p>
              <details>
                <summary>Advanced record</summary>
                <pre>{JSON.stringify(checkpointReceipt, null, 2)}</pre>
              </details>
            </div>
          )}
          {checkpointCurrent && (
            <div className="checkpoint-continuation-review">
              {!review ? (
                <>
                  <h4>Continue saved work</h4>
                  <p>
                    A model response is saved and waiting to be used. Completed
                    changes stay in place, with their original results and
                    charged usage.
                  </p>
                  <dl className="agent-checkpoint-facts">
                    <div>
                      <dt>Existing attempt</dt>
                      <dd>{checkpointCurrent.attempt}</dd>
                    </div>
                    <div>
                      <dt>Model requests used</dt>
                      <dd>
                        {checkpointCurrent.usage.requests}
                        {checkpointCurrent.limits.request_limit !== null
                          ? ` of ${checkpointCurrent.limits.request_limit}`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>Maximum time window</dt>
                      <dd>
                        {checkpointCurrent.maximum_window_seconds} seconds
                      </dd>
                    </div>
                  </dl>
                  {checkpointCurrent.admission === "HOLD" ? (
                    <div role="status">
                      <h4>Continuation held</h4>
                      {checkpointCurrent.holds.map((hold) => (
                        <p key={hold}>{holdLabels[hold]}</p>
                      ))}
                    </div>
                  ) : (
                    <button
                      ref={opener}
                      type="button"
                      className="secondary"
                      onClick={() => {
                        setReview(true);
                        setAcks([false, false, false, false]);
                      }}
                    >
                      Review saved continuation
                    </button>
                  )}
                </>
              ) : (
                <div className="agent-checkpoint-review">
                  <h4 ref={heading} tabIndex={-1}>
                    Review saved continuation
                  </h4>
                  <p>
                    Continue the same task using its saved response once.
                    Completed changes stay in place. Model, tool and command
                    usage remains charged against the existing limits.
                  </p>
                  <div className="field">
                    <label htmlFor={`checkpoint-window-${run.id}`}>
                      Time window (seconds)
                    </label>
                    <input
                      id={`checkpoint-window-${run.id}`}
                      type="number"
                      min={1}
                      max={checkpointCurrent.maximum_window_seconds}
                      step={1}
                      value={seconds}
                      onChange={(event) => {
                        setSeconds(event.target.value);
                        setAcks((values) =>
                          values.map((v, i) => (i === 3 ? false : v)),
                        );
                      }}
                      aria-describedby={`checkpoint-window-help-${run.id}`}
                    />
                    <small id={`checkpoint-window-help-${run.id}`}>
                      Choose up to {checkpointCurrent.maximum_window_seconds}{" "}
                      seconds for the worker to continue. Existing execution
                      limits still apply.
                    </small>
                  </div>
                  {[
                    "Use the saved response once.",
                    "Keep completed changes and tool results without repeating them.",
                    "Continue the same request and attempt with existing usage and limits.",
                    `Allow the worker up to ${validSeconds ? seconds : "the reviewed number of"} seconds.`,
                  ].map((label, index) => (
                    <label className="check-choice" key={index}>
                      <input
                        type="checkbox"
                        checked={acks[index]}
                        onChange={(event) =>
                          setAcks((values) =>
                            values.map((v, i) =>
                              i === index ? event.target.checked : v,
                            ),
                          )
                        }
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                  <div className="card-actions">
                    <button
                      type="button"
                      className="quiet"
                      onClick={() => {
                        setReview(false);
                        setAcks([false, false, false, false]);
                      }}
                    >
                      Back
                    </button>
                    <button
                      type="button"
                      className="primary"
                      disabled={
                        !checkpointEligible ||
                        !validSeconds ||
                        !acks.every(Boolean) ||
                        busy
                      }
                      onClick={() => void submitCheckpoint()}
                    >
                      Continue saved task
                    </button>
                  </div>
                </div>
              )}
              <details>
                <summary>Advanced record</summary>
                <pre>{JSON.stringify(checkpointCurrent, null, 2)}</pre>
              </details>
            </div>
          )}
          {readonlyReceipt && (
            <div role="status">
              <h4 ref={receiptHeading} tabIndex={-1}>
                {readonlyLabels[readonlyReceipt.status][0]}
              </h4>
              <p>Unavailable read recovery</p>
              <p>{readonlyLabels[readonlyReceipt.status][1]}</p>
              <p>
                The original read remains unavailable. Previously recorded
                results are reused, and existing consumption and budgets stay
                charged.
              </p>
            </div>
          )}
          {receipt && receipt.status !== "not_recovered" && (
            <div role="status">
              <h4 ref={receiptHeading} tabIndex={-1}>
                {labels[receipt.status]}
              </h4>
              <p>
                The original failed admission remains recorded. A queued receipt
                does not prove actual execution; inspect the later execution
                record for model, tool and command results.
              </p>
            </div>
          )}
          {current && (
            <>
              <h4>Continuation did not start</h4>
              <p>
                The current candidate verifies that no worker claimed this
                ticket. Recovery keeps the same request and attempt and
                preserves its failed receipt.
              </p>
              <dl className="agent-checkpoint-facts">
                <div>
                  <dt>Existing attempt</dt>
                  <dd>{current.attempt}</dd>
                </div>
                <div>
                  <dt>Permitted time window</dt>
                  <dd>{current.maximum_window_seconds} seconds</dd>
                </div>
                <div>
                  <dt>Commands already used</dt>
                  <dd>
                    {current.usage.commands}
                    {current.usage.command_limit !== null
                      ? ` of ${current.usage.command_limit}`
                      : ""}
                  </dd>
                </div>
              </dl>
              {current.admission === "HOLD" ? (
                <div role="status">
                  <h4>Recovery held</h4>
                  {current.holds.map((hold) => (
                    <p key={hold}>{holdLabels[hold]}</p>
                  ))}
                </div>
              ) : !review ? (
                <button
                  ref={opener}
                  type="button"
                  className="secondary"
                  onClick={() => {
                    setReview(true);
                    setAcks([false, false, false, false]);
                  }}
                >
                  Review failed admission
                </button>
              ) : (
                <div className="agent-checkpoint-review">
                  <h4 ref={heading} tabIndex={-1}>
                    Requeue the existing task
                  </h4>
                  <p>
                    Only the finite ownership window is renewed. No new grant,
                    provider attempt, consumed response replay or budget reset
                    is included.
                  </p>
                  {[
                    "I acknowledge that the worker never claimed this ticket.",
                    "I am continuing the same request and attempt.",
                    "Original consumption, grants and execution budgets remain unchanged.",
                    `I acknowledge a renewed same-attempt lease of ${current.maximum_window_seconds} seconds.`,
                  ].map((label, index) => (
                    <label className="check-choice" key={label}>
                      <input
                        type="checkbox"
                        checked={acks[index]}
                        onChange={(event) =>
                          setAcks((values) =>
                            values.map((value, i) =>
                              i === index ? event.target.checked : value,
                            ),
                          )
                        }
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                  <div className="card-actions">
                    <button
                      type="button"
                      className="quiet"
                      disabled={busy}
                      onClick={() => {
                        setReview(false);
                        setAcks([false, false, false, false]);
                      }}
                    >
                      Back
                    </button>
                    <button
                      className="primary"
                      type="button"
                      disabled={!eligible || busy || !acks.every(Boolean)}
                      onClick={() => void submit()}
                    >
                      Requeue existing task
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
