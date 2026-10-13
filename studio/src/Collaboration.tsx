/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { repositoryLabels } from "./RepositoryLabels";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  request,
  post,
  parseCommands,
  type Connection,
  type Repository,
  type ForgeRun,
} from "./api";
import {
  ChannelBootstrap,
  channelActions,
  channelActionNames,
} from "./ChannelBootstrap";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
import { safeExternalLink } from "./FeatureLinks";

type Settings = {
  repository_id: string;
  enabled: boolean;
  trusted_actors: string[];
  configuration_actors?: string[];
  channel_commands?: string[];
  remote_artifacts?: boolean;
  webhook_secret_configured: boolean;
  backend: string;
  frontend: string;
  agent: string;
  publish: boolean;
  allowed_commands: string[];
  test_commands: { argv: string[]; timeout: number }[];
  deploy_enabled: boolean;
  deploy_targets: string[];
  capabilities: {
    issue_authoring: boolean;
    issue_comments: boolean;
    pull_request_comments: boolean;
  };
};
type Receipt = {
  id: string;
  provider: string;
  status: string;
  reason: string;
  actor: string;
  issue?: { kind?: string; number?: number; url?: string } | null;
  command?: { action?: string };
  run_id?: string;
  feedback_status: string;
  created_at: string;
};
const failure = (value: unknown) =>
  value instanceof Error
    ? value.message
    : "The repository workflow request failed.";
