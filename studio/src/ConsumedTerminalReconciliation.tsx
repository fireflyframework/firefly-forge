/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { ClipboardCheck, RefreshCw } from "lucide-react";
import { ApiError, normalizeBase, type Connection, type ForgeRun } from "./api";
import type { IdentitySession } from "./Identity";
import {
  consumedTerminalRequest,
  getConsumedTerminalReconciliationCandidate,
  getConsumedTerminalReconciliationStatus,
  readReconciliationMarker,
  reconcileConsumedTerminal,
  reconciliationKey,
  sendReconciliationOnce,
  type ReconciliationCandidate,
  type ReconciliationContext,
  type ReconciliationMarker,
  type ReconciliationReceipt,
} from "./ConsumedTerminalData";
import "./ConsumedTerminalReconciliation.css";

export function ConsumedTerminalReconciliation({
  run,
  connection,
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
}) {
  const custody = run.consumed_terminal_recovery as
    Record<string, unknown> | undefined;
  const context: ReconciliationContext = {
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
    request: consumedTerminalRequest(run) ?? "",
    recovery:
      typeof custody?.record_digest === "string" ? custody.record_digest : "",
    claim:
      typeof custody?.claim_digest === "string" ? custody.claim_digest : "",
    attempt: run.attempt ?? 0,
  };
  const [saved] = useState(() => {
    try {
      return {
        marker: readReconciliationMarker(
          localStorage.getItem(reconciliationKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<ReconciliationMarker | null>(
      saved.marker,
    ),
    [blocked, setBlocked] = useState(saved.blocked),
    [candidate, setCandidate] = useState<ReconciliationCandidate | null>(null),
    [receipt, setReceipt] = useState<ReconciliationReceipt | null>(null),
    [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null),
    live = useRef(true),
    heading = useRef<HTMLHeadingElement | null>(null);
  const allowed =
    session.global_admin &&
    !!context.request &&
    run.cancel_request?.status !== "requested";
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      pending.current?.abort();
    };
  }, []);
  function failure(cause: unknown, submitted: boolean) {
    if (!live.current) return;
    setCandidate(null);
    setReceipt(null);
    setAcknowledged(false);
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setBlocked(true);
      setError(
        "Current access to the original action was denied. Sign in as its original operator; private candidate details are cleared.",
      );
    } else if (
      cause instanceof ApiError &&
      [400, 409, 422].includes(cause.status)
    ) {
      setError(
        "The reviewed source, revision or action has changed. Check current run evidence before continuing; no replacement action was sent.",
      );
    } else {
      setError(
        submitted || marker || blocked
          ? "The reconciliation outcome is unconfirmed. Read its receipt using the original request. No second submission will be sent."
          : "The current claimed action could not be verified. Check the run evidence and try this read again.",
      );
    }
  }
  function begin() {
    if (pending.current) return null;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    setAcknowledged(false);
    return controller;
  }
  function finish(controller: AbortController) {
    if (pending.current === controller) pending.current = null;
    if (live.current) setBusy(false);
  }
  async function inspect() {
    if (!allowed) return;
    const controller = begin();
    if (!controller) return;
    try {
      let current: ReconciliationReceipt | null = null;
      try {
        current = await getConsumedTerminalReconciliationStatus(
          connection,
          context,
          marker,
          controller.signal,
        );
      } catch (cause) {
        if (!(
          cause instanceof ApiError &&
          cause.status === 404 &&
          !marker &&
          !blocked
        ))
          throw cause;
      }
      if (!live.current || controller.signal.aborted) return;
      if (current) {
        setReceipt(current);
        setCandidate(null);
      } else {
        const found = await getConsumedTerminalReconciliationCandidate(
          connection,
          context,
          controller.signal,
        );
        if (!live.current || controller.signal.aborted) return;
        setCandidate(found);
        setReceipt(null);
      }
      requestAnimationFrame(() => heading.current?.focus());
    } catch (cause) {
      if (!controller.signal.aborted) failure(cause, false);
    } finally {
      finish(controller);
    }
  }
  async function reconcile() {
    if (!allowed || blocked || marker || !candidate || !acknowledged) return;
    const controller = begin();
    if (!controller) return;
    const proposed: ReconciliationMarker = {
      version: 1,
      context,
      candidate_digest: candidate.digest,
      actor_digest: candidate.actor_digest,
    };
    try {
      const result = await sendReconciliationOnce(
        localStorage,
        proposed,
        () => {
          setMarker(proposed);
          return reconcileConsumedTerminal(
            connection,
            context,
            candidate,
            controller.signal,
          );
        },
      );
      if (!live.current || controller.signal.aborted) return;
      setReceipt(result);
      setCandidate(null);
      requestAnimationFrame(() => heading.current?.focus());
    } catch (cause) {
      // An inaccessible or changed browser marker must also prevent another POST.
      if (!controller.signal.aborted) {
        setBlocked(true);
        failure(cause, true);
      }
    } finally {
      finish(controller);
    }
  }
  const evidence = receipt?.candidate ?? candidate;
  const revision =
    run.source_tree ?? run.checkpoint_revision ?? run.base_revision;
  return (
    <section
      className="consumed-terminal-reconciliation"
      aria-label="Claimed result reconciliation"
      aria-busy={busy}
    >
      <div className="reconciliation-heading">
        <ClipboardCheck size={22} aria-hidden="true" />
        <h3>Reconcile the saved result</h3>
      </div>
      <p>
        The result was already consumed by this attempt. Complete its original
        handoff through local postprocessing, without another AI request or
        replaying tools.
      </p>
      {!allowed ? (
        <p>
          The original operator with current administrator access and an
          uncancelled run is required to inspect this action.
        </p>
      ) : (
        <>
          {blocked && (
            <p role="status">
              New submissions remain held. The original action identity is
              preserved; you can still check its receipt.
            </p>
          )}
          <div className="card-actions">
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => void inspect()}
            >
              <RefreshCw size={16} aria-hidden="true" />
              {busy
                ? "Checking original action…"
                : marker || blocked || receipt
                  ? "Read reconciliation receipt"
                  : "Check claimed action"}
            </button>
          </div>
          {error && (
            <p className="reconciliation-error" role="alert">
              {error}
            </p>
          )}
          {busy && (
            <p role="status">
              Checking the original action. Keep this run open while its current
              record is confirmed.
            </p>
          )}
          {evidence && (
            <div className="reconciliation-review">
              <h4 ref={heading} tabIndex={-1}>
                {receipt
                  ? receipt.settlement.state === "accepted"
                    ? "Saved result accepted"
                    : "Reconciliation recorded"
                  : "Review the original action"}
              </h4>
              {receipt && (
                <p role="status">
                  {receipt.settlement.state === "accepted"
                    ? "The original result was accepted. Review the plan and verification evidence for the run’s outcome."
                    : "The original action remains claimed. Check its receipt for progress; another submission is held."}{" "}
                  Spent usage remains attached to this attempt.
                </p>
              )}
              <dl className="reconciliation-facts">
                <div>
                  <dt>Task attempt</dt>
                  <dd>Implementation · {evidence.same_task_attempt}</dd>
                </div>
                <div>
                  <dt>Source fingerprint</dt>
                  <dd>
                    <code>{evidence.source_digest.slice(0, 12)}</code>
                  </dd>
                </div>
                <div>
                  <dt>Recorded revision</dt>
                  <dd>
                    {revision ? (
                      <code>{revision.slice(0, 12)}</code>
                    ) : (
                      "See run evidence"
                    )}
                  </dd>
                </div>
              </dl>
              {!receipt && !marker && !blocked && (
                <>
                  <label className="check-choice">
                    <input
                      type="checkbox"
                      checked={acknowledged}
                      disabled={busy}
                      onChange={(event) =>
                        setAcknowledged(event.target.checked)
                      }
                    />
                    Use the same claimed result for local postprocessing,
                    preserve spent usage, and grant no new AI or tool execution.
                  </label>
                  <button
                    className="primary"
                    type="button"
                    disabled={busy || !acknowledged}
                    onClick={() => void reconcile()}
                  >
                    Reconcile saved result
                  </button>
                </>
              )}
              <details>
                <summary>Technical evidence</summary>
                <dl className="reconciliation-technical">
                  {Object.entries({
                    "Original request": context.request,
                    Candidate: evidence.digest,
                    "Original recovery": evidence.recovery_digest,
                    Claim: evidence.claim_digest,
                    Runtime: evidence.runtime_contract,
                    Profile: evidence.profile_digest,
                    ...(revision ? { "Recorded revision": revision } : {}),
                    ...(receipt
                      ? { "Reconciliation receipt": receipt.record_digest }
                      : {}),
                  }).map(([label, value]) => (
                    <div key={label}>
                      <dt>{label}</dt>
                      <dd>
                        <code>{value}</code>
                      </dd>
                    </div>
                  ))}
                </dl>
              </details>
            </div>
          )}
        </>
      )}
      <p className="muted">
        Reconciliation keeps the original request and attempt. It does not
        establish implementation success or publish a CRM change.
      </p>
    </section>
  );
}
