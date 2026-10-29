/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { CheckCircle2, ClipboardCheck, Clock3, RefreshCw } from "lucide-react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ForgeRun,
} from "./api";
import type { IdentitySession } from "./Identity";
import type { RecoveryContext } from "./InvalidReadRecoveryData";
import {
  finalCandidate,
  finalInput,
  finalStatus,
  finalMarkerKey,
  readFinalMarker,
  sendFinalOnce,
  type FinalCandidate,
  type FinalMarker,
  type FinalStatus,
} from "./FinalOutcomeData";
import "./AgentCheckpointRecovery.css";
import "./FinalOutcomeRecovery.css";
const holds: Record<string, string> = {
  lease_policy_disabled: "The factory continuity policy is disabled.",
  final_outcome_continuation_disabled:
    "An administrator must enable saved final-result recovery in the continuity policy.",
  current_profile_unqualified:
    "The installed runtime needs current qualification before recovery can proceed.",
  active_execution_budget_expired:
    "The original execution allowance has expired. Its spent usage remains recorded.",
};
const resultLabels: Record<string, string> = {
  queued: "Waiting for a worker",
  executing: "Accepting the saved result",
  completed: "Saved result accepted",
  failed: "The continuation failed",
  needs_reconciliation: "The continuation needs review",
};
const outcomes: Record<string, string> = {
  complete:
    "The agent returned its final report. Verification still determines whether the implementation passes.",
  blocked:
    "The agent reported a blocker. Accepting the saved report preserves that blocker and its requested follow-up.",
  clarification:
    "The agent requested clarification. The run must keep waiting for an answer.",
  failed:
    "The agent reported a failure. Accepting its report records that failure without marking the work successful.",
};
export function FinalOutcomeRecovery({
  run,
  requestId,
  connection,
  session,
}: {
  run: ForgeRun;
  requestId: string;
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
    authentication: session.user.authentication_method,
    project: run.project_id ?? null,
    repository: run.repository_id,
    run: run.id,
    request: requestId,
  };
  const [saved] = useState(() => {
    try {
      return {
        marker: readFinalMarker(
          localStorage.getItem(finalMarkerKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<FinalMarker | null>(saved.marker),
    [blocked, setBlocked] = useState(saved.blocked),
    [candidate, setCandidate] = useState<FinalCandidate | null>(null),
    [status, setStatus] = useState<FinalStatus | null>(null),
    [busy, setBusy] = useState(false),
    [acknowledged, setAcknowledged] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null),
    live = useRef(true),
    heading = useRef<HTMLHeadingElement | null>(null);
  const allowed =
    session.global_admin && run.cancel_request?.status !== "requested";
  const root = `/runs/${run.id}/final-outcome-continuations/requests/${requestId}`;
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  function failure(cause: unknown) {
    if (!live.current) return;
    setCandidate(null);
    setStatus(null);
    setAcknowledged(false);
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setBlocked(true);
      setError(
        "Current access could not be verified. Private result details are cleared; the saved action identity is preserved.",
      );
    } else {
      setError(
        "The saved result or action could not be confirmed. Keep its existing identity and check again; no replacement action has been sent.",
      );
    }
  }
  function begin() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setAcknowledged(false);
    return controller;
  }
  async function inspect() {
    if (!allowed || busy) return;
    const controller = begin();
    try {
      const current = finalStatus(
        await request(connection, `${root}/status`, {
          signal: controller.signal,
        }),
        context,
        marker?.action,
        marker?.seal,
      );
      if (!live.current || controller.signal.aborted) return;
      setStatus(current);
      setCandidate(null);
      if (current.status === "not_recovered" && !marker && !blocked) {
        const found = finalCandidate(
          await request(connection, `${root}/candidate`, {
            signal: controller.signal,
          }),
          context,
        );
        if (found.original_admission_seal !== current.original_admission_seal)
          throw Error("Original admission changed");
        if (!live.current || controller.signal.aborted) return;
        setCandidate(found);
      }
      requestAnimationFrame(() => heading.current?.focus());
    } catch (cause) {
      if (!controller.signal.aborted) failure(cause);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function accept() {
    if (
      !allowed ||
      busy ||
      blocked ||
      marker ||
      !acknowledged ||
      candidate?.admission !== "READY"
    )
      return;
    const controller = begin();
    try {
      const proposed: FinalMarker = {
        version: 1,
        context,
        action: crypto.randomUUID().replaceAll("-", ""),
        seal: candidate.original_admission_seal,
        body: finalInput(candidate),
      };
      const result = await sendFinalOnce(localStorage, proposed, async () => {
        setMarker(proposed);
        return await request(connection, `${root}/actions/${proposed.action}`, {
          method: "POST",
          body: JSON.stringify(proposed.body),
          signal: controller.signal,
        });
      });
      const receipt = finalStatus(
        result,
        context,
        proposed.action,
        proposed.seal,
      );
      if (!live.current || controller.signal.aborted) return;
      setStatus(receipt);
      setCandidate(null);
      requestAnimationFrame(() => heading.current?.focus());
    } catch (cause) {
      if (!controller.signal.aborted) failure(cause);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  const recorded = status && status.status !== "not_recovered";
  return (
    <section
      className="agent-checkpoint-recovery final-outcome-recovery"
      aria-label="Saved agent result"
    >
      <div className="final-outcome-heading">
        <ClipboardCheck size={22} aria-hidden="true" />
        <h3>Saved agent result</h3>
      </div>
      <p>
        Check whether the recorded final report can complete its original
        handoff. Existing commands and model responses are not executed again.
      </p>
      <ol className="final-outcome-steps" aria-label="Result recovery steps">
        <li aria-current={!candidate && !recorded ? "step" : undefined}>
          <span>1</span>Check the record
        </li>
        <li aria-current={candidate ? "step" : undefined}>
          <span>2</span>Review the next step
        </li>
        <li aria-current={recorded ? "step" : undefined}>
          <span>3</span>Follow the run
        </li>
      </ol>
      {!allowed ? (
        <p>
          A current factory administrator and an uncancelled run are required to
          inspect this recovery.
        </p>
      ) : (
        <>
          {blocked && (
            <p role="alert">
              The saved action identity cannot be verified in this browser. New
              submissions stay blocked; status can still be checked.
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
                ? "Checking…"
                : marker || recorded
                  ? "Read saved action"
                  : "Check saved result"}
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
          {candidate && (
            <div className="agent-checkpoint-review">
              <h4 ref={heading} tabIndex={-1}>
                Review the saved report
              </h4>
              <p>{outcomes[candidate.outcome]}</p>
              {candidate.followup_kind === "verification" && (
                <p>
                  <strong>Next step:</strong> the recorded verification handoff.
                </p>
              )}
              <dl className="agent-checkpoint-facts">
                <div>
                  <dt>Original attempt</dt>
                  <dd>{candidate.attempt}</dd>
                </div>
                <div>
                  <dt>Model requests already used</dt>
                  <dd>{candidate.usage.requests}</dd>
                </div>
                <div>
                  <dt>Tool calls already used</dt>
                  <dd>{candidate.usage.tool_calls}</dd>
                </div>
              </dl>
              {candidate.admission === "HOLD" ? (
                <div role="status">
                  <h4>Recovery is held</h4>
                  {candidate.holds.map((hold) => (
                    <p key={hold}>
                      {holds[hold] ??
                        "A current source, ownership or execution check prevents this continuation. Review the run evidence before another action."}
                    </p>
                  ))}
                </div>
              ) : (
                <>
                  <ul>
                    <li>Use the final output that was already consumed.</li>
                    <li>Keep completed effects and their original receipts.</li>
                    <li>Keep this request, attempt and spent budgets.</li>
                    <li>
                      Allow a worker to claim the continuation within the
                      reviewed window.
                    </li>
                  </ul>
                  <p className="muted">
                    The worker has {candidate.maximum_window_seconds} seconds to
                    claim the continuation. This is not a new duration limit for
                    development or QA.
                  </p>
                  <label className="check-choice">
                    <input
                      type="checkbox"
                      checked={acknowledged}
                      disabled={busy || !!marker || blocked}
                      onChange={(event) =>
                        setAcknowledged(event.target.checked)
                      }
                    />
                    I have reviewed these execution boundaries and the report's
                    next step.
                  </label>
                  <button
                    type="button"
                    className="primary"
                    disabled={busy || !!marker || blocked || !acknowledged}
                    onClick={() => void accept()}
                  >
                    <ClipboardCheck size={16} aria-hidden="true" />
                    Accept saved result
                  </button>
                </>
              )}
            </div>
          )}
          {recorded && (
            <div className="agent-checkpoint-review" role="status">
              <div className="final-outcome-heading">
                {status.run_status === "succeeded" ? (
                  <CheckCircle2 size={20} aria-hidden="true" />
                ) : (
                  <Clock3 size={20} aria-hidden="true" />
                )}
                <h4 ref={heading} tabIndex={-1}>
                  {resultLabels[status.status]}
                </h4>
              </div>
              <p>
                {status.status === "completed"
                  ? status.run_status === "paused"
                    ? "The saved report was accepted. The run is paused for its recorded follow-up; implementation success has not been established."
                    : "The saved report was accepted. Check the plan and verification results for the run's outcome."
                  : "Follow the original execution record for worker activity and retained results."}
              </p>
              <p>
                Used model and tool allowances stay attached to the original
                execution.
              </p>
            </div>
          )}
        </>
      )}
      <p className="muted">
        Checking a record does not claim the continuation or start work.
      </p>
    </section>
  );
}
