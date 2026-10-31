/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ProjectSnapshot,
} from "./api";
import { Wizard } from "./Wizard";
import type { IdentitySession } from "./Identity";
import type { PollingBindingReceipt } from "./ScmPollingData";
import {
  confirmFunctionalReceipt,
  functionalInput,
  functionalKey,
  readFunctionalAttempt,
  type FunctionalAttempt,
  type FunctionalReview,
} from "./ScmPollFunctionalData";

export function ScmPollFunctionalConsent({
  connection,
  session,
  project,
  binding,
  personName,
}: {
  connection: Connection;
  session: IdentitySession;
  project: ProjectSnapshot;
  binding: PollingBindingReceipt;
  personName: string;
}) {
  const scope = useMemo(
    () => ({
      base: normalizeBase(connection.base),
      actor: session.user.id,
      project: project.id,
      repository: binding.repository_id,
      binding: binding.request.request_id,
    }),
    [
      connection.base,
      session.user.id,
      project.id,
      binding.repository_id,
      binding.request.request_id,
    ],
  );
  const key = functionalKey(scope),
    path = `/projects/${project.id}/repositories/${binding.repository_id}/scm-polling/functional-grants`;
  const [review, setReview] = useState<FunctionalReview | null>(null),
    [previous, setPrevious] = useState<string | null>(null),
    [attempt, setAttempt] = useState<FunctionalAttempt | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [ack, setAck] = useState(false),
    [step, setStep] = useState(0),
    [turns, setTurns] = useState(0),
    [minutes, setMinutes] = useState(15),
    [corrupt, setCorrupt] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => {
    pending.current?.abort();
    pending.current = null;
    setBusy(false);
    setReview(null);
    setStep(0);
    setAck(false);
    setNotice("");
    setError("");
    setCorrupt(false);
    try {
      setAttempt(readFunctionalAttempt(sessionStorage.getItem(key), scope));
    } catch {
      setCorrupt(true);
      setError(
        "A retained functional request is invalid. Preserve it for operator review; no new consent can be sent.",
      );
    }
    return () => pending.current?.abort();
  }, [key, scope, connection.token, session.global_admin]);
  if (!session.global_admin) return null;
  async function read() {
    if (pending.current || attempt) return;
    const control = new AbortController();
    pending.current = control;
    setBusy(true);
    setError("");
    setReview(null);
    setStep(0);
    setAck(false);
    try {
      const data = await request<FunctionalReview>(
        connection,
        path + "/review?binding_request_id=" + scope.binding,
        { signal: control.signal },
      );
      if (
        data.binding.digest !== binding.digest ||
        data.project_binding_digest !== binding.project_binding_digest
      )
        throw new Error();
      functionalInput(data, "0".repeat(32), null, 0, 15, true);
      let digest: string | null = null;
      try {
        const current = await request<unknown>(
          connection,
          path + "/current/" + scope.binding,
          { signal: control.signal },
        );
        digest = await confirmFunctionalReceipt(current, scope);
      } catch (cause) {
        if (!(cause instanceof ApiError && cause.status === 404)) throw cause;
      }
      if (!control.signal.aborted) {
        setPrevious(digest);
        setReview(data);
      }
    } catch {
      if (!control.signal.aborted)
        setError(
          "Current issue, account or model policy could not be verified. No consent was sent.",
        );
    } finally {
      if (pending.current === control) {
        pending.current = null;
        setBusy(false);
      }
    }
  }
  async function recover() {
    if (!attempt || pending.current) return;
    const control = new AbortController();
    pending.current = control;
    setBusy(true);
    setError("");
    try {
      const value = await request<unknown>(
        connection,
        path + "/requests/" + attempt.input.request_id,
        { signal: control.signal },
      );
      await confirmFunctionalReceipt(value, scope, attempt.input);
      if (!control.signal.aborted) {
        sessionStorage.removeItem(key);
        setAttempt(null);
        setReview(null);
        setAck(false);
        setNotice(
          "Exact functional consent confirmed. Review current scope before any further change.",
        );
      }
    } catch {
      if (!control.signal.aborted)
        setError(
          "The original result remains unconfirmed. Keep its request ID; do not resend or replace it.",
        );
    } finally {
      if (pending.current === control) {
        pending.current = null;
        setBusy(false);
      }
    }
  }
  async function save(active: boolean) {
    if (!review || !ack || pending.current || attempt || error) return;
    const control = new AbortController();
    pending.current = control;
    setBusy(true);
    setError("");
    try {
      const input = functionalInput(
        review,
        crypto.randomUUID().replaceAll("-", ""),
        previous,
        turns,
        minutes,
        active,
      );
      const marker: FunctionalAttempt = { version: 1, scope, input };
      const raw = JSON.stringify(marker);
      sessionStorage.setItem(key, raw);
      if (sessionStorage.getItem(key) !== raw) throw new Error();
      setAttempt(marker);
      const value = await request<unknown>(connection, path, {
        method: "POST",
        body: JSON.stringify(input),
        signal: control.signal,
      });
      await confirmFunctionalReceipt(value, scope, input);
      if (!control.signal.aborted) {
        sessionStorage.removeItem(key);
        setAttempt(null);
        setReview(null);
        setAck(false);
        setNotice(
          active
            ? "Separate functional consent recorded. No model call or run was started."
            : "Functional consent revoked. Historical receipts remain preserved.",
        );
      }
    } catch {
      if (!control.signal.aborted)
        setError(
          "Consent outcome unconfirmed. Read the exact retained request before any further change.",
        );
    } finally {
      if (pending.current === control) {
        pending.current = null;
        setBusy(false);
      }
    }
  }
  return (
    <section
      className="scm-resume-consent"
      aria-label="GitHub functional assistant consent"
    >
      <h3>GitHub functional assistant</h3>
      <p>
        Separate consent for {personName} on issue #
        {binding.verified_reference.issue_number}. Existing status, answer and
        resume permissions remain unchanged. Starting and reading this
        conversation use no model.
      </p>
      <button
        type="button"
        className="secondary"
        disabled={busy || Boolean(attempt) || corrupt}
        onClick={() => void read()}
      >
        Review functional scope
      </button>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {attempt && (
        <>
          <p>
            Original request: <code>{attempt.input.request_id}</code>. Do not
            resend it.
          </p>
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => void recover()}
          >
            Read original functional result
          </button>
        </>
      )}
      {review && !attempt && (
        <Wizard
          steps={["Scope", "Allowance", "Confirm"]}
          active={step}
          onStep={(next) => setStep(Math.min(step, next))}
          title="Review functional consent"
          description="This separate decision does not start a model or alter the CRM run."
          locked={busy}
        >
          <div className="wizard-fields">
            {step === 0 && (
              <>
                <p>
                  The complete frozen project graph is in scope:{" "}
                  {project.definition.name}. Components:{" "}
                  {project.definition.components
                    .map((item) => item.id)
                    .join(", ")}
                  . Every member repository must retain current actor
                  permission.
                </p>
                <p>
                  Profile <code>{review.model_profile_id}</code>, revision{" "}
                  {review.model_profile_revision}. Current source, model,
                  runtime and native permission are checked again before
                  dispatch and feedback.
                </p>
              </>
            )}
            {step === 1 && (
              <>
                <label className="field">
                  Model-capable turns
                  <select
                    value={turns}
                    onChange={(e) => setTurns(Number(e.target.value))}
                  >
                    {[0, 1, 2, 3].map((n) => (
                      <option key={n} value={n}>
                        {n === 0 ? "None: start and status only" : n}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  Consent duration
                  <select
                    value={minutes}
                    onChange={(e) => setMinutes(Number(e.target.value))}
                  >
                    {[15, 30, 60].map((n) => (
                      <option key={n} value={n}>
                        {n} minutes
                      </option>
                    ))}
                  </select>
                </label>
                <p>
                  An uncertain provider outcome retains its reserved turn and is
                  never automatically retried or refunded.
                </p>
              </>
            )}
            {step === 2 && (
              <>
                <p>
                  Issue #{binding.verified_reference.issue_number}: model-free
                  start/status
                  {turns > 0
                    ? ` and at most ${turns} model-capable turns`
                    : " only"}
                  , expiring in {minutes} minutes. Proposals remain unreviewed.
                </p>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={ack}
                    onChange={(e) => setAck(e.target.checked)}
                  />
                  I authorize only these assistant actions and this separate
                  allowance. It does not change the original CRM run or its
                  budget, apply proposals, push code or publish.
                </label>
              </>
            )}
          </div>
          <div className="wizard-actions">
            {step > 0 && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => setStep(step - 1)}
              >
                Back
              </button>
            )}
            {step < 2 ? (
              <button
                type="button"
                className="primary"
                disabled={busy || Boolean(error)}
                onClick={() => setStep(step + 1)}
              >
                Continue
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className="primary"
                  disabled={!ack || busy || Boolean(error)}
                  onClick={() => void save(true)}
                >
                  Record separate consent
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={!ack || busy || Boolean(error) || previous === null}
                  onClick={() => void save(false)}
                >
                  Revoke functional consent
                </button>
              </>
            )}
          </div>
        </Wizard>
      )}
    </section>
  );
}
