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
  markerKey,
  readMarker,
  sendOnce,
  type Candidate,
  type RecoveryContext,
  type RecoveryMarker,
  type RecoveryStatus,
} from "./InvalidReadRecoveryData";
import "./AgentCheckpointRecovery.css";
const labels: Record<string, string> = {
  queued: "Continuation queued",
  executing: "Worker claimed the continuation",
  completed: "Continuation completed",
  failed: "Continuation failed",
  needs_reconciliation: "Continuation needs reconciliation",
};
const holdLabels: Record<string, string> = {
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
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<RecoveryMarker | null>(saved.marker),
    [blocked, setBlocked] = useState(saved.blocked),
    [current, setCurrent] = useState<Candidate | null>(null),
    [receipt, setReceipt] = useState<RecoveryStatus | null>(null),
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
  const eligible =
    allowed &&
    !blocked &&
    !marker &&
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
    if (receipt && receipt.status !== "not_recovered")
      receiptHeading.current?.focus();
  }, [receipt]);
  function failure(cause: unknown) {
    if (!live.current) return;
    setCurrent(null);
    setReceipt(null);
    setReview(false);
    setAcks([false, false, false, false]);
    setError(
      cause instanceof ApiError &&
        (cause.status === 401 || cause.status === 403)
        ? "Current authority could not be verified. Private recovery details are hidden. Any saved action remains held."
        : marker
          ? "The saved action has not been confirmed. Check its receipt; do not submit another recovery."
          : "This admission cannot currently be verified as an unclaimed failure. No task was requeued.",
    );
  }
  async function inspect() {
    if (inflight.current || !allowed || blocked) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    setCurrent(null);
    setReceipt(null);
    setReview(false);
    setAcks([false, false, false, false]);
    const controller = new AbortController();
    active.current = controller;
    try {
      const value = await request<unknown>(connection, `${root}/recovery`, {
        signal: controller.signal,
      });
      const s = recoveryStatus(
        value,
        context,
        marker?.recovery,
        marker?.body.expected_admission_seal,
      );
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
      failure(cause);
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
        Admission records a queued task. A worker must still claim it before any
        model, tool or command can run.
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
                : marker
                  ? "Check saved recovery receipt"
                  : "Check current admission"}
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
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