const rows = (text: string) =>
  text
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean);
export function Collaboration({
  connection,
  connected,
  repositories,
  onOpen,
  onConfigure,
}: {
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
  onOpen: (run: ForgeRun) => void;
  onConfigure: () => void;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [scope, setScope] = useState("repository");
  const [project, setProject] = useState("");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [loaded, setLoaded] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [key, setKey] = useState(() => crypto.randomUUID());
  const repo =
    repositories.find((item) => item.id === project) ?? repositories[0];
  const id = repo?.id;
  const currentProject = useRef(id);
  currentProject.current = id;
  const provider = repo?.connection_provider || repo?.provider;
  const supported =
    Boolean(repo?.connection_provider && repo?.remote_repository_id) &&
    ["github", "gitlab", "bitbucket"].includes(provider ?? "");
  useEffect(() => {
    if (!connected || !id || !supported) return;
    const controller = new AbortController();
    const update = () =>
      Promise.all([
        request<Settings>(connection, `/repositories/${id}/collaboration`, {
          signal: controller.signal,
        }),
        request<Receipt[]>(
          connection,
          `/collaboration/receipts?repository_id=${encodeURIComponent(id)}`,
          { signal: controller.signal },
        ),
      ])
        .then(([config, records]) => {
          if (!controller.signal.aborted) {
            setSettings(config);
            setReceipts(records);
            setLoaded(id);
            setError("");
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(failure(cause));
        });
    void update();
    const timer = setInterval(() => void update(), 10000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [connection, connected, id, supported, refresh]);
  async function process(receipt: Receipt) {
    setBusy(true);
    setError("");
    try {
      await post(
        connection,
        `/collaboration/receipts/${receipt.id}/process`,
        {},
      );
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  }
  async function issue(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const receipt = await post<Receipt>(
        connection,
        `/repositories/${id}/issues`,
        { title, body, idempotency_key: key },
      );
      setNotice(
        receipt.status === "needs_reconciliation"
          ? "The issue write outcome is unknown. Inspect the receipt and remote repository before reconciling."
          : `Issue receipt recorded: ${receipt.status}.`,
      );
      setDraft(false);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="repository-collaboration">
      <div className="page-heading">
        <div>
          <h1>Repository workflow</h1>
          <p>
            Accept signed issue and pull request commands from explicitly
            trusted actors.
          </p>
        </div>
      </div>
      {connected && (
        <div className="feature-tabs" role="group" aria-label="Workflow scope">
          <button
            className="quiet"
            aria-pressed={scope === "repository"}
            onClick={() => setScope("repository")}
          >
            Repository commands
          </button>
          <button
            className="quiet"
            aria-pressed={scope === "application"}
            onClick={() => setScope("application")}
          >
            Application onboarding
          </button>
        </div>
      )}
      {!connected ? (
        <div className="empty">
          <h3>Connect your factory first</h3>
          <p>Workflow settings and receipts are stored on the factory.</p>
        </div>
      ) : scope === "application" ? (
        <ChannelBootstrap connection={connection} onConfigure={onConfigure} />
      ) : !repo ? (
        <div className="empty">
          <h3>Connect a repository first</h3>
        </div>
      ) : (
        <>
          <div className="feature-toolbar">
            <div className="field">
              <label htmlFor="collaboration-project">Workflow project</label>
              <select
                id="collaboration-project"
                value={id}
                onChange={(event) => {
                  setProject(event.target.value);
                  setDraft(false);
                  setNotice("");
                }}
              >
                {repositories.map((item) => (
                  <option key={item.id} value={item.id}>
                    {projectLabels.get(item.id)}
                  </option>
                ))}
              </select>
            </div>
            {loaded === id && settings && (
              <span className="feature-status">
                {settings.enabled ? "Configured" : "Disabled"}
              </span>
            )}
          </div>
          {!supported ? (
            <div className="empty">
              <h3>Connect a provider application</h3>
              <p>
                This workflow supports GitHub, GitLab and Bitbucket pull
                requests. Generic Git repositories use factory runs directly.
              </p>
              <button className="secondary" onClick={onConfigure}>
                Connect provider application
              </button>
            </div>
          ) : loaded !== id || !settings ? (
            <p role="status">Loading workflow settings…</p>
          ) : (
            <>
              <WorkflowEditor
                key={id}
                connection={connection}
                repository={repo}
                initial={settings}
                onSaved={(value) => {
                  if (currentProject.current !== value.repository_id) return;
                  setSettings(value);
                  setNotice(
                    "Workflow settings saved. Incoming deliveries are recorded as receipts.",
                  );
                }}
              />
              <section className="panel feature-record">
                <div className="section-title">
                  <h2>Delivery receipts</h2>
                  {settings.capabilities.issue_authoring && (
                    <button
                      className="secondary"
                      onClick={() => setDraft((value) => !value)}
                    >
                      Draft an issue
                    </button>
                  )}
                </div>
                {provider === "bitbucket" && (
                  <p>
                    Bitbucket supports pull request comments. Its native issue
                    tracker API is unavailable.
                  </p>
                )}
                {draft && (
                  <form onSubmit={(event) => void issue(event)}>
                    <div className="field">
                      <label htmlFor="issue-title">Issue title</label>
                      <input
                        id="issue-title"
                        required
                        maxLength={250}
                        value={title}
                        onChange={(event) => {
                          setTitle(event.target.value);
                          setKey(crypto.randomUUID());
                        }}
                      />
                    </div>
                    <div className="field">
                      <label htmlFor="issue-body">Issue description</label>
                      <textarea
                        id="issue-body"
                        required
                        rows={5}
                        maxLength={50000}
                        value={body}
                        onChange={(event) => {
                          setBody(event.target.value);
                          setKey(crypto.randomUUID());
                        }}
                      />
                    </div>
                    <p>
                      Submitting creates a remote issue using the connected
                      provider grant. Network retries retain the same request
                      identity.
                    </p>
                    <button disabled={busy}>Create issue</button>
                    <button
                      type="button"
                      className="quiet"
                      onClick={() => setDraft(false)}
                    >
                      Cancel
                    </button>
                  </form>
                )}
                {!receipts.length ? (
                  <p>No deliveries recorded for this project.</p>
                ) : (
                  <div className="feature-list">
                    {receipts.map((receipt) => (
                      <article className="feature-record" key={receipt.id}>
                        <div className="feature-toolbar">
                          <strong>
                            {receipt.command?.action || "Provider delivery"}
                          </strong>
                          <span className="feature-status">
                            {receipt.status.replaceAll("_", " ")}
                          </span>
                        </div>
                        <p>{receipt.reason || "No reason reported."}</p>
                        <p className="feature-meta">
                          Actor {receipt.actor || "not reported"} · Feedback{" "}
                          {receipt.feedback_status || "not reported"} ·{" "}
                          {new Date(receipt.created_at).toLocaleString()}
                        </p>
                        {safeExternalLink(receipt.issue?.url) && (
                          <a
                            href={safeExternalLink(receipt.issue?.url)!}
                            target="_blank"
                            rel="noreferrer"
                          >
                            Open recorded issue or pull request
                          </a>
                        )}
                        <div className="feature-actions">
                          {receipt.status === "received" && (
                            <button
                              className="secondary"
                              disabled={busy}
                              onClick={() => void process(receipt)}
                            >
                              Process receipt
                            </button>
                          )}
                          {receipt.run_id && (
                            <button
                              className="secondary"
                              onClick={() =>
                                void request<ForgeRun>(
                                  connection,
                                  `/runs/${receipt.run_id}`,
                                )
                                  .then(onOpen)
                                  .catch((cause) => setError(failure(cause)))
                              }
                            >
                              Inspect run
                            </button>
                          )}
                        </div>
                        {receipt.status === "needs_reconciliation" && (
                          <Reconciliation
                            connection={connection}
                            receipt={receipt}
                            onSaved={() => setRefresh((value) => value + 1)}
                          />
                        )}
                        <details>
                          <summary>Receipt identity</summary>
                          <code>{receipt.id}</code>
                        </details>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              {notice}
            </div>
          )}
        </>
      )}
    </section>
  );
}
function WorkflowEditor({
  connection,
  repository,
  initial,
  onSaved,
}: {
  connection: Connection;
  repository: Repository;
  initial: Settings;
  onSaved: (settings: Settings) => void;
}) {
  const [step, setStep] = useState(0);
  const [enabled, setEnabled] = useState(initial.enabled);
  const [actors, setActors] = useState(initial.trusted_actors.join("\n"));
  const [secret, setSecret] = useState("");
  const [admins, setAdmins] = useState(
    (initial.configuration_actors ?? []).join("\n"),
  );
  const [channelCommands, setChannelCommands] = useState(
    initial.channel_commands ?? [],
  );
  const [remoteArtifacts, setRemoteArtifacts] = useState(
    initial.remote_artifacts ?? false,
  );
  const [commands, setCommands] = useState(initial.allowed_commands);
  const [checks, setChecks] = useState(
    JSON.stringify(initial.test_commands.map((item) => item.argv)),
  );
  const [backend, setBackend] = useState(initial.backend);
  const [frontend, setFrontend] = useState(initial.frontend);
  const [agent, setAgent] = useState(initial.agent);
  const [publish, setPublish] = useState(initial.publish);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      if (rows(admins).some((actor) => !rows(actors).includes(actor)))
        throw new Error(
          "Configuration actors must also appear in trusted actors.",
        );
      if (enabled && !rows(actors).length)
        throw new Error(
          "Add an exact provider actor ID before enabling this workflow.",
        );
      if (step === 1 && !parseCommands(checks).length)
        throw new Error(
          "Add operator-owned checks before accepting execution commands.",
        );
      if (step >= 2 && enabled && !secret && !initial.webhook_secret_configured)
        throw new Error(
          "Configure a signing secret before enabling deliveries.",
        );
      if (step < 3) {
        setStep(step + 1);
        return;
      }
      setBusy(true);
      const result = await request<Settings>(
        connection,
        `/repositories/${repository.id}/collaboration`,
        {
          method: "PUT",
          body: JSON.stringify({
            enabled,
            trusted_actors: rows(actors),
            configuration_actors: rows(admins),
            channel_commands: channelCommands,
            remote_artifacts: remoteArtifacts,
            backend,
            frontend,
            agent,
            publish,
            allowed_commands: commands,
            test_commands: parseCommands(checks),
            deploy_enabled: initial.deploy_enabled,
            deploy_targets: initial.deploy_targets,
            ...(secret ? { webhook_secret: secret } : {}),
          }),
        },
      );
      setSecret("");
      onSaved(result);
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  }
  const provider = repository.connection_provider || repository.provider;
  return (
    <Wizard
      locked={busy}
      steps={["Trust", "Commands", "Webhook", "Review"]}
      active={step}
      onStep={setStep}
      title={
        [
          "Choose trusted actors",
          "Configure execution commands",
          "Configure signed deliveries",
          "Review repository workflow",
        ][step]
      }
      description={
        [
          "Trust exact, stable provider IDs rather than display names.",
          "Commands use operator-owned checks and the chosen project stack.",
          "Expose the ingress through your operator-managed HTTPS gateway. The Studio token is not a webhook secret.",
          "Saving enables only the commands and actors shown here. Publication is explicit.",
        ][step]
      }
    >
      <SetupIllustration kind="collaboration" step={step} />
      <form onSubmit={(event) => void submit(event)}>
        {step === 0 && (
          <>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enable repository commands
            </label>
            <div className="field">
              <label htmlFor="trusted-actors">Trusted provider actor IDs</label>
              <textarea
                id="trusted-actors"
                rows={4}
                required={enabled}
                value={actors}
                onChange={(event) => setActors(event.target.value)}
              />
              <small>One exact provider ID per line.</small>
            </div>
            <div className="field">
              <label htmlFor="workflow-config-actors">
                Repository configuration actor IDs
              </label>
              <textarea
                id="workflow-config-actors"
                rows={3}
                value={admins}
                onChange={(event) => setAdmins(event.target.value)}
              />
              <small>
                Subset of trusted actors permitted to change product, brand and
                design configuration.
              </small>
            </div>
          </>
        )}
        {step === 1 && (
          <>
            <fieldset>
              <legend>Allowed comment commands</legend>
              {["run", "test", "maintain", "status"].map((action) => (
                <label key={action} className="checkbox-line">
                  <input
                    type="checkbox"
                    checked={commands.includes(action)}
                    onChange={(event) =>
                      setCommands((values) =>
                        event.target.checked
                          ? [...values, action]
                          : values.filter((value) => value !== action),
                      )
                    }
                  />
                  {action}
                </label>
              ))}
            </fieldset>
            <fieldset>
              <legend>Repository channel actions</legend>
              <div className="channel-choices">
                {channelActions.map((action) => (
                  <label className="checkbox-line" key={action}>
                    <input
                      type="checkbox"
                      checked={channelCommands.includes(action)}
                      onChange={(event) =>
                        setChannelCommands((values) =>
                          event.target.checked
                            ? [...values, action]
                            : values.filter((item) => item !== action),
                        )
                      }
                    />
                    {channelActionNames[action]}
                  </label>
                ))}
              </div>
            </fieldset>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={remoteArtifacts}
                onChange={(event) => setRemoteArtifacts(event.target.checked)}
              />
              Publish brand and design references through the granted repository
              provider
            </label>
            <div className="two-columns">
              <div className="field">
                <label htmlFor="workflow-backend">Workflow backend</label>
                <select
                  id="workflow-backend"
                  value={backend}
                  onChange={(event) => setBackend(event.target.value)}
                >
                  {["pyfly", "larafly", "java", "rust", "dotnet"].map(
                    (value) => (
                      <option key={value}>{value}</option>
                    ),
                  )}
                </select>
              </div>
              <div className="field">
                <label htmlFor="workflow-frontend">Workflow frontend</label>
                <select
                  id="workflow-frontend"
                  value={frontend}
                  onChange={(event) => setFrontend(event.target.value)}
                >
                  {["react", "angular", "nextjs", "vue", "none"].map(
                    (value) => (
                      <option key={value}>{value}</option>
                    ),
                  )}
                </select>
              </div>
            </div>
            <div className="field">
              <label htmlFor="workflow-agent">Workflow coding provider</label>
              <select
                id="workflow-agent"
                value={agent}
                onChange={(event) => setAgent(event.target.value)}
              >
                {["codex", "claude", "bridge", "agentic"].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="workflow-checks">
                Operator verification commands
              </label>
              <textarea
                id="workflow-checks"
                rows={4}
                value={checks}
                onChange={(event) => setChecks(event.target.value)}
                placeholder='[["make","check"]]'
              />
            </div>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={publish}
                onChange={(event) => setPublish(event.target.checked)}
              />
              Allow verified run publication
            </label>
          </>
        )}
        {step === 2 && (
          <>
            <p>Provider webhook ingress:</p>
            <pre>{`/api/v1/webhooks/${provider}/${repository.id}`}</pre>
            <p>
              Use your public HTTPS gateway address followed by this exact path.
              Configure the same signing secret in the provider application.
            </p>
            <div className="field">
              <label htmlFor="workflow-secret">
                New webhook signing secret
              </label>
              <input
                id="workflow-secret"
                type="password"
                minLength={8}
                maxLength={500}
                autoComplete="new-password"
                value={secret}
                onChange={(event) => setSecret(event.target.value)}
              />
              <small>
                {initial.webhook_secret_configured
                  ? "A secret is configured. Leave blank to retain it."
                  : "No secret is configured. Add one before enabling signed deliveries."}
              </small>
            </div>
          </>
        )}
        {step === 3 && (
          <dl className="review-grid">
            <div>
              <dt>Trust</dt>
              <dd>
                {enabled ? "Enabled" : "Disabled"} ·{" "}
                {rows(actors).join(", ") || "No trusted actors"}
              </dd>
            </div>
            <div>
              <dt>Commands</dt>
              <dd>{commands.join(", ") || "None"}</dd>
            </div>
            <div>
              <dt>Stack and provider</dt>
              <dd>
                {backend} / {frontend} / {agent}
              </dd>
            </div>
            <div>
              <dt>Publication</dt>
              <dd>{publish ? "Allowed after verification" : "Disabled"}</dd>
            </div>
            <div>
              <dt>Checks</dt>
              <dd>
                <pre>{checks}</pre>
              </dd>
            </div>
            <div>
              <dt>Comment example</dt>
              <dd>
                <code>
                  {
                    '/forge run {"brief":"Implement the recorded acceptance criteria"}'
                  }
                </code>
              </dd>
            </div>
          </dl>
        )}
        {error && (
          <div className="alert" role="alert">
            {error}
          </div>
        )}
        <div className="wizard-actions">
          {step > 0 && (
            <button
              type="button"
              className="secondary"
              onClick={() => setStep(step - 1)}
              disabled={busy}
            >
              Back
            </button>
          )}
          <button disabled={busy}>
            {busy ? "Saving…" : step === 3 ? "Save workflow" : "Continue"}
          </button>
        </div>
      </form>
    </Wizard>
  );
}
function Reconciliation({
  connection,
  receipt,
  onSaved,
}: {
  connection: Connection;
  receipt: Receipt;
  onSaved: () => void;
}) {
  const [outcome, setOutcome] = useState("posted");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <details>
      <summary>Reconcile an unknown remote write</summary>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          void post(
            connection,
            `/collaboration/receipts/${receipt.id}/reconcile`,
            { outcome, note },
          )
            .then(onSaved)
            .catch((cause) => setError(failure(cause)))
            .finally(() => setBusy(false));
        }}
      >
        <p>
          Inspect the provider's issue or comment before recording an outcome.
          This action does not retry the remote write.
        </p>
        <div className="field">
          <label htmlFor={`outcome-${receipt.id}`}>
            Verified remote outcome
          </label>
          <select
            id={`outcome-${receipt.id}`}
            value={outcome}
            onChange={(event) => setOutcome(event.target.value)}
          >
            <option value="posted">The write was posted</option>
            <option value="not_posted">The write was not posted</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor={`note-${receipt.id}`}>Reconciliation evidence</label>
          <textarea
            id={`note-${receipt.id}`}
            required
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </div>
        {error && (
          <div role="alert" className="alert">
            {error}
          </div>
        )}
        <button disabled={busy}>Record inspected outcome</button>
      </form>
    </details>
  );
}
