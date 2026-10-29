/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Clock3,
  FolderGit2,
  Loader2,
  MonitorPlay,
  RefreshCw,
  Server,
  ShieldCheck,
  Terminal,
} from "lucide-react";
import { ApiError, request, type Connection, type Repository } from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import {
  readRunnerSettings,
  runnerDraft,
  runnerPayload,
  type RunnerDraft,
  type RunnerSettings,
} from "./RunnerSettings";
import "./RunnerSetup.css";

export function RunnerSetup({
  connection,
  session,
  repositories,
  onAgents,
}: {
  connection: Connection;
  session: IdentitySession | null;
  repositories: Repository[];
  onAgents: () => void;
}) {
  const [repository, setRepository] = useState("");
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Runners & processes</h1>
          <p>
            Choose where new runs execute and how agents can wait for their
            work.
          </p>
        </div>
      </div>
      {!session?.global_admin ? (
        <section className="access-notice">
          <ShieldCheck size={24} />
          <div>
            <h2>Factory administrator access required</h2>
            <p>Ask an administrator to configure repository runners.</p>
          </div>
        </section>
      ) : (
        <div className="runner-setup">
          <div className="field runner-repository">
            <label htmlFor="runner-repository">
              <FolderGit2 size={17} /> Repository
            </label>
            <select
              id="runner-repository"
              value={repository}
              onChange={(e) => setRepository(e.target.value)}
            >
              <option value="">Choose a repository</option>
              {repositories.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            {!repositories.length && (
              <p>
                Register a repository through Connections before configuring its
                runner.
              </p>
            )}
          </div>
          {repository ? (
            <RepositoryRunner
              key={`${connection.base}:${session.user.id}:${repository}`}
              connection={connection}
              repository={repository}
              onAgents={onAgents}
            />
          ) : (
            <section className="panel runner-introduction">
              <Server size={28} />
              <h2>Start with a repository</h2>
              <p>
                Runner settings apply to future runs in that repository.
                Existing executions keep their saved policy, tools and budgets.
              </p>
              <ol>
                <li>Choose the execution environment.</li>
                <li>Review process monitoring and resource settings.</li>
                <li>Save, then check worker availability.</li>
              </ol>
            </section>
          )}
        </div>
      )}
    </>
  );
}

