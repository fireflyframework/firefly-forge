/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  GraduationCap,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
import {
  learningPayload,
  readLearningSettings,
  type LearningSettings,
  type LearningValues,
} from "./LearningSettings";
import "./LearningConfiguration.css";
const message = (cause: unknown) =>
  (cause instanceof Error
    ? cause.message
    : "Learning settings could not be read."
  ).slice(0, 800);
export function LearningConfiguration({
  connection,
  session,
}: {
  connection: Connection;
  session: IdentitySession | null;
}) {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Learning</h1>
          <p>Choose how new runs reuse verified project knowledge.</p>
        </div>
      </div>
      {session?.global_admin ? (
        <LearningAdministrator
          key={`${connection.base}:${session.user.id}`}
          connection={connection}
        />
      ) : (
        <section className="access-notice">
          <ShieldCheck size={22} />
          <div>
            <h2>Factory administrator access required</h2>
            <p>Ask a factory administrator to change learning settings.</p>
          </div>
        </section>
      )}
    </>
  );
}
function LearningAdministrator({ connection }: { connection: Connection }) {
  const [saved, setSaved] = useState<LearningSettings | null>(null),
    [draft, setDraft] = useState<LearningValues | null>(null),
    [busy, setBusy] = useState(true),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [uncertain, setUncertain] = useState(false),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0);
  const active = useRef<AbortController | null>(null),
    alert = useRef<HTMLDivElement>(null);
  const reload = useCallback(async () => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = readLearningSettings(
        await request<unknown>(connection, "/learning/policy", {
          signal: controller.signal,
        }),
      );
      if (controller.signal.aborted) return;
      setSaved(value);
      setDraft({ ...value.settings });
      setUncertain(false);
      setEditing(false);
      setStep(0);
    } catch (cause) {
      if (!controller.signal.aborted) setError(message(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [connection]);
  useEffect(() => {
    void reload();
    return () => active.current?.abort();
  }, [reload]);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  function edit() {
    if (!saved || busy || uncertain) return;
    setDraft({ ...saved.settings });
    setEditing(true);
    setStep(0);
    setNotice("");
    setError("");
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!saved || !draft || busy || uncertain) return;
    if (step < 2) {
      setError("");
      setStep(step + 1);
      return;
    }
    let values: Record<string, boolean | number>;
    try {
      values = learningPayload(saved, draft);
    } catch (cause) {
      setError(message(cause));
      setStep(1);
      return;
    }
    if (
      JSON.stringify(values) ===
      JSON.stringify(learningPayload(saved, saved.settings))
    ) {
      setNotice("No learning settings changed.");
      setEditing(false);
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = readLearningSettings(
        await request<unknown>(connection, "/learning/policy", {
          method: "PUT",
          body: JSON.stringify(values),
          signal: controller.signal,
        }),
      );
      if (controller.signal.aborted) return;
      setSaved(value);
      setDraft({ ...value.settings });
      setEditing(false);
      setNotice("Learning settings saved. New runs will capture this policy.");
    } catch (cause) {
      if (controller.signal.aborted) return;
      const rejected =
        cause instanceof ApiError &&
        cause.status >= 400 &&
        cause.status < 500 &&
        cause.status !== 408;
      setUncertain(!rejected);
      setError(
        rejected
          ? message(cause)
          : "The save outcome is unknown. Reload saved settings before making another change.",
      );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  const descriptions = [
    "Select the verified knowledge a new run may reuse.",
    "Keep retrieval bounded and reject stale evidence.",
    "Review the policy before saving it for future runs.",
  ];
  return (
    <div className="learning-configuration">
      {error && (
        <div className="alert" role="alert" tabIndex={-1} ref={alert}>
          {error}
        </div>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {busy && !saved && <p role="status">Loading learning settings…</p>}
      {!editing && saved && (
        <section className="integration-card learning-state">
          <div className="section-heading">
            <div>
              <h2>Saved learning policy</h2>
              <p>
                Applies to new runs. Existing runs keep their captured policy.
              </p>
            </div>
            <GraduationCap size={28} />
          </div>
          <PolicySummary values={saved.settings} />
          <div className="learning-actions">
            <button
              className="primary"
              disabled={busy || uncertain}
              onClick={edit}
            >
              Edit learning settings
              <ArrowRight size={16} />
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void reload()}
            >
              <RefreshCw size={16} />
              Reload saved settings
            </button>
          </div>
        </section>
      )}
      {editing && saved && draft && (
        <Wizard
          steps={["Knowledge", "Limits", "Review"]}
          active={step}
          onStep={setStep}
          locked={busy || uncertain}
          className="learning-wizard"
          title={
            [
              "What should a run learn from?",
              "How much knowledge can a run retrieve?",
              "Review learning settings",
            ][step]
          }
          description={descriptions[step]}
          guide={
            <>
              <SetupIllustration kind="learning" step={step} />
              <h3>
                {
                  [
                    "Verified evidence",
                    "Bounded retrieval",
                    "Captured per run",
                  ][step]
                }
              </h3>
              <p>
                {
                  [
                    "Evidence must match the current project, repository, framework version and dependencies. Invalidated or expired records are excluded.",
                    "The factory checks eligibility again before accepting a result. Changing these limits does not grant access to another workspace.",
                    "Saving changes creates a new policy revision. Runs already admitted keep their original settings and evidence.",
                  ][step]
                }
              </p>
            </>
          }
        >
          <form onSubmit={(event) => void submit(event)}>
            {step === 0 && (
              <>
                <label className="learning-choice">
                  <input
                    type="checkbox"
                    aria-label="Enable verified learning"
                    checked={draft.enabled}
                    onChange={(e) =>
                      setDraft({ ...draft, enabled: e.target.checked })
                    }
                    disabled={busy || uncertain}
                  />
                  Enable verified learning
                  <span>
                    Record verified procedures and reuse eligible project
                    knowledge.
                  </span>
                </label>
                <label className="learning-choice">
                  <input
                    type="checkbox"
                    aria-label="Read verified learning from flycanon"
                    checked={draft.projected_retrieval}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        projected_retrieval: e.target.checked,
                      })
                    }
                    disabled={
                      !saved.projectedSupported ||
                      !draft.enabled ||
                      busy ||
                      uncertain
                    }
                  />
                  Read verified learning from flycanon
                  <span>
                    Read cited projections of eligible records using the
                    existing scoped connection.
                  </span>
                </label>
                {!saved.projectedSupported && (
                  <p className="learning-help">
                    Upgrade the server to enable verified projection retrieval.
                  </p>
                )}
                <p className="learning-help">
                  Projection reads require verified learning to be enabled. With
                  learning disabled, the factory can still search configured
                  project and framework knowledge. These settings do not submit,
                  promote or invalidate a learning record.
                </p>
              </>
            )}
            {step === 1 && (
              <>
                <div className="learning-limits">
                  {(
                    [
                      {
                        key: "max_age_hours",
                        label: "Maximum age (hours)",
                        max: 720,
                        help: "Reject records older than this age.",
                      },
                      {
                        key: "max_candidates",
                        label: "Source results per lookup",
                        max: 16,
                        help: "Limit project and framework source results when no exact procedure matches.",
                      },
                      {
                        key: "max_lookup_calls",
                        label: "Lookups per run",
                        max: 100,
                        help: "Limit knowledge-first requests in each run.",
                      },
                    ] as const
                  ).map((item) => (
                    <div className="field" key={item.key}>
                      <label htmlFor={`learning-${item.key}`}>
                        {item.label}
                      </label>
                      <input
                        id={`learning-${item.key}`}
                        type="number"
                        min={1}
                        max={item.max}
                        step={1}
                        required
                        value={
                          Number.isFinite(draft[item.key])
                            ? draft[item.key]
                            : ""
                        }
                        disabled={busy || uncertain}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            [item.key]: e.target.valueAsNumber,
                          })
                        }
                        aria-describedby={`learning-help-${item.key}`}
                      />
                      <small id={`learning-help-${item.key}`}>
                        {item.help}
                      </small>
                    </div>
                  ))}
                </div>
                <p className="learning-help">
                  The saved repeated-attempt setting is preserved. Automated
                  repair-loop enforcement remains a separate capability.
                </p>
              </>
            )}
            {step === 2 && (
              <>
                <PolicySummary values={draft} />
                <p className="learning-help">
                  Applies to new runs. Existing runs keep their captured policy.
                </p>
              </>
            )}
            <div className="wizard-actions">
              <div>
                <button
                  className="secondary"
                  type="button"
                  disabled={busy || uncertain}
                  onClick={() => (step ? setStep(step - 1) : setEditing(false))}
                >
                  <ArrowLeft size={16} />
                  {step ? "Back" : "Cancel"}
                </button>
                {uncertain && (
                  <button
                    className="secondary"
                    type="button"
                    disabled={busy}
                    onClick={() => void reload()}
                  >
                    <RefreshCw size={16} />
                    Reload saved settings
                  </button>
                )}
              </div>
              <button className="primary" disabled={busy || uncertain}>
                {busy
                  ? "Saving…"
                  : step === 2
                    ? "Save learning settings"
                    : "Continue"}
                <ArrowRight size={16} />
              </button>
            </div>
          </form>
        </Wizard>
      )}
      {!saved && !busy && (
        <button className="secondary" onClick={() => void reload()}>
          <RefreshCw size={16} />
          Reload saved settings
        </button>
      )}
    </div>
  );
}
function PolicySummary({ values }: { values: LearningValues }) {
  return (
    <dl className="learning-summary">
      <div>
        <dt>Verified learning</dt>
        <dd>{values.enabled ? "Enabled" : "Disabled"}</dd>
      </div>
      <div>
        <dt>flycanon projection reads</dt>
        <dd>
          {values.projected_retrieval
            ? values.enabled
              ? "Enabled"
              : "Inactive while learning is disabled"
            : "Disabled"}
        </dd>
      </div>
      <div>
        <dt>Maximum age</dt>
        <dd>{values.max_age_hours} hours</dd>
      </div>
      <div>
        <dt>Source results per lookup</dt>
        <dd>{values.max_candidates}</dd>
      </div>
      <div>
        <dt>Lookups per run</dt>
        <dd>{values.max_lookup_calls}</dd>
      </div>
    </dl>
  );
}
