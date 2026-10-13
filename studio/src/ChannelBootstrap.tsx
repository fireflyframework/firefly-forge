/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState, type FormEvent } from "react";
import { GitBranch, ShieldCheck, Webhook } from "lucide-react";
import { request, parseCommands, type Connection } from "./api";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
type Defaults = {
  enabled: boolean;
  trusted_actors: string[];
  configuration_actors: string[];
  channel_commands: string[];
  remote_artifacts?: boolean;
  backend: string;
  frontend: string;
  agent: string;
  publish: boolean;
  allowed_commands: string[];
  test_commands: { argv: string[]; timeout: number }[];
  deploy_enabled: boolean;
  deploy_targets: string[];
};
type Config = {
  provider: string;
  enabled: boolean;
  auto_register: boolean;
  trusted_actors: string[];
  configuration_actors: string[];
  defaults: Defaults;
  webhook_secret_configured: boolean;
  ingress_path: string;
};
type Receipt = {
  id: string;
  status: string;
  reason?: string;
  created_at: string;
  delivery_id: string;
  repository_ids: string[];
  repository_receipt_status?: string;
  repository_receipt_reason?: string;
};
export const channelActions = [
  "project",
  "product",
  "brand",
  "design",
  "knowledge",
  "applications",
  "maintenance",
  "resume",
  "handoff",
  "cancel",
  "publish",
];
export const channelActionNames: Record<string, string> = {
  project: "Inspect project",
  product: "Save product definition",
  brand: "Configure product brand",
  design: "Generate design references",
  knowledge: "Read and index knowledge",
  applications: "Inspect applications",
  maintenance: "Queue incident maintenance",
  resume: "Resume paused work",
  handoff: "Change coding provider",
  cancel: "Cancel a run",
  publish: "Publish verified source",
};
const lines = (value: string) =>
  value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
