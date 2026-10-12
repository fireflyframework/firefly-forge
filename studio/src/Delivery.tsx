/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import {
  parseCommands,
  post,
  request,
  type Connection,
  type ForgeRun,
  type Repository,
} from "./api";
import { Wizard } from "./Wizard";
import type { DeploymentJob } from "./factory";

type Target = {
  id: string;
  name: string;
  repository_id: string;
  commands: { argv: string[] }[];
  verification: { argv: string[] }[];
  rollback_commands: { argv: string[] }[];
  rollback_verification: { argv: string[] }[];
};
const message = (cause: unknown) =>
  cause instanceof Error ? cause.message : "Deployment request failed.";

export function DeliverySetup({
  connection,
  connected,
  repositories,
}: {
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
}) {
  const [step, setStep] = useState(0);
  const [targets, setTargets] = useState<Target[]>([]);
  const [name, setName] = useState("");
  const [identifier, setIdentifier] = useState("");
  const [project, setProject] = useState("");
  const [commands, setCommands] = useState("");
  const [checks, setChecks] = useState("");
  const [rollback, setRollback] = useState("");
  const [rollbackChecks, setRollbackChecks] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    request<Target[]>(connection, "/deployment-targets", {
      signal: controller.signal,
    })
      .then(setTargets)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(message(cause));
      });
    return () => controller.abort();
  }, [connection, connected]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    try {
      if (step === 1 && !parseCommands(commands).length)
        throw new Error("Add at least one deployment command.");
      if (step >= 2 && !parseCommands(checks).length)
        throw new Error("Add a health check before saving a target.");
      if (
        step >= 2 &&
        Boolean(parseCommands(rollback).length) !==
          Boolean(parseCommands(rollbackChecks).length)
      )
        throw new Error("Rollback needs both commands and health checks.");
      if (step < 3) {
        setStep(step + 1);
        return;
      }
      setBusy(true);
      const target = await request<Target>(
        connection,
        `/deployment-targets/${identifier}`,
        {
          method: "PUT",
          body: JSON.stringify({
            name,
            repository_id: project,
            commands: parseCommands(commands),
            verification: parseCommands(checks),
            rollback_commands: parseCommands(rollback),
            rollback_verification: parseCommands(rollbackChecks),
          }),
        },
      );
      setTargets((current) => [
        ...current.filter((item) => item.id !== target.id),
        target,
      ]);
      setNotice("Target saved. Deploy a verified run from its Deployment tab.");
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  const input = (
    label: string,
    value: string,
    setter: (value: string) => void,
    required = true,
  ) => (
    <div className="field">
      <label htmlFor={label}>{label}</label>
      <textarea
        id={label}
        rows={4}
        value={value}
        required={required}
        spellCheck={false}
        placeholder='[["make","deploy-staging"]]'
        onChange={(event) => setter(event.target.value)}
      />
      <small>
        JSON argument arrays. Commands run from an isolated export of the
        verified source.
      </small>
    </div>
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Deployment targets</h1>
          <p>
            Define the deployment, health checks and rollback for each project.
          </p>
        </div>
      </div>
      {!connected ? (
        <div className="empty">
          <h3>Connect your factory first</h3>
          <p>Target settings are saved on the factory server.</p>
        </div>
      ) : (
        <Wizard
          steps={["Target", "Commands", "Health checks", "Review"]}
          active={step}
          onStep={setStep}
          title={
            [
              "Choose the project and target",
              "Define deployment commands",
              "Verify and recover",
              "Review the deployment target",
            ][step]
          }
          description={
            [
              "A target belongs to one repository. Give it a name that identifies the environment.",
              "Use the project's deployment tools. Credentials stay in the server environment.",
              "A deployment succeeds only when its health checks pass. Add explicit rollback commands when available.",
              "These commands will be frozen when a deployment is queued. Saving a target does not deploy software.",
            ][step]
          }
        >
          <form onSubmit={(event) => void submit(event)}>
            {step === 0 && (
              <>
                <div className="field">
                  <label htmlFor="target-name">Target name</label>
                  <input
                    id="target-name"
                    value={name}
                    required
                    maxLength={120}
                    placeholder="Staging environment"
                    onChange={(event) => setName(event.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="target-id">Target ID</label>
                  <input
                    id="target-id"
                    value={identifier}
                    required
                    pattern="[a-z][a-z0-9-]{0,63}"
                    placeholder="staging"
                    onChange={(event) => setIdentifier(event.target.value)}
                  />
                  <small>
                    Lowercase letters, numbers and hyphens. An existing ID
                    updates that target.
                  </small>
                </div>
                <div className="field">
                  <label htmlFor="target-project">Target project</label>
                  <select
                    id="target-project"
                    value={project}
                    required
                    onChange={(event) => setProject(event.target.value)}
                  >
                    <option value="">Choose a repository</option>
                    {repositories.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </div>
              </>
            )}
            {step === 1 && input("Deployment commands", commands, setCommands)}
            {step === 2 && (
              <>
                {input("Deployment health checks", checks, setChecks)}
                <details>
                  <summary>Configure rollback</summary>
                  <div className="integration-section">
                    {input("Rollback commands", rollback, setRollback, false)}
                    {input(
                      "Rollback health checks",
                      rollbackChecks,
                      setRollbackChecks,
                      false,
                    )}
                  </div>
                </details>
              </>
            )}
            {step === 3 && (
              <dl className="wizard-summary">
                <div>
                  <dt>Target</dt>
                  <dd>
                    {name} ({identifier})
                  </dd>
                </div>
                <div>
                  <dt>Project</dt>
                  <dd>
                    {repositories.find((item) => item.id === project)?.name}
                  </dd>
                </div>
                <div>
                  <dt>Deploy</dt>
                  <dd>
                    <pre>{commands}</pre>
                  </dd>
                </div>
                <div>
                  <dt>Health checks</dt>
                  <dd>
                    <pre>{checks}</pre>
                  </dd>
                </div>
                <div>
                  <dt>Rollback</dt>
                  <dd>{rollback || "No rollback configured"}</dd>
                </div>
                <div>
                  <dt>Rollback checks</dt>
                  <dd>{rollbackChecks || "No rollback configured"}</dd>
                </div>
              </dl>
            )}
            {error && (
              <div className="alert" role="alert">
                {error}
              </div>
            )}
            {notice && (
              <p className="integration-status" role="status">
                {notice}
              </p>
            )}
            <div className="wizard-actions">
              {step ? (
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setStep(step - 1)}
                >
                  <ArrowLeft size={16} />
                  Back
                </button>
              ) : (
                <span>Target settings remain on the server</span>
              )}
              <button
                className="primary"
                disabled={busy || !repositories.length}
              >
                {step === 3
                  ? "Save deployment target"
                  : step === 2
                    ? "Review target"
                    : "Continue"}
                <ArrowRight size={16} />
              </button>
            </div>
          </form>
        </Wizard>
      )}
      {!!targets.length && (
        <section className="integration-section">
          <h2>Configured targets</h2>
          <div className="integration-grid">
            {targets.map((target) => (
              <article className="integration-card" key={target.id}>
                <h3>{target.name}</h3>
                <p>
                  {repositories.find((item) => item.id === target.repository_id)
                    ?.name ?? target.repository_id}
                </p>
                <p>
                  {target.commands.length} deployment commands ·{" "}
                  {target.verification.length} health checks
                </p>
                <button
                  className="secondary"
                  onClick={() => {
                    setIdentifier(target.id);
                    setName(target.name);
                    setProject(target.repository_id);
                    setCommands(
                      JSON.stringify(target.commands.map((item) => item.argv)),
                    );
                    setChecks(
                      JSON.stringify(
                        target.verification.map((item) => item.argv),
                      ),
                    );
                    setRollback(
                      JSON.stringify(
                        target.rollback_commands.map((item) => item.argv),
                      ),
                    );
                    setRollbackChecks(
                      JSON.stringify(
                        target.rollback_verification.map((item) => item.argv),
                      ),
                    );
                    setStep(0);
                    setNotice("");
                  }}
                >
                  Edit {target.name}
                </button>
              </article>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

export function RunDelivery({
  connection,
  run,
  onConfigure,
}: {
  connection: Connection;
  run: ForgeRun;
  onConfigure: () => void;
}) {
  const [targets, setTargets] = useState<Target[]>([]);
  const [jobs, setJobs] = useState<
    (DeploymentJob & { target_snapshot?: Target })[]
  >([]);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    async function update() {
      try {
        const [currentTargets, deployments] = await Promise.all([
          request<Target[]>(connection, "/deployment-targets", {
            signal: controller.signal,
          }),
          request<(DeploymentJob & { target_snapshot?: Target })[]>(
            connection,
            "/deployments",
            { signal: controller.signal },
          ),
        ]);
        if (!controller.signal.aborted) {
          setTargets(
            currentTargets.filter(
              (item) => item.repository_id === run.repository_id,
            ),
          );
          setJobs(deployments.filter((item) => item.run_id === run.id));
        }
      } catch (cause) {
        if (!controller.signal.aborted) setError(message(cause));
      }
    }
    void update();
    const timer = window.setInterval(() => void update(), 2500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [connection, run.id, run.repository_id, refresh]);
  async function deploy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await post(connection, `/runs/${run.id}/deploy`, { target_id: target });
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function rollback(id: string) {
    setBusy(true);
    setError("");
    try {
      await post(connection, `/deployments/${id}/rollback`);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="run-delivery">
      <div className="section-heading">
        <div>
          <h2>Deploy this verified source</h2>
          <p className="subtle">
            The server checks that the workspace still matches its passing
            verification.
          </p>
        </div>
        <button className="secondary" onClick={onConfigure}>
          Configure targets
        </button>
      </div>
      <form
        className="integration-card"
        onSubmit={(event) => void deploy(event)}
      >
        <div className="field">
          <label htmlFor="deploy-target">Deployment target</label>
          <select
            id="deploy-target"
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            required
          >
            <option value="">Choose a target for this project</option>
            {targets.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </div>
        <button
          className="primary"
          disabled={busy || run.status !== "succeeded" || !targets.length}
        >
          Deploy verified source
        </button>
      </form>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      <section className="integration-section">
        <h2>Deployment history</h2>
        <div className="integration-grid">
          {jobs.map((job) => (
            <article className="integration-card" key={job.id}>
              <h3>
                {job.operation === "rollback" ? "Rollback" : "Deployment"}:{" "}
                {job.target_id}
              </h3>
              <span className="badge">{job.status}</span>
              <small>{job.id}</small>
              {job.error && <p>{job.error}</p>}
              {job.evidence.map((item, index) => (
                <details key={index}>
                  <summary>
                    {item.stage}: exit {item.exit_code}
                  </summary>
                  <pre>{item.output}</pre>
                </details>
              ))}
              {job.operation === "deploy" &&
                ["succeeded", "failed", "interrupted"].includes(job.status) &&
                !!job.target_snapshot?.rollback_commands?.length && (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => void rollback(job.id)}
                  >
                    Roll back deployment
                  </button>
                )}
            </article>
          ))}
        </div>
        {!jobs.length && (
          <p className="integration-status">
            No deployment has been requested for this run.
          </p>
        )}
      </section>
    </div>
  );
}
