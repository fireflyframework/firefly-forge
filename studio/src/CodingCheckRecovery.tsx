/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  CheckCircle2,
  FileCheck2,
  ShieldCheck,
  Terminal,
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
import { Wizard } from "./Wizard";
import {
  allowanceCandidate,
  allowanceInput,
  allowanceMarkerKey,
  existingAllowanceId,
  type AllowanceContext,
  type AllowanceReview,
} from "./ProviderRecoveryAllowanceData";
import {
  codingAllowanceReceipt,
  codingCandidate,
  codingCurrent,
  codingInput,
  codingPort,
  codingReceipt,
  codingUnclaimed,
  readCodingMarker,
  retainCodingAllowance,
  retainCodingGrant,
  type CodingCandidate,
  type CodingMarker,
  type CodingReceipt,
} from "./CodingCheckGrantData";
import "./ProjectAccess.css";
import "./ProviderRecoveryAllowance.css";
import "./CodingCheckRecovery.css";

export function CodingCheckRecovery({
  run,
  connection,
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
}) {
  const admin =
    session.global_admin &&
    session.user.authentication_method !== "scm_mapping";
  const potential =
    admin &&
    (run.agent === "codex" || run.agent === "claude") &&
    ((run.attempt === 4 && run.active_task === "implement") ||
      run.attempt === 5);
  const eligible =
    potential &&
    run.attempt === 4 &&
    run.status === "failed" &&
    !existingAllowanceId(run.provider_recovery) &&
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
    [foreign, setForeign] = useState(false),
    [held, setHeld] = useState(false),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [candidate, setCandidate] = useState<CodingCandidate | null>(null),
    [marker, setMarker] = useState<CodingMarker | null>(null),
    [receipt, setReceipt] = useState<CodingReceipt | null>(null),
    [finalView, setFinalView] = useState<AllowanceReview | null>(null),
    [reserved, setReserved] = useState(false);
  const [selected, setSelected] = useState<number[]>([]),
    [calls, setCalls] = useState("1"),
    [seconds, setSeconds] = useState("900"),
    [port, setPort] = useState("0"),
    [grantReason, setGrantReason] = useState(""),
    [reason, setReason] = useState(""),
    [evidence, setEvidence] = useState(""),
    [adopt, setAdopt] = useState(false),
    [ack, setAck] = useState(false);
  const live = useRef(true),
    inflight = useRef(false),
    controller = useRef<AbortController | null>(null),
    dialog = useRef<HTMLDialogElement>(null),
    opener = useRef<HTMLButtonElement>(null),
    heading = useRef<HTMLHeadingElement>(null),
    futureId = useRef(""),
    retained = useRef<CodingMarker | null>(null);
  const path = `/runs/${run.id}/coding-check-grants`,
    finalPath = `/runs/${run.id}/provider-recoveries/allowances`;
  useEffect(() => {
    live.current = true;
    async function restore() {
      if (!potential) return;
      try {
        const raw = localStorage.getItem(allowanceMarkerKey(context));
        if (raw !== null && JSON.parse(raw).kind !== "coding-check-recovery") {
          if (live.current) {
            setForeign(true);
            setLoaded(true);
            setEditing(false);
            controller.current?.abort();
          }
          return;
        }
        const saved = await readCodingMarker(raw, context);
        if (
          retained.current &&
          (!saved ||
            saved.inputDigest !== retained.current.inputDigest ||
            (retained.current.allowance &&
              saved.allowance?.inputDigest !==
                retained.current.allowance.inputDigest))
        )
          throw new Error("The retained recovery changed");
        if (live.current) {
          retained.current = saved;
          setMarker(saved);
          setLoaded(true);
          setReceipt(null);
          setReserved(false);
          if (saved) {
            futureId.current = saved.grant.recovery_request_id;
            setEditing(false);
            controller.current?.abort();
          }
        }
      } catch {
        if (live.current) {
          setHeld(true);
          setLoaded(true);
        }
      }
    }
    void restore();
    const storage = (event: StorageEvent) => {
      if (event.key === null || event.key === allowanceMarkerKey(context))
        void restore();
    };
    window.addEventListener("storage", storage);
    return () => {
      live.current = false;
      controller.current?.abort();
      window.removeEventListener("storage", storage);
    };
    // The execution record remounts on connection, run and authority changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    const node = dialog.current;
    if (editing) node?.showModal();
    return () => node?.close();
  }, [editing]);
  useEffect(() => {
    if (!editing || !dialog.current) return;
    dialog.current
      .querySelector<HTMLElement>(".wizard-heading h2")
      ?.focus({ preventScroll: true });
    dialog.current.scrollTop = 0;
  }, [editing, step]);
  useEffect(() => {
    if (!receipt) return;
    const remaining = receipt.expires_at_ms - Date.now();
    const timer = setTimeout(
      () => {
        setReceipt(null);
        setFinalView(null);
        setError(
          "The configured-check grant expired. No replacement grant or recovery was sent.",
        );
      },
      Math.min(Math.max(remaining, 0), 3600000),
    );
    return () => clearTimeout(timer);
  }, [receipt]);
  function fail(cause: unknown) {
    setReceipt(null);
    setReserved(false);
    setError(
      cause instanceof ApiError && [401, 403].includes(cause.status)
        ? "Current administrator access could not be confirmed. Keep the saved identity and check it after access is restored."
        : retained.current
          ? "This action is not confirmed. Check the saved exact receipt; no grant or reservation will be repeated."
          : "The review could not be confirmed. No grant or reservation was sent. Refresh the current check review before continuing.",
    );
  }
  async function action(call: (signal: AbortSignal) => Promise<void>) {
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    const c = new AbortController();
    controller.current = c;
    try {
      await call(c.signal);
    } catch (cause) {
      if (live.current && !c.signal.aborted) fail(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function loadCandidate(signal: AbortSignal) {
    const value = await request<unknown>(
      connection,
      `${path}/candidate?recovery_request_id=${futureId.current}&bind_port=${codingPort(Number(port))}`,
      { signal },
    );
    return codingCandidate(
      value,
      context,
      futureId.current,
      codingPort(Number(port)),
    );
  }
  function close() {
    controller.current?.abort();
    setEditing(false);
    requestAnimationFrame(() => {
      if (opener.current && !opener.current.disabled) opener.current.focus();
      else heading.current?.focus();
    });
  }
  function prepare() {
    if (!eligible || marker || foreign || held) return;
    setEditing(true);
    setStep(0);
    setAck(false);
    void action(async (signal) => {
      try {
        codingUnclaimed(
          await request<unknown>(connection, `${path}/current`, { signal }),
          context,
        );
      } catch (cause) {
        if (live.current && !signal.aborted) {
          setHeld(true);
          setEditing(false);
        }
        throw cause;
      }
      if (!live.current || signal.aborted) return;
      futureId.current ||= crypto.randomUUID().replaceAll("-", "");
      const c = await loadCandidate(signal);
      if (!live.current || signal.aborted) return;
      setCandidate(c);
      setSelected(c.command_indices);
      setCalls(
        String(Math.min(c.command_count, c.interactive_commands_remaining)),
      );
    });
  }
  async function active(saved: CodingMarker, signal: AbortSignal) {
    const value = await request<unknown>(
        connection,
        `${path}/${saved.grant.request_id}`,
        { signal },
      ),
      parsed = await codingReceipt(value, saved);
    const current = await request<unknown>(connection, `${path}/current`, {
      signal,
    });
    return codingCurrent(current, context, parsed);
  }
  async function exact(saved: CodingMarker, signal: AbortSignal) {
    if (saved.allowance) {
      const value = await request<unknown>(
        connection,
        `${finalPath}/${saved.grant.recovery_request_id}`,
        { signal },
      );
      await codingAllowanceReceipt(value, saved);
      if (live.current && !signal.aborted) setReserved(true);
      return;
    }
    const parsed = await active(saved, signal);
    const value = await request<unknown>(
      connection,
      `${finalPath}/candidate?agent=${context.agent}&adopt_current_executable=true`,
      { signal },
    );
    const view = allowanceCandidate(value, context);
    if (live.current && !signal.aborted) {
      setReceipt(parsed);
      setFinalView(view);
      setStep(2);
      setAdopt(false);
      setAck(false);
    }
  }
  function grant() {
    if (!candidate || marker || held || foreign || !eligible) return;
    void action(async (signal) => {
      const fresh = await loadCandidate(signal);
      if (JSON.stringify(fresh) !== JSON.stringify(candidate)) {
        setCandidate(null);
        setStep(0);
        throw new Error("Candidate changed");
      }
      const body = codingInput(
        candidate,
        crypto.randomUUID().replaceAll("-", ""),
        selected,
        Number(calls),
        Number(seconds),
        grantReason,
      );
      const saved = await retainCodingGrant(
        localStorage,
        navigator.locks,
        context,
        candidate,
        body,
      );
      retained.current = saved;
      if (!live.current) return;
      setMarker(saved);
      if (signal.aborted) return;
      await request<unknown>(connection, path, {
        method: "POST",
        body: JSON.stringify(saved.grant),
        signal,
      });
      await exact(saved, signal);
    });
  }
  function reserve() {
    if (
      !marker ||
      marker.allowance ||
      !receipt ||
      !finalView ||
      !adopt ||
      !ack ||
      held ||
      !eligible
    )
      return;
    void action(async (signal) => {
      await active(marker, signal);
      const fresh = allowanceCandidate(
        await request<unknown>(
          connection,
          `${finalPath}/candidate?agent=${context.agent}&adopt_current_executable=true`,
          { signal },
        ),
        context,
      );
      if (JSON.stringify(fresh) !== JSON.stringify(finalView)) {
        setFinalView(null);
        setAdopt(false);
        setStep(2);
        throw new Error("Recovery changed");
      }
      const body = {
        ...allowanceInput(
          finalView,
          marker.grant.recovery_request_id,
          reason,
          evidence,
        ),
        coding_check_grant_id: marker.grant.request_id,
      };
      const saved = await retainCodingAllowance(
        localStorage,
        navigator.locks,
        marker,
        finalView,
        body,
      );
      retained.current = saved;
      if (!live.current) return;
      setMarker(saved);
      if (signal.aborted) return;
      await request<unknown>(connection, finalPath, {
        method: "POST",
        body: JSON.stringify(body),
        signal,
      });
      await exact(saved, signal);
    });
  }
  let scopeValid = false,
    mitigationValid = false;
  try {
    if (candidate) {
      codingInput(
        candidate,
        "0".repeat(32),
        selected,
        Number(calls),
        Number(seconds),
        grantReason,
      );
      scopeValid = true;
    }
  } catch {
    /* Incomplete scope. */
  }
  try {
    if (finalView) {
      allowanceInput(finalView, futureId.current, reason, evidence);
      mitigationValid = adopt && receipt !== null;
    }
  } catch {
    /* Incomplete mitigation. */
  }
  if (!potential) return null;
  return (
    <section
      className="provider-allowance-panel coding-check-panel"
      aria-label="Recovery with configured checks"
    >
      <div className="provider-allowance-intro">
        <ShieldCheck size={22} aria-hidden="true" />
        <div>
          <h2 ref={heading} tabIndex={-1}>
            Recovery with configured checks
          </h2>
          <p>
            Grant a coding agent access to selected configured checks, then
            reserve the final recovery. Check results do not replace independent
            final verification.
          </p>
        </div>
      </div>
      {!loaded ? (
        <p role="status">Checking saved recovery…</p>
      ) : foreign ? (
        <p>
          A different final allowance request is already retained. Use its
          original recovery panel; this flow cannot replace it.
        </p>
      ) : held && !marker ? (
        <p className="alert" role="alert">
          An existing or unconfirmed configured-check grant blocks this flow.
          Restore its original saved identity before continuing. No new grant or
          recovery request will be sent.
        </p>
      ) : marker ? (
        <div className="provider-allowance-result" aria-live="polite">
          <h3>
            {reserved ? (
              <>
                <CheckCircle2 size={20} aria-hidden="true" />
                Final recovery reserved
              </>
            ) : marker.allowance ? (
              "Read the saved final reservation"
            ) : receipt ? (
              "Configured-check grant confirmed"
            ) : (
              "Read the saved configured-check grant"
            )}
          </h3>
          <p>
            {reserved
              ? "One fifth dispatch is reserved and spent, even if it never starts. Check the execution record for actual results. There is no sixth attempt."
              : marker.allowance
                ? "Keep this exact identity and read its receipt. A lost response does not prove that reservation failed."
                : "The grant and future recovery identity are retained. Confirm its exact receipt and current authority before continuing."}
          </p>
          <details>
            <summary>Request details</summary>
            <dl className="provider-allowance-digests">
              <div>
                <dt>Grant request</dt>
                <dd>{marker.grant.request_id}</dd>
              </div>
              <div>
                <dt>Recovery request</dt>
                <dd>{marker.grant.recovery_request_id}</dd>
              </div>
            </dl>
          </details>
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => {
              setEditing(true);
              void action((signal) => exact(marker, signal));
            }}
          >
            {busy
              ? "Checking receipt…"
              : marker.allowance
                ? "Check saved final reservation"
                : "Check saved grant"}
          </button>
        </div>
      ) : (
        <button
          ref={opener}
          type="button"
          className="secondary"
          disabled={!eligible || busy}
          onClick={prepare}
        >
          <FileCheck2 size={18} aria-hidden="true" />
          Prepare recovery with configured checks
        </button>
      )}
      {!editing && error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {editing && (
        <dialog
          ref={dialog}
          className="access-member-dialog provider-allowance-dialog coding-check-dialog"
          aria-label="Recovery with configured checks"
          onCancel={(event) => {
            event.preventDefault();
            close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Recovery with configured checks</h2>
            <button
              type="button"
              className="quiet"
              onClick={close}
              aria-label="Close configured-check recovery"
            >
              <X size={18} aria-hidden="true" />
              Close
            </button>
          </div>
          {reserved ? (
            <div className="provider-allowance-result" role="status">
              <h3>Final recovery reserved</h3>
              <p>
                The exact linked reservation is confirmed. Follow execution and
                independent verification in the run record. This does not prove
                product QA passed.
              </p>
              <button type="button" className="secondary" onClick={close}>
                Return to execution record
              </button>
            </div>
          ) : (
            <Wizard
              steps={["Checks", "Grant", "Mitigation", "Reserve"]}
              active={step}
              onStep={(next) => {
                if (!marker) {
                  setStep(next);
                  setAck(false);
                }
              }}
              locked={busy || marker !== null}
              title={
                [
                  "Review configured checks",
                  "Grant selected checks",
                  "Review the recovery mitigation",
                  "Reserve the final attempt",
                ][step]
              }
              description={
                [
                  "Only retained commands can be selected. This does not grant general command execution.",
                  "This grants scoped check access for one identified recovery; it does not reserve a dispatch.",
                  "The candidate is refreshed after the grant. The original failure and source history remain retained.",
                  "Uses the confirmed configured-check grant. This reserves the last recovery attempt, even if it never starts.",
                ][step]
              }
            >
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  if (step === 0 && candidate) setStep(1);
                  else if (step === 1) grant();
                  else if (step === 2 && mitigationValid) {
                    setStep(3);
                    setAck(false);
                  } else if (step === 3) reserve();
                }}
              >
                {error && (
                  <p className="alert" role="alert">
                    {error}
                  </p>
                )}
                {step === 0 &&
                  (candidate ? (
                    <>
                      {candidate.interactive_commands_remaining === 0 && (
                        <p role="status">
                          No interactive command capacity remains. The remaining
                          budget is reserved for independent final verification.
                        </p>
                      )}
                      <div className="coding-check-budget">
                        <div>
                          <span>Available shared commands</span>
                          <strong>{candidate.shared_commands_remaining}</strong>
                        </div>
                        <div>
                          <span>Independent final checks reserved</span>
                          <strong>
                            {candidate.independent_final_check_count}
                          </strong>
                        </div>
                        <div>
                          <span>Interactive capacity</span>
                          <strong>
                            {candidate.interactive_commands_remaining}
                          </strong>
                        </div>
                      </div>
                      <fieldset className="coding-check-list">
                        <legend>Configured checks</legend>
                        {candidate.commands.map((command) => (
                          <label
                            key={command.command_index}
                            className="coding-check-command"
                          >
                            <input
                              type="checkbox"
                              checked={selected.includes(command.command_index)}
                              onChange={(event) =>
                                setSelected(
                                  event.target.checked
                                    ? [...selected, command.command_index].sort(
                                        (a, b) => a - b,
                                      )
                                    : selected.filter(
                                        (i) => i !== command.command_index,
                                      ),
                                )
                              }
                            />
                            <span>
                              <span>
                                Check {command.command_index + 1} · Timeout{" "}
                                {command.timeout} seconds
                              </span>
                              <code>
                                {command.argv
                                  .map((arg) => JSON.stringify(arg))
                                  .join(" ")}
                              </code>
                            </span>
                          </label>
                        ))}
                      </fieldset>
                      <div className="provider-allowance-executable">
                        <Terminal size={20} aria-hidden="true" />
                        <div>
                          <strong>
                            {candidate.runner.mode === "host"
                              ? "Trusted host execution"
                              : "Configured container runner"}
                          </strong>
                          <p>
                            {candidate.runner.mode === "host"
                              ? "Commands run as the forge service account. Timeout and output limits apply. Filesystem, CPU, memory and network isolation are not provided."
                              : candidate.runner.reason}
                          </p>
                          <small>
                            Runner timeout:{" "}
                            {String(candidate.runner.configuration.timeout)}{" "}
                            seconds. Output limit:{" "}
                            {String(
                              candidate.runner.configuration.max_output_bytes,
                            )}{" "}
                            bytes.
                          </small>
                        </div>
                      </div>
                      <details>
                        <summary>Reviewed source and runner identity</summary>
                        <dl className="provider-allowance-digests">
                          <div>
                            <dt>Source fingerprint</dt>
                            <dd>{candidate.source_digest}</dd>
                          </div>
                          <div>
                            <dt>Runner fingerprint</dt>
                            <dd>{candidate.runner.digest}</dd>
                          </div>
                        </dl>
                      </details>
                    </>
                  ) : (
                    <p role="status">
                      {busy
                        ? "Loading the retained checks…"
                        : "Refresh the configured-check candidate before continuing."}
                    </p>
                  ))}
                {step === 0 && (
                  <details>
                    <summary>Advanced connection</summary>
                    <label className="field">
                      <span>Local tool port</span>
                      <input
                        type="number"
                        min="0"
                        max="65535"
                        step="1"
                        value={port}
                        onChange={(event) => {
                          setPort(event.target.value);
                          setCandidate(null);
                        }}
                      />
                      <small>
                        Use 0 to let the system choose a port, or 1024–65535 for
                        a fixed loopback port. No service configuration is
                        changed.
                      </small>
                    </label>
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() =>
                        void action(async (signal) => {
                          const c = await loadCandidate(signal);
                          if (live.current && !signal.aborted) {
                            setCandidate(c);
                            setSelected(c.command_indices);
                            setCalls(
                              String(
                                Math.min(
                                  c.command_count,
                                  c.interactive_commands_remaining,
                                ),
                              ),
                            );
                          }
                        })
                      }
                    >
                      Refresh check review
                    </button>
                  </details>
                )}
                {step === 1 && candidate && (
                  <>
                    <p>
                      {selected.length} selected checks. Up to{" "}
                      {Math.min(20, candidate.interactive_commands_remaining)}{" "}
                      interactive calls can be granted without consuming
                      independent final verification capacity.
                    </p>
                    <div className="coding-check-fields">
                      <label className="field">
                        <span>Maximum check calls</span>
                        <input
                          type="number"
                          min="1"
                          max={Math.min(
                            20,
                            candidate.interactive_commands_remaining,
                          )}
                          step="1"
                          value={calls}
                          onChange={(event) => setCalls(event.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span>Grant lifetime (seconds)</span>
                        <input
                          type="number"
                          min="30"
                          max="3600"
                          step="1"
                          value={seconds}
                          onChange={(event) => setSeconds(event.target.value)}
                        />
                        <small>
                          Expiry also applies during execution. It does not
                          extend the run budget.
                        </small>
                      </label>
                    </div>
                    <label className="field">
                      <span>Reason for granting check access</span>
                      <textarea
                        rows={4}
                        maxLength={2000}
                        value={grantReason}
                        onChange={(event) => setGrantReason(event.target.value)}
                      />
                    </label>
                    <p>
                      Granting access does not start a check or reserve the
                      final attempt. You will review and reserve that attempt
                      separately.
                    </p>
                  </>
                )}
                {step === 2 &&
                  (receipt && finalView ? (
                    <>
                      <p className="coding-check-confirmed">
                        <CheckCircle2 size={18} aria-hidden="true" />
                        Grant confirmed for {receipt.callLimit} calls until{" "}
                        {new Date(receipt.expires_at_ms).toLocaleString()}.
                      </p>
                      <div className="provider-allowance-executable">
                        <FileCheck2 size={20} aria-hidden="true" />
                        <div>
                          <strong>Current CLI selected for review</strong>
                          <code>{finalView.executablePath}</code>
                        </div>
                      </div>
                      <details>
                        <summary>
                          Current source, CLI and native failure
                        </summary>
                        <dl className="provider-allowance-digests">
                          <div>
                            <dt>Source fingerprint</dt>
                            <dd>{finalView.sourceDigest}</dd>
                          </div>
                          <div>
                            <dt>CLI fingerprint</dt>
                            <dd>{finalView.executableSha256}</dd>
                          </div>
                          <div>
                            <dt>Native fourth-attempt failure</dt>
                            <dd>{finalView.nativeProcessDigest}</dd>
                          </div>
                        </dl>
                      </details>
                      <label className="field">
                        <span>Mitigation reviewed</span>
                        <textarea
                          rows={4}
                          maxLength={2000}
                          value={reason}
                          onChange={(event) => setReason(event.target.value)}
                        />
                      </label>
                      <label className="field">
                        <span>Mitigation evidence SHA-256</span>
                        <input
                          type="text"
                          aria-label="Mitigation evidence SHA-256"
                          aria-describedby="coding-check-mitigation-help"
                          maxLength={64}
                          value={evidence}
                          onChange={(event) => setEvidence(event.target.value)}
                          spellCheck={false}
                        />
                        <small id="coding-check-mitigation-help">
                          This fingerprint records your administrative
                          attestation. It is not server-verified QA evidence. Do
                          not enter credentials.
                        </small>
                      </label>
                      <label className="check-choice provider-allowance-choice">
                        <input
                          type="checkbox"
                          checked={adopt}
                          onChange={(event) => setAdopt(event.target.checked)}
                        />
                        <span>
                          Adopt the reviewed current CLI for this final
                          recovery.
                        </span>
                      </label>
                    </>
                  ) : (
                    <p>
                      The grant is not currently confirmed. Check its saved
                      exact receipt and active status before continuing.
                    </p>
                  ))}
                {step === 3 && marker && (
                  <>
                    <dl className="provider-allowance-review">
                      <div>
                        <dt>Final dispatch</dt>
                        <dd>
                          Attempt 5 of 5. No sixth dispatch or budget reset.
                        </dd>
                      </div>
                      <div>
                        <dt>Selected checks</dt>
                        <dd>
                          <ul className="coding-check-selected">
                            {marker.grant.command_indices.map((index) => (
                              <li key={index}>
                                <strong>Check {index + 1}</strong>
                                <code>
                                  {marker.candidate.commands[index].argv.join(
                                    " ",
                                  )}
                                </code>
                              </li>
                            ))}
                          </ul>
                        </dd>
                      </div>
                      <div>
                        <dt>Check access</dt>
                        <dd>
                          Up to {marker.grant.call_limit} calls, with
                          independent final verification reserved separately.
                        </dd>
                      </div>
                      <div>
                        <dt>Mitigation</dt>
                        <dd>{reason}</dd>
                      </div>
                    </dl>
                    <label className="check-choice provider-allowance-choice">
                      <input
                        type="checkbox"
                        checked={ack}
                        onChange={(event) => setAck(event.target.checked)}
                      />
                      <span>
                        I understand this spends the last recovery attempt, even
                        if it never starts. Independent final verification is
                        still required.
                      </span>
                    </label>
                  </>
                )}
                {marker && !reserved && (
                  <details>
                    <summary>Request details</summary>
                    <dl className="provider-allowance-digests">
                      <div>
                        <dt>Grant request</dt>
                        <dd>{marker.grant.request_id}</dd>
                      </div>
                      <div>
                        <dt>Recovery request</dt>
                        <dd>{marker.grant.recovery_request_id}</dd>
                      </div>
                    </dl>
                  </details>
                )}
                <div className="wizard-actions">
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => {
                      if (marker || step === 0) close();
                      else setStep(step - 1);
                    }}
                  >
                    {marker || step === 0 ? "Close" : "Back"}
                  </button>
                  {marker && (marker.allowance || !receipt) ? (
                    <button
                      type="button"
                      className="primary"
                      disabled={busy}
                      onClick={() =>
                        void action((signal) => exact(marker, signal))
                      }
                    >
                      {busy ? "Checking…" : "Check saved exact receipt"}
                    </button>
                  ) : (
                    <button
                      type="submit"
                      className="primary"
                      disabled={
                        busy ||
                        held ||
                        (step === 0 &&
                          (!candidate ||
                            selected.length === 0 ||
                            candidate.interactive_commands_remaining === 0)) ||
                        (step === 1 && !scopeValid) ||
                        (step === 2 && !mitigationValid) ||
                        (step === 3 && (!ack || !mitigationValid))
                      }
                    >
                      {busy
                        ? "Checking…"
                        : [
                            "Continue",
                            "Grant selected checks",
                            "Review final recovery",
                            "Reserve final attempt",
                          ][step]}
                      {!busy && <ArrowRight size={16} aria-hidden="true" />}
                    </button>
                  )}
                </div>
              </form>
            </Wizard>
          )}
        </dialog>
      )}
    </section>
  );
}