const fail = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "Application onboarding request failed.";
export function ChannelBootstrap({
  connection,
  onConfigure,
}: {
  connection: Connection;
  onConfigure: () => void;
}) {
  const [provider, setProvider] = useState("github");
  const [config, setConfig] = useState<Config | null>(null);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      request<Config>(connection, `/collaboration/bootstrap/${provider}`, {
        signal: controller.signal,
      }),
      request<Receipt[]>(
        connection,
        `/collaboration/bootstrap/${provider}/receipts`,
        { signal: controller.signal },
      ),
    ])
      .then(([settings, records]) => {
        if (!controller.signal.aborted) {
          setConfig(settings);
          setReceipts(records);
          setError("");
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(fail(cause));
      });
    return () => controller.abort();
  }, [connection, provider, revision]);
  return (
    <section className="channel-bootstrap">
      <div className="feature-toolbar">
        <div className="field">
          <label htmlFor="bootstrap-provider">Provider application</label>
          <select
            id="bootstrap-provider"
            value={provider}
            onChange={(event) => {
              setProvider(event.target.value);
              setConfig(null);
            }}
          >
            <option value="github">GitHub</option>
            <option value="gitlab">GitLab</option>
            <option value="bitbucket">Bitbucket · pull requests</option>
          </select>
        </div>
        <button className="secondary" onClick={onConfigure}>
          <GitBranch size={18} />
          Configure provider grant
        </button>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {config?.provider === provider ? (
        <>
          <BootstrapEditor
            key={provider}
            connection={connection}
            initial={config}
            onSaved={(value) => {
              setConfig(value);
              setRevision((version) => version + 1);
            }}
          />
          <section className="feature-record">
            <header>
              <h3>Application delivery receipts</h3>
              <button
                className="quiet"
                onClick={() => setRevision((value) => value + 1)}
              >
                Refresh
              </button>
            </header>
            {!receipts.length ? (
              <p>No application-wide deliveries recorded.</p>
            ) : (
              receipts.map((receipt) => (
                <article className="feature-record" key={receipt.id}>
                  <div className="feature-toolbar">
                    <strong>{receipt.delivery_id}</strong>
                    <span className="feature-status">{receipt.status}</span>
                  </div>
                  <p>{receipt.reason || "Provider delivery processed."}</p>
                  <p className="feature-meta">
                    {new Date(receipt.created_at).toLocaleString()} ·{" "}
                    {receipt.repository_ids.length} repositories recorded
                  </p>
                  {receipt.repository_receipt_status && (
                    <p>
                      Repository receipt {receipt.repository_receipt_status}:{" "}
                      {receipt.repository_receipt_reason ||
                        "No denial reason reported"}
                    </p>
                  )}
                </article>
              ))
            )}
          </section>
        </>
      ) : (
        !error && <p role="status">Loading application onboarding settings…</p>
      )}
    </section>
  );
}
function BootstrapEditor({
  connection,
  initial,
  onSaved,
}: {
  connection: Connection;
  initial: Config;
  onSaved: (value: Config) => void;
}) {
  const [step, setStep] = useState(0);
  const [enabled, setEnabled] = useState(initial.enabled);
  const [auto, setAuto] = useState(initial.auto_register);
  const [actors, setActors] = useState(initial.trusted_actors.join("\n"));
  const [admins, setAdmins] = useState(initial.configuration_actors.join("\n"));
  const [defaults, setDefaults] = useState(initial.defaults);
  const [checks, setChecks] = useState(
    JSON.stringify(initial.defaults.test_commands.map((item) => item.argv)),
  );
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setSaved(false);
    try {
      if (enabled && !lines(actors).length)
        throw new Error(
          "Add trusted provider actor IDs before enabling onboarding.",
        );
      if (lines(admins).some((id) => !lines(actors).includes(id)))
        throw new Error(
          "Configuration actors must also appear in trusted actors.",
        );
      if (step >= 2 && enabled && !secret && !initial.webhook_secret_configured)
        throw new Error(
          "Configure a signing secret before enabling global deliveries.",
        );
      if (step < 3) {
        setStep(step + 1);
        return;
      }
      setBusy(true);
      const result = await request<Config>(
        connection,
        `/collaboration/bootstrap/${initial.provider}`,
        {
          method: "PUT",
          body: JSON.stringify({
            enabled,
            auto_register: auto,
            trusted_actors: lines(actors),
            configuration_actors: lines(admins),
            defaults: {
              ...defaults,
              test_commands: parseCommands(checks),
              trusted_actors: lines(actors),
              configuration_actors: lines(admins),
            },
            ...(secret ? { webhook_secret: secret } : {}),
          }),
        },
      );
      setSecret("");
      setSaved(true);
      onSaved(result);
    } catch (cause) {
      setError(fail(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Wizard
      locked={busy}
      steps={["Trust", "Commands", "Webhook", "Review"]}
      active={step}
      onStep={setStep}
      title={
        [
          "Trust application actors",
          "Choose repository command defaults",
          "Connect signed application deliveries",
          "Review application onboarding",
        ][step]
      }
      description={
        [
          "Provider grants bound repository access. Exact actor IDs determine who may issue commands.",
          "These defaults apply when the signed onboarding flow registers a granted repository.",
          "Configure the ingress path on your operator-managed HTTPS gateway and use a separate signing secret.",
          "Saved configuration is not proof of connectivity. Delivery receipts record actual outcomes.",
        ][step]
      }
    >
      <SetupIllustration kind="onboarding" step={step} />
      <form onSubmit={(event) => void submit(event)}>
        {step === 0 && (
          <>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              <ShieldCheck size={18} />
              Enable signed application commands
            </label>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={auto}
                onChange={(event) => setAuto(event.target.checked)}
              />
              Automatically register freshly granted repositories
            </label>
            <div className="field">
              <label htmlFor="bootstrap-actors">
                Trusted application actor IDs
              </label>
              <textarea
                id="bootstrap-actors"
                rows={3}
                required={enabled}
                value={actors}
                onChange={(event) => setActors(event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="bootstrap-admins">Configuration actor IDs</label>
              <textarea
                id="bootstrap-admins"
                rows={3}
                value={admins}
                onChange={(event) => setAdmins(event.target.value)}
              />
              <small>
                Subset of trusted actors. These actors may change product and
                brand configuration through signed commands.
              </small>
            </div>
          </>
        )}
        {step === 1 && (
          <>
            <fieldset>
              <legend>Repository channel actions</legend>
              <div className="channel-choices">
                {channelActions.map((action) => (
                  <label className="checkbox-line" key={action}>
                    <input
                      type="checkbox"
                      checked={defaults.channel_commands.includes(action)}
                      onChange={(event) =>
                        setDefaults((value) => ({
                          ...value,
                          channel_commands: event.target.checked
                            ? [...value.channel_commands, action]
                            : value.channel_commands.filter(
                                (item) => item !== action,
                              ),
                        }))
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
                checked={defaults.remote_artifacts ?? false}
                onChange={(event) =>
                  setDefaults((value) => ({
                    ...value,
                    remote_artifacts: event.target.checked,
                  }))
                }
              />
              Publish brand and design references through the granted repository
              provider
            </label>
            <div className="two-columns">
              {(
                [
                  [
                    "backend",
                    "Default backend",
                    ["pyfly", "larafly", "java", "rust", "dotnet"],
                  ],
                  [
                    "frontend",
                    "Default frontend",
                    ["react", "angular", "nextjs", "vue", "none"],
                  ],
                  [
                    "agent",
                    "Default coding provider",
                    ["codex", "claude", "bridge", "agentic"],
                  ],
                ] as const
              ).map(([field, label, options]) => (
                <div className="field" key={field}>
                  <label htmlFor={`bootstrap-${field}`}>{label}</label>
                  <select
                    id={`bootstrap-${field}`}
                    value={defaults[field]}
                    onChange={(event) =>
                      setDefaults((value) => ({
                        ...value,
                        [field]: event.target.value,
                      }))
                    }
                  >
                    {options.map((option) => (
                      <option key={option}>{option}</option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
            <div className="field">
              <label htmlFor="bootstrap-checks">
                Default verification commands
              </label>
              <textarea
                id="bootstrap-checks"
                rows={3}
                value={checks}
                onChange={(event) => setChecks(event.target.value)}
                placeholder='[["make","check"]]'
              />
              <small>
                Operator-owned JSON argument arrays. Maintenance and execution
                require actual checks.
              </small>
            </div>
            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={defaults.publish}
                onChange={(event) =>
                  setDefaults((value) => ({
                    ...value,
                    publish: event.target.checked,
                  }))
                }
              />
              Permit explicit verified publication
            </label>
          </>
        )}
        {step === 2 && (
          <>
            <p>
              <Webhook size={18} />
              Application-wide ingress:
            </p>
            <pre>{initial.ingress_path}</pre>
            <p>
              The provider application must have a current repository grant. Use
              this path beneath your own public HTTPS gateway; no external
              address is inferred.
            </p>
            <div className="field">
              <label htmlFor="bootstrap-secret">
                Application webhook signing secret
              </label>
              <input
                id="bootstrap-secret"
                type="password"
                minLength={8}
                autoComplete="new-password"
                value={secret}
                onChange={(event) => setSecret(event.target.value)}
              />
              <small>
                {initial.webhook_secret_configured
                  ? "A secret is stored. Blank preserves it."
                  : "No secret is configured."}
              </small>
            </div>
          </>
        )}
        {step === 3 && (
          <dl className="wizard-summary">
            <div>
              <dt>Provider</dt>
              <dd>{initial.provider}</dd>
            </div>
            <div>
              <dt>Onboarding</dt>
              <dd>
                {enabled ? "Enabled" : "Disabled"} · automatic registration{" "}
                {auto ? "enabled" : "disabled"}
              </dd>
            </div>
            <div>
              <dt>Trusted actors</dt>
              <dd>{lines(actors).join(", ") || "None"}</dd>
            </div>
            <div>
              <dt>Configuration actors</dt>
              <dd>{lines(admins).join(", ") || "None"}</dd>
            </div>
            <div>
              <dt>Actions</dt>
              <dd>{defaults.channel_commands.join(", ") || "None"}</dd>
            </div>
            <div>
              <dt>Default stack</dt>
              <dd>
                {defaults.backend} / {defaults.frontend} / {defaults.agent}
              </dd>
            </div>
            <div>
              <dt>Comment example</dt>
              <dd>
                <code>{"/forge project {}"}</code>
              </dd>
            </div>
          </dl>
        )}
        {error && (
          <div className="alert" role="alert">
            {error}
          </div>
        )}
        {saved && (
          <p className="notice" role="status">
            Application onboarding settings saved. Inspect actual delivery
            receipts to verify operation.
          </p>
        )}
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
          <button disabled={busy}>
            {busy
              ? "Saving…"
              : step === 3
                ? "Save application onboarding"
                : "Continue"}
          </button>
        </div>
      </form>
    </Wizard>
  );
}
