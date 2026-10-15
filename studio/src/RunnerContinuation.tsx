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
  candidateRead,
  continuationKey,
  continuationReceipt,
  readContinuationMarker,
  retainContinuation,
  type ContinuationCommand,
  type ContinuationContext,
  type ContinuationMarker,
} from "./RunnerContinuationData";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";
const reasons: Record<string, string> = {
  read_budget_exceeded:
    "Bounded local inspection is unavailable. No continuation is offered; exact saved requests remain readable.",
  source_unavailable:
    "This API host cannot verify the shared workspace. Use the authorized host with the shared source; existing request receipts remain read-only.",
  leader_unavailable:
    "The original Factory leader is not currently proven live. No request is offered.",
  source_changed:
    "Original source changed. Inspect the execution evidence; no continuation is offered.",
  request_already_recorded:
    "An original command already has a saved continuation request. Use its exact request identity; do not create another.",
  admission_unavailable: "The bounded continuation admission queue is full.",
  not_applicable: "This run does not use an original pool command.",
  no_candidate:
    "No original active command currently passes local continuation checks.",
  evidence_unavailable:
    "The original local command evidence cannot be verified.",
  evidence_changed:
    "Local command evidence changed. Refresh local checks before reviewing it.",
};
export function RunnerContinuation({
  run,
  connection,
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
}) {
  const context: ContinuationContext = {
    base: new URL(normalizeBase(connection.base), window.location.href).href,
    actor: session.user.id,
    repository: run.repository_id,
    project: run.project_id ?? null,
    run: run.id,
  };
  const [saved] = useState(() => {
    try {
      return {
        marker: readContinuationMarker(
          sessionStorage.getItem(continuationKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<ContinuationMarker | null>(saved.marker),
    [blocked, setBlocked] = useState(saved.blocked),
    [commands, setCommands] = useState<ContinuationCommand[]>([]),
    [state, setState] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0),
    [selected, setSelected] = useState(""),
    [ack, setAck] = useState(false),
    [receipt, setReceipt] = useState<{
      status: string;
      accepted: boolean;
    } | null>(null);
  const active = useRef<AbortController | null>(null),
    live = useRef(true),
    inflight = useRef(false),
    dialog = useRef<HTMLDialogElement>(null),
    opener = useRef<HTMLButtonElement>(null),
    result = useRef<HTMLDivElement>(null),
    heading = useRef<HTMLHeadingElement>(null);
  const permission = run.publish ? "release" : "execute";
  const allowed =
    session.global_admin ||
    session.memberships.some(
      (m) =>
        m.project_id === context.project && m.permissions.includes(permission),
    );
  const activeRun =
    run.status === "running" && run.cancel_request?.status !== "requested";
  const path = `/runs/${run.id}/runner-continuations`;
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    const node = dialog.current;
    if (editing) node?.showModal();
    return () => node?.close();
  }, [editing]);
  useEffect(() => {
    if (marker && !editing) result.current?.focus({ preventScroll: true });
  }, [marker, editing]);
  function failure(cause: unknown) {
    if (!live.current) return;
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setCommands([]);
      setReceipt(null);
      setState("");
      setEditing(false);
      setError(
        "Current access could not be confirmed. Private confirmation has been cleared. Any saved request identity remains held for authorized reconciliation or cancellation.",
      );
    } else if (cause instanceof ApiError && cause.status === 404)
      setError(
        "No matching receipt is currently available. This does not prove the request was not dispatched.",
      );
    else
      setError(
        "The original request could not be confirmed. Its identity is retained; no request was repeated.",
      );
  }
  async function candidates(signal: AbortSignal) {
    return candidateRead(
      await request<unknown>(connection, path + "/candidates", { signal }),
      context,
    );
  }
  async function refresh() {
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    active.current = controller;
    try {
      const data = await candidates(controller.signal);
      if (live.current && !controller.signal.aborted) {
        setCommands(data.commands);
        setState(data.state);
      }
    } catch (cause) {
      failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  useEffect(() => {
    if (!allowed || !activeRun || marker || blocked) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    void candidates(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setCommands(data.commands);
          setState(data.state);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) failure(cause);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
    // The owner remounts on changes to the complete authority context.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function exact(m: ContinuationMarker, signal: AbortSignal) {
    const value = continuationReceipt(
      await request<unknown>(connection, path + "/" + m.request, { signal }),
      m,
    );
    if (live.current && !signal.aborted) {
      setReceipt(value);
      setError("");
    }
  }
  async function check() {
    if (inflight.current || !marker) return;
    inflight.current = true;
    setBusy(true);
    const controller = new AbortController();
    active.current = controller;
    try {
      await exact(marker, controller.signal);
    } catch (cause) {
      failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  function close() {
    if (busy) return;
    setEditing(false);
    requestAnimationFrame(() => {
      if (opener.current && !opener.current.disabled) opener.current.focus();
      else heading.current?.focus();
    });
  }
  async function send() {
    if (inflight.current || !ack || marker || blocked || !allowed || !activeRun)
      return;
    const command = commands.find((c) => c.execution_id === selected);
    if (!command) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    active.current = controller;
    try {
      const fresh = await candidates(controller.signal);
      if (!live.current || controller.signal.aborted) return;
      if (
        !fresh.commands.some(
          (c) => JSON.stringify(c) === JSON.stringify(command),
        )
      ) {
        setCommands([]);
        setError(
          "The original command changed. Close this review and refresh local checks.",
        );
        return;
      }
      let retained: ContinuationMarker;
      try {
        retained = retainContinuation(
          sessionStorage,
          context,
          command,
          crypto.randomUUID().replaceAll("-", ""),
        );
      } catch {
        setBlocked(true);
        setError(
          "Request storage could not be verified. Nothing was sent; keep any existing saved identity for reconciliation.",
        );
        return;
      }
      setMarker(retained);
      setEditing(false);
      try {
        await request<unknown>(connection, path, {
          method: "POST",
          body: JSON.stringify({
            request_id: retained.request,
            execution_id: retained.execution,
          }),
          signal: controller.signal,
        });
      } catch (cause) {
        failure(cause);
        return;
      }
      await exact(retained, controller.signal);
    } catch (cause) {
      failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  if (!allowed) return null;
  return (
    <section
      className="continuation-panel"
      aria-label="Original runner command continuation"
    >
      <div>
        <h2 ref={heading} tabIndex={-1}>
          Original runner command
        </h2>
        <p>
          Request the same pending pool command under its original actor, source
          and budgets. The leader confirms it is still waiting; admission is not
          command success.
        </p>
      </div>
      {marker ? (
        <div ref={result} tabIndex={-1} role="status">
          <h3>Original request retained</h3>
          <p>
            {receipt?.accepted
              ? "Continuation admission accepted. This does not confirm the command succeeded."
              : receipt
                ? `Request state: ${receipt.status.replaceAll("_", " ")}. No request will be repeated.`
                : "The outcome is not confirmed. Keep this exact request; do not submit another identity."}
          </p>
          <details>
            <summary>Request identity and original evidence</summary>
            <dl className="learning-record-summary">
              <div>
                <dt>Request</dt>
                <dd>{marker.request}</dd>
              </div>
              <div>
                <dt>Execution</dt>
                <dd>{marker.execution}</dd>
              </div>
              <div>
                <dt>Original task</dt>
                <dd>
                  {marker.task} · attempt {marker.attempt}
                </dd>
              </div>
            </dl>
            <p>
              The development plan and immutable journal below retain the
              original command. Use the run's authorized cancellation controls
              when necessary; this view cannot replay or reset a request.
            </p>
            <a href={`#run-journal-${run.id}`}>
              Inspect original execution journal
            </a>
            {activeRun && (
              <p>
                <a href={`#run-controls-${run.id}`}>
                  Inspect authorized run cancellation controls
                </a>
              </p>
            )}
          </details>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void check()}
          >
            {busy ? "Checking original request…" : "Check original request"}
          </button>
        </div>
      ) : (
        <div>
          <p>
            {blocked
              ? "Saved request storage cannot be verified. New continuation requests are unavailable."
              : !activeRun
                ? "This run is not active; no new continuation request is offered."
                : reasons[state]}
          </p>
          {activeRun && commands.length > 0 && (
            <button
              ref={opener}
              className="secondary"
              disabled={busy || blocked}
              onClick={() => {
                setEditing(true);
                setStep(0);
                setSelected("");
                setAck(false);
                setError("");
              }}
            >
              Request continuation
            </button>
          )}
          <button
            className="secondary"
            disabled={busy || blocked || !activeRun}
            onClick={() => void refresh()}
          >
            Refresh local checks
          </button>
        </div>
      )}
      {!editing && error && <p role="alert">{error}</p>}
      {editing && (
        <dialog
          ref={dialog}
          className="access-member-dialog learning-invalidation-dialog"
          aria-label="Request original command continuation"
          onCancel={(event) => {
            event.preventDefault();
            close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Request original command continuation</h2>
            <button
              className="quiet"
              aria-label="Close continuation review"
              disabled={busy}
              onClick={close}
            >
              Close
            </button>
          </div>
          <Wizard
            steps={["Original command", "Review"]}
            active={step}
            onStep={(value) => {
              setStep(value);
              setAck(false);
            }}
            locked={busy || blocked}
            title={
              step === 0
                ? "Select the original command"
                : "Review one continuation request"
            }
            description="Local checks are provisional. The current leader and controller retain final authority."
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (step === 0) {
                  setStep(1);
                  setAck(false);
                } else void send();
              }}
            >
              {error && <p role="alert">{error}</p>}
              {step === 0 ? (
                <fieldset>
                  <legend>Commands awaiting leader confirmation</legend>
                  {commands.map((c) => (
                    <label key={c.execution_id}>
                      <input
                        type="radio"
                        name="continuation-command"
                        value={c.execution_id}
                        checked={selected === c.execution_id}
                        onChange={() => setSelected(c.execution_id)}
                      />
                      {c.command_label} · {c.task_id} · attempt {c.attempt}
                    </label>
                  ))}
                </fieldset>
              ) : (
                <>
                  <h3>
                    {
                      commands.find((c) => c.execution_id === selected)
                        ?.command_label
                    }
                  </h3>
                  <p>
                    This preserves the original execution identity and frozen
                    budgets. No replacement command, actor or source is
                    submitted.
                  </p>
                  <details>
                    <summary>Original command identity</summary>
                    <p>{selected}</p>
                    <p>
                      Argument count:{" "}
                      {
                        commands.find((c) => c.execution_id === selected)
                          ?.argument_count
                      }
                    </p>
                  </details>
                  <label>
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(event) => setAck(event.target.checked)}
                    />
                    I understand the leader must confirm this original command.
                  </label>
                </>
              )}
              <div className="wizard-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={close}
                >
                  Cancel review
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={busy || blocked || (step === 0 ? !selected : !ack)}
                >
                  {step === 0
                    ? "Review original command"
                    : "Send continuation request"}
                </button>
              </div>
            </form>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
