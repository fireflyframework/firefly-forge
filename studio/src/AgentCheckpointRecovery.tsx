/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { Check, Clock3, RefreshCw, ShieldCheck } from "lucide-react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ForgeRun,
} from "./api";
import type { IdentitySession } from "./Identity";
import {
  candidatePage,
  discoveredReceipt,
  markerKey,
  observationReceipt,
  readMarker,
  restoreBody,
  restoreReceipt,
  sendOnce,
  type Candidate,
  type RecoveryContext,
  type RecoveryMarker,
} from "./AgentCheckpointRecoveryData";
import "./AgentCheckpointRecovery.css";
const labels: Record<string, string> = {
  ready: "Original result saved",
  awaiting_result: "Original result needs checking",
  held: "Held for reconciliation",
  legacy: "Earlier checkpoint format",
  expired: "Original deadline passed",
  consumed: "Original result already used",
  ticket_recorded: "Continuation already recorded",
  queued: "Continuation queued",
  executing: "Original implementation continuing",
  completed: "Original implementation receipt recorded",
  failed: "Continuation failed",
  needs_reconciliation: "Continuation needs reconciliation",
  pending: "Model result pending",
  paused: "Model result paused",
  terminal: "Original result check recorded",
  result_unavailable: "Original result unavailable",
  no_candidate: "No eligible original checkpoint is saved",
  evidence_unavailable: "Saved checkpoint evidence cannot be verified",
};
export function AgentCheckpointRecovery({
  run,
  connection,
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
}) {
  const context: RecoveryContext = {
    base: new URL(
      normalizeBase(connection.base),
      typeof window === "undefined"
        ? "https://localhost"
        : window.location.href,
    ).href,
    actor: session.user.id,
    project: run.project_id ?? null,
    repository: run.repository_id,
    run: run.id,
  };
  const [saved] = useState(() => {
    try {
      return {
        marker: readMarker(sessionStorage.getItem(markerKey(context)), context),
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<RecoveryMarker | null>(saved.marker),
    [blocked, setBlocked] = useState(saved.blocked),
    [items, setItems] = useState<Candidate[]>([]),
    [selected, setSelected] = useState(saved.marker?.completion ?? ""),
    [state, setState] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [review, setReview] = useState(false),
    [ack, setAck] = useState(false),
    [confirmed, setConfirmed] = useState(false),
    [receipt, setReceipt] = useState<{
      kind: "observation" | "restore";
      status: string;
      request: string;
      completion: string;
    } | null>(null);
  const live = useRef(true),
    inflight = useRef(false),
    active = useRef<AbortController | null>(null),
    reviewHeading = useRef<HTMLHeadingElement | null>(null),
    reviewOpener = useRef<HTMLButtonElement | null>(null),
    panel = useRef<HTMLElement | null>(null),
    reviewed = useRef(false);
  const permissions =
    session.memberships.find((m) => m.project_id === context.project)
      ?.permissions ?? [];
  const allowed =
    !!context.project &&
    (session.global_admin ||
      (permissions.includes("execute") &&
        (!run.publish || permissions.includes("release"))));
  const held =
    ["needs_reconciliation", "interrupted"].includes(run.status) &&
    run.cancel_request?.status !== "requested";
  const root = `/runs/${run.id}`,
    candidate = items.find((c) => c.completion_id === selected) ?? null;
  const confirmedCurrent =
    confirmed &&
    marker?.request === receipt?.request &&
    marker?.completion === receipt?.completion;
  const completedPrevious =
    marker?.kind === "restore" &&
    confirmedCurrent &&
    receipt?.status === "completed";
  const canReplace =
    confirmedCurrent &&
    (marker?.kind === "observation" ||
      (completedPrevious && candidate?.completion_id !== marker.completion));
  const canCheck =
    allowed &&
    held &&
    !blocked &&
    candidate?.state === "awaiting_result" &&
    (!marker ||
      (canReplace &&
        receipt?.kind === "observation" &&
        ["pending", "paused"].includes(receipt.status)) ||
      (completedPrevious && candidate?.completion_id !== marker?.completion));
  const canContinue =
    allowed &&
    held &&
    !blocked &&
    candidate?.state === "ready" &&
    candidate.result !== null &&
    (!marker || canReplace);
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
      reviewHeading.current?.focus();
    } else if (reviewed.current) {
      (reviewOpener.current ?? panel.current)?.focus();
      reviewed.current = false;
    }
  }, [review]);
  function failure(cause: unknown) {
    if (!live.current) return;
    setConfirmed(false);
    setItems([]);
    setReceipt(null);
    setReview(false);
    setAck(false);
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setState("");
      setBlocked(true);
      setError(
        "Current access could not be confirmed. Private result details are cleared; the saved request stays held for authorized readback.",
      );
    } else if (cause instanceof ApiError && cause.status === 404)
      setError(
        "No exact receipt is available yet. This does not prove the request was not accepted. Keep its saved identity and check again.",
      );
    else
      setError(
        "The original result could not be confirmed. Existing request identities stay held; use exact readback for any saved request.",
      );
  }
  async function discover(signal: AbortSignal) {
    const page = candidatePage(
      await request<unknown>(
        connection,
        root + "/agent-checkpoints/candidates",
        { signal },
      ),
      context,
    );
    if (!live.current || signal.aborted) return;
    setReview(false);
    setAck(false);
    setItems(page.items);
    setState(page.state);
    setSelected((value) => value || page.items[0]?.completion_id || "");
  }
  async function work(action: (signal: AbortSignal) => Promise<void>) {
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    active.current = controller;
    try {
      await action(controller.signal);
    } catch (cause) {
      if (!controller.signal.aborted) failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function refresh() {
    await work(discover);
  }
  useEffect(() => {
    if (!allowed || run.agent !== "bridge") return;
    void work(discover);
    return () => active.current?.abort(); // The parent keys this surface by the complete authenticated context.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function exact(recorded = false) {
    await work(async (signal) => {
      if (marker && !recorded) {
        const prefix =
          root +
          (marker.kind === "restore"
            ? `/agent-checkpoints/${marker.completion}/restores`
            : `/bridge-requests/${marker.completion}/observations`);
        const raw = await request<unknown>(
          connection,
          prefix + `/${marker.request}`,
          { signal },
        );
        const status =
          marker.kind === "restore"
            ? restoreReceipt(raw, marker)
            : observationReceipt(raw, marker);
        if (!live.current || signal.aborted) return;
        setReceipt({
          kind: marker.kind,
          status,
          request: marker.request,
          completion: marker.completion,
        });
        setConfirmed(true);
      } else if (candidate?.ticket) {
        const raw = await request<unknown>(
          connection,
          root +
            `/agent-checkpoints/${candidate.completion_id}/restores/${candidate.ticket.id}`,
          { signal },
        );
        const status = discoveredReceipt(raw, candidate, context);
        if (!live.current || signal.aborted) return;
        setReceipt({
          kind: "restore",
          status,
          request: candidate.ticket.id,
          completion: candidate.completion_id,
        });
      }
      await discover(signal);
    });
  }
  async function submit(kind: "observation" | "restore") {
    if (
      !candidate ||
      (kind === "observation" ? !canCheck : !canContinue) ||
      (kind === "restore" && !ack)
    )
      return;
    await work(async (signal) => {
      const next: RecoveryMarker = {
        version: 1,
        context,
        completion: candidate.completion_id,
        checkpoint: candidate.checkpoint_digest,
        intent: candidate.intent_digest,
        kind,
        request: crypto.randomUUID().replaceAll("-", ""),
        source: kind === "restore" ? candidate.result : null,
      };
      let retained = false;
      try {
        await sendOnce(
          sessionStorage,
          next,
          async () => {
            retained = true;
            setMarker(next);
            setConfirmed(false);
            setReview(false);
            setAck(false);
            const path =
              kind === "restore"
                ? root + `/agent-checkpoints/${next.completion}/restores`
                : root + `/bridge-requests/${next.completion}/observations`;
            const body =
              kind === "restore"
                ? restoreBody(next)
                : {
                    version: 1,
                    observation_id: next.request,
                    expected_intent_digest: next.intent,
                  };
            const raw = await request<unknown>(connection, path, {
              method: "POST",
              body: JSON.stringify(body),
              signal,
            });
            const status =
              kind === "restore"
                ? restoreReceipt(raw, next)
                : observationReceipt(raw, next);
            if (!live.current || signal.aborted) return;
            setReceipt({
              kind,
              status,
              request: next.request,
              completion: next.completion,
            });
            setConfirmed(true);
          },
          canReplace && marker && receipt
            ? { marker, state: receipt.status }
            : undefined,
        );
      } catch (cause) {
        if (!retained && live.current) setBlocked(true);
        throw cause;
      }
      await discover(signal);
    });
  }
  if (run.agent !== "bridge") return null;
  return (
    <section
      className="agent-checkpoint-recovery"
      aria-label="Original model result"
      ref={panel}
      tabIndex={-1}
    >
      <div className="section-heading">
        <div>
          <h2>Original model result</h2>
          <p className="subtle">
            Keep the retained work and review the original implementation result
            before continuing.
          </p>
        </div>
        <ShieldCheck size={20} aria-hidden="true" />
      </div>
      {!allowed ? (
        <p>
          Current project execution access is required to inspect this result.
        </p>
      ) : (
        <>
          <p className="subtle">
            A held run keeps its existing work. Checking the original result
            asks Bridge for that result; continuing uses it once. Verification
            and delivery still have their own outcomes.
          </p>
          <div className="card-actions">
            <button
              type="button"
              className="secondary"
              disabled={busy || blocked}
              onClick={() => void refresh()}
            >
              <RefreshCw size={15} />
              Refresh saved result
            </button>
            {(marker || candidate?.ticket) && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void exact()}
              >
                <Clock3 size={15} />
                Read exact saved request
              </button>
            )}
          </div>
          {candidate?.ticket &&
            marker &&
            candidate.ticket.id !== marker.request && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void exact(true)}
              >
                Read recorded continuation
              </button>
            )}
          {items.length > 0 && (
            <div className="field">
              <label htmlFor={`original-result-${run.id}`}>
                Original implementation attempt
              </label>
              <select
                id={`original-result-${run.id}`}
                value={selected}
                disabled={busy || (!!marker && !completedPrevious)}
                onChange={(e) => {
                  setSelected(e.target.value);
                  if (!marker) setReceipt(null);
                  setReview(false);
                  setAck(false);
                }}
              >
                {items.map((c) => (
                  <option key={c.completion_id} value={c.completion_id}>
                    Attempt {c.attempt}
                    {c.ordinal ? `, result ${c.ordinal}` : ""}:{" "}
                    {labels[c.state]}
                  </option>
                ))}
              </select>
            </div>
          )}
          {candidate && (
            <>
              <dl className="agent-checkpoint-facts">
                <div>
                  <dt>Checkpoint saved</dt>
                  <dd>{new Date(candidate.created_at).toLocaleString()}</dd>
                </div>
                <div>
                  <dt>Original deadline</dt>
                  <dd>
                    {candidate.deadline_ms
                      ? new Date(candidate.deadline_ms).toLocaleString()
                      : "Unavailable for this earlier checkpoint"}
                  </dd>
                </div>
                <div>
                  <dt>Result state</dt>
                  <dd>{labels[candidate.state]}</dd>
                </div>
              </dl>
              {candidate.ticket && (
                <p role="status">
                  {labels[candidate.ticket.status]}. Read its exact saved
                  request to confirm the current receipt.
                </p>
              )}
            </>
          )}
          {canCheck && (
            <button
              className="secondary"
              type="button"
              disabled={busy}
              onClick={() => void submit("observation")}
            >
              Check original result
            </button>
          )}
          {canContinue && !review && (
            <button
              className="secondary"
              type="button"
              disabled={busy}
              ref={reviewOpener}
              onClick={() => {
                setReview(true);
                setAck(false);
              }}
            >
              Review original result
            </button>
          )}
          {review && canContinue && (
            <div className="agent-checkpoint-review">
              <h3 ref={reviewHeading} tabIndex={-1}>
                Review continuation
              </h3>
              <ol>
                <li>
                  Your retained work stays attached to its original
                  implementation attempt.
                </li>
                <li>
                  The saved model result continues from the original checkpoint.
                </li>
                <li>
                  Later verification or repair may succeed, fail, or need
                  reconciliation.
                </li>
              </ol>
              <label className="check-choice">
                <input
                  type="checkbox"
                  checked={ack}
                  onChange={(e) => setAck(e.target.checked)}
                />
                I understand this result is consumed once and a later failure
                may remain held.
              </label>
              <div className="card-actions">
                <button
                  type="button"
                  className="primary"
                  disabled={busy || !ack}
                  onClick={() => void submit("restore")}
                >
                  <Check size={15} />
                  Continue from original result
                </button>
                <button
                  type="button"
                  className="quiet"
                  onClick={() => {
                    setReview(false);
                    setAck(false);
                  }}
                >
                  Close review
                </button>
              </div>
            </div>
          )}
          {marker && !confirmed && (
            <p role="status">
              A request identity is saved. Use exact readback to confirm its
              outcome before taking another action.
            </p>
          )}
          {receipt && (
            <p role="status" aria-live="polite">
              {labels[receipt.status]}.
              {receipt.kind === "restore" &&
                receipt.status === "completed" &&
                " This confirms the original implementation receipt; check the run’s verification and delivery outcomes separately."}
            </p>
          )}
          {state && state !== "available" && (
            <p role="status">{labels[state]}</p>
          )}
          {blocked && (
            <p role="status">
              Request storage or access could not be verified. No new request is
              offered. Saved requests remain available for exact readback.
            </p>
          )}
          {!held && (
            <p className="subtle">
              Continuation is offered only while the original implementation is
              held and uncancelled.
            </p>
          )}
        </>
      )}
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
