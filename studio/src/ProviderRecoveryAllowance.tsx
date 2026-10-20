/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  CheckCircle2,
  FileCheck2,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ForgeRun,
} from "./api";
import type { IdentitySession } from "./Identity";
import {
  allowanceCandidate,
  allowanceInput,
  allowanceMarkerKey,
  allowanceReceipt,
  allowanceReadback,
  existingAllowanceId,
  readAllowanceMarker,
  retainAllowance,
  type AllowanceContext,
  type AllowanceMarker,
  type AllowanceReview,
} from "./ProviderRecoveryAllowanceData";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";
import "./ProviderRecoveryAllowance.css";
import { codingStateEvent } from "./CodingCheckGrantData";

export function ProviderRecoveryAllowance({
  run,
  connection,
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
}) {
  const official = run.agent === "codex" || run.agent === "claude";
  const existingId = existingAllowanceId(run.provider_recovery);
  const nativeAdmin =
    session.global_admin &&
    session.user.authentication_method !== "scm_mapping";
  const potential =
    nativeAdmin &&
    official &&
    ((run.attempt === 4 && run.active_task === "implement") ||
      run.attempt === 5);
  const eligible =
    potential &&
    run.attempt === 4 &&
    run.status === "failed" &&
    !existingId &&
    run.cancel_request?.status !== "requested" &&
    !run.bridge_completion_hold;
  const context: AllowanceContext = {
    base: new URL(
      normalizeBase(connection.base),
      typeof window === "undefined"
        ? "https://localhost"
        : window.location.href,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    authentication: session.user.authentication_method,
    run: run.id,
    repository: run.repository_id,
    project: run.project_id ?? null,
    agent: run.agent === "claude" ? "claude" : "codex",
  };
  const [loaded, setLoaded] = useState(false),
    [codingFlow, setCodingFlow] = useState(false),
    [blocked, setBlocked] = useState(false),
    [marker, setMarker] = useState<AllowanceMarker | null>(null),
    [confirmed, setConfirmed] = useState(false),
    [candidate, setCandidate] = useState<AllowanceReview | null>(null),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0),
    [reason, setReason] = useState(""),
    [evidence, setEvidence] = useState(""),
    [adopt, setAdopt] = useState(false),
    [ack, setAck] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const retained = useRef<AllowanceMarker | null>(null);
  const live = useRef(true),
    inflight = useRef(false),
    controller = useRef<AbortController | null>(null),
    dialog = useRef<HTMLDialogElement>(null),
    opener = useRef<HTMLButtonElement>(null),
    heading = useRef<HTMLHeadingElement>(null),
    result = useRef<HTMLDivElement>(null);
  const path = `/runs/${run.id}/provider-recoveries/allowances`;
  useEffect(() => {
    live.current = true;
    if (!potential)
      return () => {
        live.current = false;
      };
    const key = allowanceMarkerKey(context);
    async function restore() {
      try {
        const raw = localStorage.getItem(key);
        if (raw && JSON.parse(raw).kind === "coding-check-recovery") {
          if (live.current) {
            setCodingFlow(true);
            setLoaded(true);
            setBlocked(true);
            setEditing(false);
            setConfirmed(false);
            controller.current?.abort();
          }
          return;
        }
        const saved = await readAllowanceMarker(raw, context);
        if (live.current) {
          if (saved) {
            retained.current = saved;
            setMarker(saved);
            setEditing(false);
            setCandidate(null);
            setAck(false);
          } else if (retained.current) {
            setBlocked(true);
          }
          setLoaded(true);
          setConfirmed(false);
        }
      } catch {
        if (live.current) {
          setBlocked(true);
          setLoaded(true);
          setConfirmed(false);
        }
      }
    }
    void restore();
    const storage = (event: StorageEvent) => {
      if (event.key === key || event.key === null) void restore();
    };
    window.addEventListener("storage", storage);
    window.addEventListener(codingStateEvent, restore);
    return () => {
      live.current = false;
      controller.current?.abort();
      window.removeEventListener("storage", storage);
      window.removeEventListener(codingStateEvent, restore);
    };
    // ExecutionRecord remounts on the complete connection and authority context.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const node = dialog.current;
    if (editing) node?.showModal();
    return () => node?.close();
  }, [editing]);
  useEffect(() => {
    if (marker && !editing) result.current?.focus({ preventScroll: true });
  }, [marker, editing]);
  function close() {
    controller.current?.abort();
    setEditing(false);
    requestAnimationFrame(() => {
      if (opener.current && !opener.current.disabled) opener.current.focus();
      else heading.current?.focus();
    });
  }
  function failure(cause: unknown, pending: boolean) {
    if (!live.current) return;
    setConfirmed(false);
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setCandidate(null);
      setEditing(false);
      setError(
        "Current administrator access could not be confirmed. Any saved request remains held for authorized readback.",
      );
    } else if (pending && cause instanceof ApiError && cause.status === 404) {
      setError(
        "No matching receipt is available yet. Keep the saved request; a missing receipt does not prove the reservation failed.",
      );
    } else if (pending) {
      setError(
        "The reservation is not confirmed. Its exact request is saved. Check that receipt before taking another action.",
      );
    } else {
      setCandidate(null);
      setError(
        "This run does not currently pass the final allowance checks. Review its execution record and refresh the candidate.",
      );
    }
  }
  async function loadCandidate(signal: AbortSignal) {
    return allowanceCandidate(
      await request<unknown>(
        connection,
        path +
          `/candidate?agent=${context.agent}&adopt_current_executable=true`,
        { signal },
      ),
      context,
    );
  }
  async function refresh() {
    if (inflight.current || !eligible || marker || blocked) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    setAck(false);
    setAdopt(false);
    const c = new AbortController();
    controller.current = c;
    try {
      const view = await loadCandidate(c.signal);
      if (live.current && !c.signal.aborted) setCandidate(view);
    } catch (cause) {
      if (!c.signal.aborted) failure(cause, false);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function exact(saved: AllowanceMarker, signal: AbortSignal) {
    await allowanceReceipt(
      await request<unknown>(connection, path + "/" + saved.body.request_id, {
        signal,
      }),
      saved,
    );
    if (live.current && !signal.aborted) {
      setConfirmed(true);
      setError("");
    }
  }
  async function check() {
    if (inflight.current || (!marker && !existingId)) return;
    inflight.current = true;
    setBusy(true);
    setConfirmed(false);
    setError("");
    const c = new AbortController();
    controller.current = c;
    try {
      if (marker) await exact(marker, c.signal);
      else {
        await allowanceReadback(
          await request<unknown>(connection, path + "/" + existingId, {
            signal: c.signal,
          }),
          context,
          existingId!,
        );
        if (live.current && !c.signal.aborted) {
          setConfirmed(true);
          setError("");
        }
      }
    } catch (cause) {
      if (!c.signal.aborted) failure(cause, true);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  let validMitigation = false;
  if (candidate) {
    try {
      allowanceInput(candidate, "0".repeat(32), reason, evidence);
      validMitigation = adopt;
    } catch {
      /* Incomplete form. */
    }
  }
  async function reserve() {
    if (
      inflight.current ||
      !eligible ||
      !candidate ||
      !validMitigation ||
      !ack ||
      marker ||
      blocked
    )
      return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const c = new AbortController();
    controller.current = c;
    let saved: AllowanceMarker | null = null;
    try {
      const fresh = await loadCandidate(c.signal);
      if (!live.current || c.signal.aborted) return;
      if (JSON.stringify(fresh) !== JSON.stringify(candidate)) {
        setCandidate(null);
        setStep(0);
        setAdopt(false);
        setAck(false);
        setError(
          "The reviewed source, executable or failure changed. Refresh and review the current candidate before reserving.",
        );
        return;
      }
      const body = allowanceInput(
        candidate,
        crypto.randomUUID().replaceAll("-", ""),
        reason,
        evidence,
      );
      try {
        saved = await retainAllowance(
          localStorage,
          navigator.locks,
          context,
          candidate,
          body,
        );
      } catch {
        setBlocked(true);
        setError(
          "Request storage could not be verified. No reservation was sent. Keep any saved identity for reconciliation.",
        );
        return;
      }
      if (!live.current) return;
      retained.current = saved;
      setMarker(saved);
      setEditing(false);
      if (c.signal.aborted) return;
      await request<unknown>(connection, path, {
        method: "POST",
        body: JSON.stringify(saved.body),
        signal: c.signal,
      });
      await exact(saved, c.signal);
    } catch (cause) {
      if (!c.signal.aborted) failure(cause, saved !== null);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  if (!potential) return null;
  if (codingFlow) return null;
  const name = context.agent === "codex" ? "Codex" : "Claude Code";
  return (
    <section
      className="provider-allowance-panel"
      aria-label="Final provider allowance"
    >
      <div className="provider-allowance-intro">
        <ShieldCheck size={22} aria-hidden="true" />
        <div>
          <h2 ref={heading} tabIndex={-1}>
            Final provider allowance
          </h2>
          <p>
            Administrator review for one final {name} dispatch in this run. The
            automatic four-attempt limit stays unchanged.
          </p>
        </div>
      </div>
      {!loaded ? (
        <p role="status">Checking saved request…</p>
      ) : marker || existingId ? (
        <div
          ref={result}
          tabIndex={-1}
          className="provider-allowance-result"
          role="status"
        >
          <h3>
            {confirmed ? (
              <>
                <CheckCircle2 size={18} aria-hidden="true" /> Final allowance
                reserved
              </>
            ) : marker ? (
              "Exact reservation request saved"
            ) : (
              "Existing final reservation"
            )}
          </h3>
          <p>
            {confirmed
              ? "The server retained the final allowance. It is spent even if dispatch never starts. Follow the plan and execution record for the actual outcome."
              : marker
                ? "The outcome is not confirmed. Keep this request and check its exact receipt. No reservation will be repeated."
                : "The run identifies an existing reservation. Its original request is not saved in this browser. Read the server receipt to confirm it; this action cannot reserve another attempt."}
          </p>
          <p className="provider-allowance-identity">
            Request <code>{marker?.body.request_id ?? existingId}</code>
          </p>
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => void check()}
          >
            <RefreshCw size={16} aria-hidden="true" />
            {busy
              ? "Checking receipt…"
              : marker
                ? "Check saved reservation"
                : "Read existing reservation"}
          </button>
        </div>
      ) : blocked ? (
        <p role="status">
          Saved request storage cannot be verified. New final allowance
          reservations are disabled.
        </p>
      ) : eligible ? (
        <button
          ref={opener}
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => {
            setEditing(true);
            setStep(0);
            setCandidate(null);
            setAdopt(false);
            setAck(false);
            void refresh();
          }}
        >
          Review final allowance <ArrowRight size={16} aria-hidden="true" />
        </button>
      ) : (
        <p className="subtle">
          No new final allowance is available for this run. Existing execution
          records remain available below.
        </p>
      )}
      {!editing && error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {editing && (
        <dialog
          ref={dialog}
          className="access-member-dialog provider-allowance-dialog"
          aria-label="Reserve a final provider allowance"
          onCancel={(event) => {
            event.preventDefault();
            close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Final provider allowance</h2>
            <button
              type="button"
              className="quiet"
              aria-label="Close final allowance review"
              onClick={close}
            >
              <X size={18} aria-hidden="true" />
              Close
            </button>
          </div>
          <Wizard
            steps={["Failed attempt", "Mitigation", "Review"]}
            active={step}
            onStep={(next) => {
              setStep(next);
              setAck(false);
            }}
            locked={busy || blocked}
            title={
              [
                "Review the failed attempt",
                "Record the mitigation",
                "Confirm the final reservation",
              ][step]
            }
            description={
              [
                "The server checks current authority, native process evidence and retained source.",
                "Explain what you reviewed before spending the final allowance.",
                "This reserves one fifth dispatch. The allowance cannot be renewed.",
              ][step]
            }
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (step < 2) {
                  setStep(step + 1);
                  setAck(false);
                } else void reserve();
              }}
            >
              {error && (
                <p className="alert" role="alert">
                  {error}
                </p>
              )}
              {step === 0 ? (
                <>
                  {busy ? (
                    <p role="status">Checking the current candidate…</p>
                  ) : candidate ? (
                    <>
                      <dl className="provider-allowance-facts">
                        <div>
                          <dt>Provider</dt>
                          <dd>{name}</dd>
                        </div>
                        <div>
                          <dt>Failed attempt</dt>
                          <dd>4</dd>
                        </div>
                        <div>
                          <dt>Final dispatch</dt>
                          <dd>5</dd>
                        </div>
                      </dl>
                      <p>
                        A native failure receipt is retained. Eligibility
                        requires a nonzero exit, a waited leader, drained
                        streams and an absent process group.
                      </p>
                      <div className="provider-allowance-executable">
                        <FileCheck2 size={20} aria-hidden="true" />
                        <div>
                          <strong>Current CLI selected for review</strong>
                          <code>{candidate.executablePath}</code>
                        </div>
                      </div>
                      <details>
                        <summary>Source and executable fingerprints</summary>
                        <dl className="provider-allowance-digests">
                          <div>
                            <dt>Retained source</dt>
                            <dd>{candidate.sourceDigest}</dd>
                          </div>
                          <div>
                            <dt>Git identity</dt>
                            <dd>{candidate.gitDigest}</dd>
                          </div>
                          <div>
                            <dt>CLI SHA-256</dt>
                            <dd>{candidate.executableSha256}</dd>
                          </div>
                          <div>
                            <dt>Native failure receipt</dt>
                            <dd>{candidate.nativeProcessDigest}</dd>
                          </div>
                        </dl>
                      </details>
                      <p className="subtle">
                        The existing workspace, model configuration and spent
                        repair and verification budgets stay attached to this
                        run.
                      </p>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void refresh()}
                      disabled={busy}
                    >
                      Refresh candidate
                    </button>
                  )}
                </>
              ) : step === 1 ? (
                <>
                  <div className="field">
                    <label htmlFor="allowance-reason">
                      Mitigation reviewed
                    </label>
                    <textarea
                      id="allowance-reason"
                      rows={4}
                      maxLength={2000}
                      required
                      value={reason}
                      placeholder="Describe the change and local checks you reviewed."
                      onChange={(event) => {
                        setReason(event.target.value);
                        setAck(false);
                      }}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="allowance-evidence">
                      Mitigation evidence SHA-256
                    </label>
                    <input
                      id="allowance-evidence"
                      type="text"
                      spellCheck={false}
                      autoComplete="off"
                      required
                      pattern="[a-f0-9]{64}"
                      maxLength={64}
                      value={evidence}
                      aria-describedby="allowance-evidence-help"
                      onChange={(event) => {
                        setEvidence(event.target.value);
                        setAck(false);
                      }}
                    />
                    <small id="allowance-evidence-help">
                      Paste the 64-character lowercase fingerprint of the
                      evidence you reviewed. This records your attestation;
                      forge does not run or validate those tests here.
                    </small>
                  </div>
                  <label className="check-choice provider-allowance-choice">
                    <input
                      type="checkbox"
                      checked={adopt}
                      onChange={(event) => {
                        setAdopt(event.target.checked);
                        setAck(false);
                      }}
                    />
                    Use the reviewed current CLI for this attempt.
                  </label>
                </>
              ) : (
                <>
                  <dl className="provider-allowance-review">
                    <div>
                      <dt>Reservation</dt>
                      <dd>One fifth {name} dispatch in this run</dd>
                    </div>
                    <div>
                      <dt>Mitigation reviewed</dt>
                      <dd>{reason}</dd>
                    </div>
                    <div>
                      <dt>Evidence SHA-256</dt>
                      <dd>
                        <code>{evidence}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Selected CLI</dt>
                      <dd>
                        <code>{candidate?.executablePath}</code>
                      </dd>
                    </div>
                  </dl>
                  <p>
                    The allowance is spent when reserved, including a timeout,
                    lost response, unlaunched dispatch or failed attempt. There
                    is no sixth dispatch through this recovery.
                  </p>
                  <label className="check-choice provider-allowance-choice">
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(event) => setAck(event.target.checked)}
                    />
                    I authorize this one final reservation and understand it
                    cannot be renewed.
                  </label>
                </>
              )}
              <div className="wizard-actions">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    if (step > 0) {
                      setStep(step - 1);
                      setAck(false);
                    } else close();
                  }}
                  disabled={busy}
                >
                  {step ? "Back" : "Cancel review"}
                </button>
                <button
                  type="submit"
                  className="primary"
                  disabled={
                    busy ||
                    blocked ||
                    !candidate ||
                    (step === 1 && !validMitigation) ||
                    (step === 2 && (!validMitigation || !ack))
                  }
                >
                  {busy
                    ? "Checking…"
                    : [
                        "Continue to mitigation",
                        "Review reservation",
                        "Reserve final allowance",
                      ][step]}
                  <ArrowRight size={16} aria-hidden="true" />
                </button>
              </div>
            </form>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
