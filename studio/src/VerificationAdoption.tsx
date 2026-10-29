/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { CheckCircle2, ClipboardCheck, RefreshCw } from "lucide-react";
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
  adoptionCandidate,
  adoptionInput,
  adoptionKey,
  confirmAdoption,
  readAdoption,
  sendAdoptionOnce,
  type AdoptionCandidate,
  type AdoptionMarker,
} from "./VerificationAdoptionData";
import "./AgentCheckpointRecovery.css";
import "./FinalOutcomeRecovery.css";
import "./VerificationAdoption.css";

export function VerificationAdoption({
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
    authentication: session.user.authentication_method,
    project: run.project_id ?? null,
    repository: run.repository_id,
    run: run.id,
    request: run.worker_request!.id,
  };
  const [saved] = useState(() => {
    try {
      return {
        marker: readAdoption(
          localStorage.getItem(adoptionKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [marker, setMarker] = useState<AdoptionMarker | null>(saved.marker),
    [blocked, setBlocked] = useState(saved.blocked),
    [candidate, setCandidate] = useState<AdoptionCandidate | null>(null),
    [selected, setSelected] = useState<string[]>([]),
    [provider, setProvider] = useState(
      ["agentic", "bridge", "codex", "claude"].includes(run.agent ?? "")
        ? run.agent!
        : "agentic",
    ),
    [review, setReview] = useState(false),
    [acknowledged, setAcknowledged] = useState(false),
    [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null),
    live = useRef(true),
    heading = useRef<HTMLHeadingElement | null>(null);
  const membership = session.memberships.find(
    (value) => value.project_id === run.project_id,
  );
  const allowed =
    session.user.authentication_method !== "scm_mapping" &&
    (session.global_admin ||
      (!!membership?.permissions.includes("execute") &&
        (!run.publish || membership.permissions.includes("release")))) &&
    run.cancel_request?.status !== "requested";
  const providerAllowed =
    session.global_admin || !["codex", "claude"].includes(provider);
  const root = `/runs/${run.id}/verification-handoffs`;
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
    setReview(false);
    setAcknowledged(false);
    setConfirmed(false);
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setBlocked(true);
      setError(
        "Current execution access could not be verified. Private review details are cleared; any saved submission is preserved.",
      );
    } else
      setError(
        "The verification approval could not be confirmed. Read the saved approval before taking another action; nothing is retried automatically.",
      );
  }
  function begin() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }
  async function inspect() {
    if (!allowed || busy || (!providerAllowed && !marker)) return;
    const controller = begin();
    try {
      if (marker) {
        const receipt = await request(
          connection,
          `${root}/adoptions/${marker.body.request_id}`,
          { signal: controller.signal },
        );
        await confirmAdoption(receipt, marker);
        if (live.current && !controller.signal.aborted) setConfirmed(true);
      } else if (!blocked) {
        const found = adoptionCandidate(
          await request(
            connection,
            `${root}/candidates?agent=${encodeURIComponent(provider)}`,
            { signal: controller.signal },
          ),
          context,
          provider,
        );
        if (live.current && !controller.signal.aborted) {
          setCandidate(found);
          setSelected([]);
          setReview(false);
          setAcknowledged(false);
        }
      }
      requestAnimationFrame(() => heading.current?.focus());
    } catch (cause) {
      if (!controller.signal.aborted) failure(cause);
    } finally {
      if (live.current) setBusy(false);
    }
  }
  async function save() {
    if (
      !allowed ||
      !providerAllowed ||
      busy ||
      blocked ||
      marker ||
      !candidate ||
      !review ||
      !acknowledged
    )
      return;
    const controller = begin();
    let transportStarted = false;
    try {
      const proposed: AdoptionMarker = {
        version: 1,
        context,
        body: adoptionInput(
          candidate,
          crypto.randomUUID().replaceAll("-", ""),
          selected,
        ),
      };
      const receipt = await sendAdoptionOnce(
        localStorage,
        proposed,
        async () => {
          transportStarted = true;
          setMarker(proposed);
          return await request(connection, `${root}/adoptions`, {
            method: "POST",
            body: JSON.stringify(proposed.body),
            signal: controller.signal,
          });
        },
      );
      await confirmAdoption(receipt, proposed);
      if (live.current && !controller.signal.aborted) {
        setConfirmed(true);
        setCandidate(null);
        setReview(false);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        failure(cause);
        if (!transportStarted && live.current) {
          setBlocked(true);
          setError(
            "This browser could not retain the approval. No submission was sent. Keep any saved submission identity before restarting this review.",
          );
        }
      }
    } finally {
      if (live.current) setBusy(false);
    }
  }
  return (
    <section
      className="agent-checkpoint-recovery final-outcome-recovery verification-adoption"
      aria-label="Factory verification approval"
    >
      <div className="final-outcome-heading">
        <ClipboardCheck size={22} aria-hidden="true" />
        <h3 ref={heading} tabIndex={-1}>
          Approve factory verification
        </h3>
      </div>
      {!candidate && !marker && (
        <p>
          Approve this request's frozen checks and coding provider. Existing
          command budgets stay unchanged.
        </p>
      )}
      <ol
        className="final-outcome-steps"
        aria-label="Verification approval steps"
      >
        <li aria-current={!candidate && !marker ? "step" : undefined}>
          <span>1</span>Read the checks
        </li>
        <li aria-current={candidate && !review ? "step" : undefined}>
          <span>2</span>Choose reuse
        </li>
        <li aria-current={review || marker ? "step" : undefined}>
          <span>3</span>Review and follow
        </li>
      </ol>
      {!allowed ? (
        <p>
          Approval requires current execute access and release access when
          publishing. Host providers also require factory administrator access.
        </p>
      ) : (
        <>
          {error && (
            <p role="alert" className="alert">
              {error}
            </p>
          )}
          {blocked && (
            <p role="status">
              Keep the saved submission identity. A new approval cannot be sent
              from this review.
            </p>
          )}
          {!marker && !blocked && !candidate && (
            <label>
              Coding provider
              <select
                aria-label="Verification coding provider"
                value={provider}
                disabled={busy}
                onChange={(event) => {
                  setProvider(event.target.value);
                  setCandidate(null);
                  setSelected([]);
                  setReview(false);
                  setAcknowledged(false);
                }}
              >
                {[
                  ["agentic", "Configured AI model"],
                  ["bridge", "Agent bridge"],
                  ["codex", "Codex CLI"],
                  ["claude", "Claude Code CLI"],
                ].map(([value, label]) => (
                  <option
                    key={value}
                    value={value}
                    disabled={
                      !session.global_admin &&
                      ["codex", "claude"].includes(value)
                    }
                  >
                    {label}
                  </option>
                ))}
              </select>
            </label>
          )}
          {(marker || (!blocked && !candidate)) && (
            <button
              type="button"
              className="secondary"
              disabled={busy || (!marker && !providerAllowed)}
              onClick={() => void inspect()}
            >
              <RefreshCw size={16} />
              {marker
                ? "Read saved verification approval"
                : "Read approved checks"}
            </button>
          )}
          {candidate && (
            <div className="agent-checkpoint-review">
              <h4>
                {review
                  ? "Review this verification handoff"
                  : "Which checks may be reused later?"}
              </h4>
              <p>
                Run all {candidate.commands_count} frozen checks now. Selected
                checks may be reused in later QA when source and execution
                conditions match.
              </p>
              {!review && (
                <>
                  <p>
                    Select only source and build checks independent of time,
                    changing data and external services.
                  </p>
                  {(candidate.reusable_build_candidates ?? []).length ? (
                    <fieldset>
                      <legend>Source and build checks</legend>
                      {candidate.reusable_build_candidates!.map((check) => (
                        <label
                          className="verification-reuse-choice"
                          key={check.command_digest}
                        >
                          <input
                            type="checkbox"
                            checked={selected.includes(check.command_digest)}
                            onChange={(event) => {
                              setSelected((current) =>
                                event.target.checked
                                  ? [...current, check.command_digest]
                                  : current.filter(
                                      (value) => value !== check.command_digest,
                                    ),
                              );
                              setAcknowledged(false);
                            }}
                          />
                          <span>
                            Check {check.command_index}
                            <code>{check.argv.join(" ")}</code>
                          </span>
                        </label>
                      ))}
                    </fieldset>
                  ) : (
                    <p>
                      No source-build candidates are available for this runner.
                      The checks will use fresh executions.
                    </p>
                  )}
                  <div className="worker-dialogue-actions">
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => {
                        setCandidate(null);
                        setSelected([]);
                      }}
                    >
                      Change provider
                    </button>
                    <button
                      type="button"
                      className="primary"
                      onClick={() => setReview(true)}
                    >
                      Review verification approval
                    </button>
                  </div>
                </>
              )}
              {review && (
                <>
                  <p>
                    {selected.length} source{" "}
                    {selected.length === 1 ? "check" : "checks"} selected for
                    possible reuse. Unselected checks run fresh. Earlier results
                    cannot be made reusable retroactively.
                  </p>
                  <label className="verification-reuse-choice">
                    <input
                      type="checkbox"
                      checked={acknowledged}
                      onChange={(event) =>
                        setAcknowledged(event.target.checked)
                      }
                    />
                    <span>
                      I approve the frozen checks. Each selected check is
                      independent of time and external state.
                    </span>
                  </label>
                  <div className="worker-dialogue-actions">
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() => {
                        setReview(false);
                        setAcknowledged(false);
                      }}
                    >
                      Edit selection
                    </button>
                    <button
                      type="button"
                      className="primary"
                      disabled={busy || !acknowledged}
                      onClick={() => void save()}
                    >
                      Save verification approval
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
          {confirmed && (
            <div role="status" className="record-evidence">
              <h4 className="final-outcome-heading">
                <CheckCircle2 size={18} />
                Verification approval recorded
              </h4>
              <p>
                The native approval is saved. It does not establish passed
                tests. Follow the run's Verification tab and journal for
                execution results.
              </p>
            </div>
          )}
        </>
      )}
    </section>
  );
}