function RepositoryRunner({
  connection,
  repository,
  onAgents,
}: {
  connection: Connection;
  repository: string;
  onAgents: () => void;
}) {
  const [saved, setSaved] = useState<RunnerSettings | null>(null),
    [draft, setDraft] = useState<RunnerDraft | null>(null);
  const [step, setStep] = useState(0),
    [busy, setBusy] = useState(true),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [uncertain, setUncertain] = useState(false);
  const [probe, setProbe] = useState<{
    available: boolean;
    detail: string;
  } | null>(null);
  const active = useRef<AbortController | null>(null),
    alert = useRef<HTMLDivElement>(null);
  const path = `/repositories/${repository}/runner`;
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    void request<unknown>(connection, path, { signal: controller.signal })
      .then((value) => {
        const settings = readRunnerSettings(value, repository);
        if (!controller.signal.aborted) {
          setSaved(settings);
          if (settings.sandbox.mode !== "pool") setDraft(runnerDraft(settings));
        }
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setError(
            "Runner settings could not be loaded. Reload before making changes.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => active.current?.abort();
  }, [connection, path, repository]);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  function controller() {
    active.current?.abort();
    const next = new AbortController();
    active.current = next;
    setBusy(true);
    setError("");
    return next;
  }
  async function reload() {
    const next = controller();
    try {
      const value = readRunnerSettings(
        await request<unknown>(connection, path, { signal: next.signal }),
        repository,
      );
      if (next.signal.aborted) return;
      setSaved(value);
      setDraft(value.sandbox.mode === "pool" ? null : runnerDraft(value));
      setUncertain(false);
      setProbe(null);
      setStep(0);
      setNotice("Saved settings reloaded. Review them before saving again.");
    } catch {
      if (!next.signal.aborted)
        setError(
          "Runner settings could not be reloaded. No further save will be attempted.",
        );
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!saved || !draft || busy || uncertain) return;
    let payload: ReturnType<typeof runnerPayload>;
    try {
      payload = runnerPayload(saved, draft);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Review the runner settings.",
      );
      return;
    }
    if (step < 2) {
      setStep(step + 1);
      setError("");
      return;
    }
    const next = controller();
    let sent = false;
    try {
      const current = readRunnerSettings(
        await request<unknown>(connection, path, { signal: next.signal }),
        repository,
      );
      if (next.signal.aborted) return;
      if (JSON.stringify(current) !== JSON.stringify(saved))
        throw new Error(
          "Saved runner settings changed. Reload and review the current version.",
        );
      sent = true;
      const result = readRunnerSettings(
        await request<unknown>(connection, path, {
          method: "PUT",
          body: JSON.stringify(payload),
          signal: next.signal,
        }),
        repository,
      );
      if (next.signal.aborted) return;
      setSaved(result);
      setDraft(runnerDraft(result));
      setProbe(null);
      setNotice(
        "Runner settings saved for future runs. Check availability, then review agent tool requests.",
      );
    } catch (cause) {
      if (next.signal.aborted) return;
      if (
        sent &&
        !(
          cause instanceof ApiError &&
          cause.status >= 400 &&
          cause.status < 500
        )
      ) {
        setUncertain(true);
        setError(
          "The save result is unknown. Reload saved settings before making another request.",
        );
      } else
        setError(
          sent
            ? "The server refused these settings. Review the runner policy before saving again."
            : cause instanceof Error
              ? cause.message
              : "Current settings could not be checked. Nothing was saved.",
        );
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  }
  async function check() {
    const next = controller();
    setProbe(null);
    try {
      const value = await request<{
        repository_id: string;
        revision: string | null;
        execution: { available: boolean };
        tools: unknown[];
      }>(connection, path + "/readiness", { signal: next.signal });
      if (next.signal.aborted) return;
      if (
        value.repository_id !== repository ||
        value.revision !== saved?.revision ||
        typeof value.execution?.available !== "boolean"
      )
        throw new Error();
      setProbe({
        available: value.execution.available,
        detail: value.execution.available
          ? "The worker can use this runner. Project dependencies and application readiness still need their own checks."
          : "This API has not verified worker availability. Check the execution worker and its runner prerequisites.",
      });
    } catch {
      if (!next.signal.aborted)
        setError(
          "Worker availability could not be verified for this saved revision. Reload settings and try the check again.",
        );
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  }
  return (
    <>
      {error && (
        <div className="error-banner" role="alert" ref={alert} tabIndex={-1}>
          {error}
        </div>
      )}
      {notice && (
        <div className="success-banner" role="status">
          <Check size={18} />
          {notice}
        </div>
      )}
      <div className="runner-toolbar">
        <button
          type="button"
          className="secondary"
          onClick={() => void reload()}
          disabled={busy}
        >
          <RefreshCw size={16} />
          Reload saved settings
        </button>
        {busy && (
          <span role="status">
            <Loader2 className="spin" size={16} /> Working
          </span>
        )}
      </div>
      {saved?.sandbox.mode === "pool" && (
        <section className="panel runner-introduction">
          <Server size={24} />
          <h2>Managed pool runner</h2>
          <p>
            This repository uses a saved pool profile. This form preserves that
            selection. Process monitoring is currently available only for
            explicitly configured host runners.
          </p>
        </section>
      )}
      {saved && draft && (
        <Wizard
          className="runner-wizard"
          steps={["Execution", "Processes", "Review"]}
          active={step}
          onStep={setStep}
          locked={busy || uncertain}
          title={
            [
              "Where should this repository run?",
              "What can agents wait for?",
              "Review runner settings",
            ][step]
          }
          description={
            [
              "Changes apply when a new run captures this repository's policy.",
              "Process monitoring requires explicit tool requests for each new run.",
              "Saving does not change existing executions or install project dependencies.",
            ][step]
          }
          guide={
            <>
              <div className="runner-guide-heading">
                <Server size={24} />
                <h3>
                  {draft.mode === "host"
                    ? "Trusted local execution"
                    : "Docker execution"}
                </h3>
              </div>
              <p>
                {draft.mode === "host"
                  ? "Commands use the factory service account. CPU, memory and network isolation are unavailable. Use a trusted development machine."
                  : "The worker uses the exact saved image and Docker context. It never falls back to host execution."}
              </p>
              <div className="runner-process-flow">
                <span>
                  <Terminal size={18} />
                  Start
                </span>
                <ArrowRight size={16} />
                <span>
                  <Clock3 size={18} />
                  Wait
                </span>
                <ArrowRight size={16} />
                <span>
                  <Check size={18} />
                  React
                </span>
              </div>
              <p>Wait for process exit, a log message or a workspace file.</p>
              <details>
                <summary>Supported execution</summary>
                <p>
                  Available for trusted host runs without required checkpoints.
                  Distributed monitoring and durable agent parking are not
                  enabled by this setting. A wait timeout preserves the process.
                  A chosen work deadline stops it. Explicit cancellation or
                  invocation shutdown also drains it.
                </p>
              </details>
            </>
          }
        >
          <form onSubmit={(event) => void submit(event)}>
            {step === 0 && (
              <>
                <div className="field">
                  <label htmlFor="runner-mode">Execution environment</label>
                  <select
                    id="runner-mode"
                    disabled={busy || uncertain}
                    value={draft.mode}
                    onChange={(e) => {
                      setDraft({
                        ...draft,
                        mode: e.target.value as RunnerDraft["mode"],
                        monitor: false,
                      });
                      setProbe(null);
                    }}
                  >
                    <option value="host">Trusted host</option>
                    <option value="docker">Docker</option>
                  </select>
                </div>
                {draft.mode === "docker" && (
                  <>
                    <div className="field">
                      <label htmlFor="runner-image">
                        Image with SHA256 digest
                      </label>
                      <input
                        id="runner-image"
                        value={draft.image}
                        onChange={(e) =>
                          setDraft({ ...draft, image: e.target.value })
                        }
                        placeholder="registry/image@sha256:…"
                        disabled={busy || uncertain}
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="runner-context">Docker context</label>
                      <input
                        id="runner-context"
                        value={draft.context}
                        onChange={(e) =>
                          setDraft({ ...draft, context: e.target.value })
                        }
                        disabled={busy || uncertain}
                      />
                    </div>
                  </>
                )}
                <div className="runner-field-grid">
                  <div className="field">
                    <label className="check-choice">
                      <input
                        type="checkbox"
                        checked={draft.deadlineEnabled}
                        disabled={busy || uncertain}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            deadlineEnabled: e.target.checked,
                          })
                        }
                      />
                      <span>Set a work deadline</span>
                    </label>
                    {draft.deadlineEnabled ? (
                      <>
                        <label htmlFor="runner-deadline">
                          Stop after (seconds)
                        </label>
                        <input
                          id="runner-deadline"
                          inputMode="numeric"
                          value={draft.deadline}
                          onChange={(e) =>
                            setDraft({ ...draft, deadline: e.target.value })
                          }
                          disabled={busy || uncertain}
                        />
                      </>
                    ) : (
                      <p className="runner-help">
                        Continue until completion or explicit cancellation.
                      </p>
                    )}
                  </div>
                  <div className="field">
                    <label htmlFor="runner-output">Log size (bytes)</label>
                    <input
                      id="runner-output"
                      inputMode="numeric"
                      value={draft.output}
                      onChange={(e) =>
                        setDraft({ ...draft, output: e.target.value })
                      }
                      disabled={busy || uncertain}
                    />
                  </div>
                </div>
                <p className="runner-help">
                  Work has no time limit by default. Owner heartbeats detect
                  abandoned processes separately. Other saved controls stay
                  unchanged; switching to host removes Docker-only settings.
                </p>
              </>
            )}
            {step === 1 && (
              <>
                <label className="check-choice runner-monitor-choice">
                  <input
                    type="checkbox"
                    checked={draft.monitor}
                    disabled={draft.mode !== "host" || busy || uncertain}
                    onChange={(e) =>
                      setDraft({ ...draft, monitor: e.target.checked })
                    }
                  />
                  <span>
                    <strong>Enable process monitoring</strong>
                    <span>
                      Let an agent start a process, read its logs and wait for a
                      condition.
                    </span>
                  </span>
                  <MonitorPlay size={22} />
                </label>
                <p className="runner-help">
                  {draft.mode === "host"
                    ? "Available for trusted host runs without required checkpoints. Each tool also needs a fresh role grant and command execution permission. Saving this option does not grant those tools."
                    : "This monitor is unavailable for Docker and pool runners. Choosing Docker keeps it disabled."}
                </p>
                <p className="runner-help">
                  Wait for process exit, a log message or a workspace file.
                  Inspect the real exit code and bounded logs before continuing.
                </p>
              </>
            )}
            {step === 2 && (
              <>
                <dl className="wizard-summary">
                  <div>
                    <dt>Environment</dt>
                    <dd>{draft.mode === "host" ? "Trusted host" : "Docker"}</dd>
                  </div>
                  <div>
                    <dt>Process monitoring</dt>
                    <dd>
                      {draft.monitor
                        ? "Enabled for eligible future runs"
                        : "Disabled"}
                    </dd>
                  </div>
                  <div>
                    <dt>Work deadline</dt>
                    <dd>
                      {draft.deadlineEnabled
                        ? `After ${Number(draft.deadline).toLocaleString()} seconds`
                        : "Until completion or cancellation"}
                    </dd>
                  </div>
                  <div>
                    <dt>Log size</dt>
                    <dd>{Number(draft.output).toLocaleString()} bytes</dd>
                  </div>
                  {draft.mode === "docker" && (
                    <>
                      <div>
                        <dt>Image</dt>
                        <dd>{draft.image}</dd>
                      </div>
                      <div>
                        <dt>Context</dt>
                        <dd>{draft.context}</dd>
                      </div>
                    </>
                  )}
                </dl>
                <p className="runner-help">
                  Before saving, forge rereads this revision. The server
                  currently has no conditional update support, so coordinate
                  concurrent administration changes.
                </p>
              </>
            )}
            <div className="wizard-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setStep(step - 1)}
                disabled={step === 0 || busy || uncertain}
              >
                <ArrowLeft size={16} />
                Back
              </button>
              <button
                type="submit"
                className="primary"
                disabled={busy || uncertain}
              >
                {busy && <Loader2 className="spin" size={16} />}
                {step === 2 ? "Save runner settings" : "Continue"}
                {step < 2 && <ArrowRight size={16} />}
              </button>
            </div>
          </form>
        </Wizard>
      )}
      {saved && (
        <section className="panel runner-check">
          <div>
            <h2>Check the saved runner</h2>
            <p>
              This checks worker availability. It does not start a project
              server or prove its dependencies are installed.
            </p>
          </div>
          <div className="runner-toolbar">
            <button
              type="button"
              className="secondary"
              onClick={() => void check()}
              disabled={busy || uncertain}
            >
              Check availability
            </button>
            <button type="button" className="quiet" onClick={onAgents}>
              Review agent tools
              <ArrowRight size={16} />
            </button>
          </div>
          {probe && (
            <p role="status">
              <ShieldCheck size={18} />
              {probe.detail}
            </p>
          )}
        </section>
      )}
    </>
  );
}
