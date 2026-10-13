/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { repositoryLabels } from "./RepositoryLabels";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { ArrowLeft, ArrowRight, Plus, RefreshCw, X } from "lucide-react";
import {
  parseCommands,
  post,
  request,
  type ApplicationIncident,
  type Connection,
  type ForgeRun,
  type MetricThreshold,
  type MonitoredApplication,
  type Repository,
} from "./api";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
import "./StudioFeatures.css";

type Target = { id: string; name: string; repository_id: string };
type Provider = { id: string; name: string; ready: boolean };
const message = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The application record could not be loaded.";
const date = (value: string) => new Date(value).toLocaleString();
const field = (
  label: string,
  id: string,
  children: ReactNode,
  hint?: string,
) => (
  <div className="field">
    <label htmlFor={id}>{label}</label>
    {children}
    {hint && <small>{hint}</small>}
  </div>
);
const status = (app: MonitoredApplication) =>
  !app.latest
    ? "No recorded check"
    : app.stale
      ? "Stale observation"
      : app.latest.status;

export function Operations({
  connection,
  connected,
  repositories,
  providers,
  onOpen,
  onConfigure,
}: {
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
  providers: unknown;
  onOpen: (run: ForgeRun) => void;
  onConfigure: () => void;
}) {
  const [apps, setApps] = useState<MonitoredApplication[]>([]);
  const [targets, setTargets] = useState<Target[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<MonitoredApplication | null>(null);
  const [editor, setEditor] = useState<MonitoredApplication | "new" | null>(
    null,
  );
  const [incident, setIncident] = useState<ApplicationIncident | null>(null);
  const [action, setAction] = useState<"maintenance" | "resolve">(
    "maintenance",
  );
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [actionError, setActionError] = useState("");
  const error = catalogError || detailError || actionError;
  const [refresh, setRefresh] = useState(0);
  const selected = apps.find((app) => app.id === selectedId) ?? apps[0] ?? null;
  const current = detail?.id === selected?.id ? detail : selected;
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    const load = async () => {
      setLoading(true);
      try {
        const [applications, configuredTargets] = await Promise.all([
          request<MonitoredApplication[]>(connection, "/applications", {
            signal: controller.signal,
          }),
          request<Target[]>(connection, "/deployment-targets", {
            signal: controller.signal,
          }),
        ]);
        if (!controller.signal.aborted) {
          setApps(applications);
          setTargets(configuredTargets);
          setCatalogError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setCatalogError(message(cause));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [connection, connected, refresh]);
  const applicationId = selected?.id;
  useEffect(() => {
    if (!connected || !applicationId) return;
    const controller = new AbortController();
    const load = async () => {
      try {
        const app = await request<MonitoredApplication>(
          connection,
          `/applications/${applicationId}`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) {
          setDetail(app);
          setDetailError("");
        }
      } catch (cause) {
        if (!controller.signal.aborted) setDetailError(message(cause));
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 10000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [applicationId, connection, connected, refresh]);
  async function check() {
    if (!current) return;
    setBusy(true);
    setActionError("");
    try {
      await post(connection, `/applications/${current.id}/check`);
      setRefresh((value) => value + 1);
    } catch (cause) {
      setActionError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="operations">
      <div className="page-heading">
        <div>
          <h1>Applications</h1>
          <p>
            Inspect recorded health checks, metrics and incidents after
            deployment.
          </p>
        </div>
        <button
          className="primary"
          disabled={!connected || !targets.length}
          onClick={() => setEditor("new")}
        >
          <Plus size={16} />
          Monitor an application
        </button>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
          {current && " Showing the last loaded record."}
        </div>
      )}
      {!connected ? (
        <div className="empty">
          <h3>Connect your factory first</h3>
          <p>
            Application records and checks are managed by your factory server.
          </p>
        </div>
      ) : editor ? (
        <ApplicationEditor
          key={editor === "new" ? "new" : editor.id}
          connection={connection}
          repositories={repositories}
          targets={targets}
          initial={editor === "new" ? null : editor}
          onCancel={() => setEditor(null)}
          onSaved={(app) => {
            setSelectedId(app.id);
            setDetail(app);
            setEditor(null);
            setRefresh((value) => value + 1);
          }}
        />
      ) : (
        <>
          {!targets.length && (
            <div className="integration-status">
              Configure a deployment target before registering its application.
              <button className="text-button" onClick={onConfigure}>
                Configure a target
                <ArrowRight size={15} />
              </button>
            </div>
          )}
          <div className="feature-toolbar">
            {field(
              "Application",
              "operation-app",
              <select
                id="operation-app"
                value={selected?.id ?? ""}
                disabled={!apps.length}
                onChange={(event) => setSelectedId(event.target.value)}
              >
                <option value="" disabled>
                  No configured applications
                </option>
                {apps.map((app) => (
                  <option key={app.id} value={app.id}>
                    {app.name}
                  </option>
                ))}
              </select>,
            )}
            <button
              className="secondary"
              disabled={loading}
              onClick={() => setRefresh((value) => value + 1)}
            >
              <RefreshCw size={15} />
              Refresh records
            </button>
          </div>
          {!current ? (
            <div className="empty">
              <h3>
                {loading
                  ? "Loading application records"
                  : "No applications configured"}
              </h3>
              <p>
                Add an application and its real health endpoint to begin
                recording observations.
              </p>
            </div>
          ) : (
            <article className="feature-record">
              <header>
                <div>
                  <h2>{current.name}</h2>
                  <p className="subtle">
                    {repositories.find(
                      (repo) => repo.id === current.repository_id,
                    )?.name ?? current.repository_id}
                  </p>
                </div>
                <div className="feature-actions">
                  <button
                    className="secondary"
                    onClick={() => setEditor(current)}
                  >
                    Edit application
                  </button>
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => void check()}
                  >
                    {busy ? "Checking endpoint…" : "Check now"}
                  </button>
                </div>
              </header>
              <div className="feature-meta">
                <span
                  className={`feature-status ${current.latest && !current.stale && !catalogError && !detailError ? current.latest.status : ""}`}
                >
                  {catalogError || detailError
                    ? "Updates unavailable"
                    : status(current)}
                </span>
                <span>
                  {current.latest
                    ? `Last recorded ${date(current.latest.created_at)}`
                    : "The server has not recorded a check yet."}
                </span>
                <span>
                  Configured interval {current.interval_seconds} seconds
                </span>
              </div>
              {current.stale && current.latest && (
                <p className="integration-status">
                  The latest observation is stale. Current application health is
                  unknown until a new check completes.
                </p>
              )}
              {current.latest && (
                <>
                  <dl className="feature-measurements">
                    <div>
                      <dt>Recorded response latency</dt>
                      <dd>{current.latest.latency_ms.toFixed(1)} ms</dd>
                    </div>
                    {Object.entries(current.latest.metrics).map(
                      ([name, value]) => (
                        <div key={name}>
                          <dt>{name}</dt>
                          <dd>{value}</dd>
                        </div>
                      ),
                    )}
                  </dl>
                  {!!current.latest.reasons.length && (
                    <ul className="feature-reasons">
                      {current.latest.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  )}
                </>
              )}
              <div className="feature-links">
                {Object.entries(current.observability).map(
                  ([name, url]) =>
                    url && (
                      <a
                        href={url}
                        key={name}
                        target="_blank"
                        rel="noreferrer noopener"
                      >
                        Open{" "}
                        {name === "dashboard_url"
                          ? "dashboard"
                          : name.replace("_url", "")}
                      </a>
                    ),
                )}
              </div>
              <section className="integration-section">
                <h3>Incidents</h3>
                <div className="feature-list">
                  {current.incidents.map((item) => (
                    <article key={item.id}>
                      <header>
                        <div>
                          <h3>{item.summary}</h3>
                          <time>{date(item.updated_at)}</time>
                        </div>
                        <span className={`feature-status ${item.status}`}>
                          {item.status}
                        </span>
                      </header>
                      {item.resolution && <p>{item.resolution}</p>}
                      {!!item.maintenance_run_ids.length && (
                        <p className="subtle">
                          Maintenance runs:{" "}
                          {item.maintenance_run_ids.join(", ")}
                        </p>
                      )}
                      <div className="feature-actions">
                        <button
                          className="secondary"
                          disabled={item.status !== "open"}
                          onClick={() => {
                            setIncident(item);
                            setAction("maintenance");
                          }}
                        >
                          Start maintenance
                        </button>
                        <button
                          className="secondary"
                          disabled={
                            item.status !== "open" ||
                            current.stale ||
                            current.latest?.status !== "healthy"
                          }
                          onClick={() => {
                            setIncident(item);
                            setAction("resolve");
                          }}
                        >
                          Resolve incident
                        </button>
                      </div>
                      {item.status === "open" &&
                        (current.stale ||
                          current.latest?.status !== "healthy") && (
                          <p className="subtle">
                            Resolution requires a recent healthy check.
                          </p>
                        )}
                    </article>
                  ))}
                </div>
                {!current.incidents.length && (
                  <p className="integration-status">
                    No incidents have been recorded for this application.
                  </p>
                )}
              </section>
              <section className="integration-section">
                <h3>Observation history</h3>
                <div className="feature-list">
                  {current.observations?.map((item) => (
                    <article key={item.id}>
                      <header>
                        <time>{date(item.created_at)}</time>
                        <span className={`feature-status ${item.status}`}>
                          {item.status}
                        </span>
                      </header>
                      <p>{item.latency_ms.toFixed(1)} ms response latency</p>
                      {!!item.reasons.length && (
                        <ul className="feature-reasons">
                          {item.reasons.map((reason) => (
                            <li key={reason}>{reason}</li>
                          ))}
                        </ul>
                      )}
                      <details>
                        <summary>Recorded metrics</summary>
                        <pre>{JSON.stringify(item.metrics, null, 2)}</pre>
                      </details>
                    </article>
                  ))}
                </div>
                {!current.observations?.length && (
                  <p className="integration-status">
                    No observation history returned.
                  </p>
                )}
              </section>
            </article>
          )}
        </>
      )}
      {incident && (
        <IncidentAction
          connection={connection}
          providers={providers}
          incident={incident}
          action={action}
          onClose={() => setIncident(null)}
          onCompleted={(run) => {
            setIncident(null);
            setRefresh((value) => value + 1);
            if (run) onOpen(run);
          }}
        />
      )}
    </section>
  );
}

function ApplicationEditor({
  connection,
  repositories,
  targets,
  initial,
  onCancel,
  onSaved,
}: {
  connection: Connection;
  repositories: Repository[];
  targets: Target[];
  initial: MonitoredApplication | null;
  onCancel: () => void;
  onSaved: (app: MonitoredApplication) => void;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [step, setStep] = useState(0);
  const [identifier, setIdentifier] = useState(initial?.id ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [project, setProject] = useState(initial?.repository_id ?? "");
  const [target, setTarget] = useState(initial?.target_id ?? "");
  const [backend, setBackend] = useState(initial?.backend ?? "pyfly");
  const [frontend, setFrontend] = useState(initial?.frontend ?? "react");
  const [health, setHealth] = useState(initial?.health_url ?? "");
  const [metrics, setMetrics] = useState(initial?.metrics_url ?? "");
  const [interval, setInterval] = useState(initial?.interval_seconds ?? 60);
  const [latency, setLatency] = useState(initial?.max_latency_ms ?? 1000);
  const [thresholds, setThresholds] = useState<MetricThreshold[]>(
    initial?.metric_thresholds ?? [],
  );
  const [links, setLinks] = useState(initial?.observability ?? {});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (step < 3) {
      setStep((value) => value + 1);
      return;
    }
    setBusy(true);
    try {
      const app = await request<MonitoredApplication>(
        connection,
        `/applications/${identifier}`,
        {
          method: "PUT",
          body: JSON.stringify({
            name,
            repository_id: project,
            target_id: target,
            backend,
            frontend,
            health_url: health,
            metrics_url: metrics || null,
            interval_seconds: interval,
            max_latency_ms: latency,
            metric_thresholds: thresholds,
            observability: links,
          }),
        },
      );
      onSaved(app);
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Wizard
      steps={["Application", "Health", "Metrics", "Review"]}
      active={step}
      onStep={setStep}
      title={
        [
          "Choose the application and target",
          "Configure the health check",
          "Set metric limits and links",
          "Review monitoring settings",
        ][step]
      }
      description={
        [
          "Choose an existing deployment target for this project.",
          "The server probes the configured endpoint and records its response.",
          "Metrics are optional. Set only limits that match the endpoint's actual metric names.",
          "Saving schedules future checks. It does not deploy or repair the application.",
        ][step]
      }
    >
      <SetupIllustration kind="application" step={step} />
      <form onSubmit={(event) => void submit(event)}>
        {step === 0 && (
          <>
            {field(
              "Application name",
              "application-name",
              <input
                id="application-name"
                required
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />,
            )}
            {field(
              "Application ID",
              "application-id",
              <input
                id="application-id"
                required
                pattern="[a-z][a-z0-9-]{0,63}"
                value={identifier}
                onChange={(event) => setIdentifier(event.target.value)}
              />,
            )}
            {field(
              "Application project",
              "application-project",
              <select
                id="application-project"
                value={project}
                required
                onChange={(event) => {
                  setProject(event.target.value);
                  setTarget("");
                }}
              >
                <option value="">Choose a repository</option>
                {repositories.map((repo) => (
                  <option key={repo.id} value={repo.id}>
                    {projectLabels.get(repo.id)}
                  </option>
                ))}
              </select>,
            )}
            {field(
              "Application target",
              "application-target",
              <select
                id="application-target"
                value={target}
                required
                onChange={(event) => setTarget(event.target.value)}
              >
                <option value="">Choose a target for this project</option>
                {targets
                  .filter((item) => item.repository_id === project)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
              </select>,
            )}
          </>
        )}
        {step === 0 && (
          <div className="two-columns">
            <div className="field">
              <label htmlFor="application-backend">Application backend</label>
              <select
                id="application-backend"
                value={backend}
                onChange={(event) => setBackend(event.target.value)}
              >
                <option value="pyfly">PyFly</option>
                <option value="larafly">LaraFly</option>
                <optgroup label="Additional profiles">
                  <option value="java">Java</option>
                  <option value="rust">Rust</option>
                  <option value="dotnet">.NET</option>
                </optgroup>
              </select>
            </div>
            <div className="field">
              <label htmlFor="application-frontend">Application frontend</label>
              <select
                id="application-frontend"
                value={frontend}
                onChange={(event) => setFrontend(event.target.value)}
              >
                {["react", "angular", "nextjs", "vue", "none"].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
            <p>
              Incident maintenance uses this application's configured stack.
            </p>
          </div>
        )}
        {step === 1 && (
          <>
            {field(
              "Health endpoint",
              "application-health",
              <input
                id="application-health"
                type="url"
                required
                value={health}
                onChange={(event) => setHealth(event.target.value)}
                placeholder="https://your-application.example/health"
              />,
              "HTTPS, or HTTP on loopback for local checks. Do not include credentials in the URL.",
            )}
            {field(
              "Check interval (seconds)",
              "application-interval",
              <input
                id="application-interval"
                type="number"
                min={30}
                required
                value={interval}
                onChange={(event) => setInterval(Number(event.target.value))}
              />,
            )}
            {field(
              "Maximum response latency (ms)",
              "application-latency",
              <input
                id="application-latency"
                type="number"
                min={1}
                required
                value={latency}
                onChange={(event) => setLatency(Number(event.target.value))}
              />,
            )}
          </>
        )}
        {step === 2 && (
          <>
            {field(
              "Metrics endpoint",
              "application-metrics",
              <input
                id="application-metrics"
                type="url"
                value={metrics}
                onChange={(event) => setMetrics(event.target.value)}
                placeholder="https://your-application.example/metrics"
              />,
              "Optional Prometheus text endpoint. Names and values come from recorded checks.",
            )}
            <h3>Metric limits</h3>
            {thresholds.map((threshold, index) => (
              <div className="feature-threshold" key={index}>
                {field(
                  "Metric name",
                  `metric-name-${index}`,
                  <input
                    id={`metric-name-${index}`}
                    required
                    value={threshold.name}
                    onChange={(event) =>
                      setThresholds((values) =>
                        values.map((value, item) =>
                          item === index
                            ? { ...value, name: event.target.value }
                            : value,
                        ),
                      )
                    }
                  />,
                )}
                {field(
                  "Degraded when",
                  `metric-operator-${index}`,
                  <select
                    id={`metric-operator-${index}`}
                    value={threshold.operator}
                    onChange={(event) =>
                      setThresholds((values) =>
                        values.map((value, item) =>
                          item === index
                            ? {
                                ...value,
                                operator: event.target.value as "gt" | "lt",
                              }
                            : value,
                        ),
                      )
                    }
                  >
                    <option value="gt">Above</option>
                    <option value="lt">Below</option>
                  </select>,
                )}
                {field(
                  "Limit",
                  `metric-value-${index}`,
                  <input
                    id={`metric-value-${index}`}
                    type="number"
                    step="any"
                    required
                    value={threshold.value}
                    onChange={(event) =>
                      setThresholds((values) =>
                        values.map((value, item) =>
                          item === index
                            ? { ...value, value: Number(event.target.value) }
                            : value,
                        ),
                      )
                    }
                  />,
                )}
                <button
                  type="button"
                  className="quiet"
                  aria-label={`Remove metric limit ${index + 1}`}
                  onClick={() =>
                    setThresholds((values) =>
                      values.filter((_, item) => item !== index),
                    )
                  }
                >
                  <X size={16} />
                </button>
              </div>
            ))}
            <button
              type="button"
              className="secondary"
              onClick={() =>
                setThresholds((values) => [
                  ...values,
                  { name: "", operator: "gt", value: 0 },
                ])
              }
            >
              Add metric limit
            </button>
            <div className="integration-section">
              <h3>Observability links</h3>
              {(["logs_url", "traces_url", "dashboard_url"] as const).map(
                (key) =>
                  field(
                    key === "dashboard_url"
                      ? "Dashboard link"
                      : key === "logs_url"
                        ? "Logs link"
                        : "Traces link",
                    `application-${key}`,
                    <input
                      key={key}
                      id={`application-${key}`}
                      type="url"
                      value={links[key] ?? ""}
                      onChange={(event) =>
                        setLinks((current) => {
                          const next = { ...current };
                          if (event.target.value)
                            next[key] = event.target.value;
                          else delete next[key];
                          return next;
                        })
                      }
                    />,
                  ),
              )}
            </div>
          </>
        )}
        {step === 3 && (
          <dl className="wizard-summary">
            <div>
              <dt>Application</dt>
              <dd>
                {name} ({identifier})
              </dd>
            </div>
            <div>
              <dt>Project / target</dt>
              <dd>
                {repositories.find((repo) => repo.id === project)?.name} /{" "}
                {targets.find((item) => item.id === target)?.name}
              </dd>
            </div>
            <div>
              <dt>Maintenance stack</dt>
              <dd>
                {backend} / {frontend}
              </dd>
            </div>
            <div>
              <dt>Health check</dt>
              <dd>
                {health}
                <br />
                Every {interval} seconds, maximum {latency} ms
              </dd>
            </div>
            <div>
              <dt>Metrics</dt>
              <dd>
                {metrics || "Not configured"}
                {thresholds.map((item) => (
                  <p key={item.name}>
                    {item.name} {item.operator === "gt" ? "above" : "below"}{" "}
                    {item.value}
                  </p>
                ))}
              </dd>
            </div>
            <div>
              <dt>Observability</dt>
              <dd>
                {Object.values(links).join("\n") || "No links configured"}
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
          <button
            type="button"
            className="secondary"
            onClick={() => (step ? setStep((value) => value - 1) : onCancel())}
          >
            <ArrowLeft size={16} />
            {step ? "Back" : "Cancel"}
          </button>
          <button className="primary" disabled={busy}>
            {step === 3
              ? "Save application"
              : step === 2
                ? "Review application"
                : "Continue"}
            <ArrowRight size={16} />
          </button>
        </div>
      </form>
    </Wizard>
  );
}

function IncidentAction({
  connection,
  providers,
  incident,
  action,
  onClose,
  onCompleted,
}: {
  connection: Connection;
  providers: unknown;
  incident: ApplicationIncident;
  action: "maintenance" | "resolve";
  onClose: () => void;
  onCompleted: (run?: ForgeRun) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [brief, setBrief] = useState("");
  const [agent, setAgent] = useState("codex");
  const [commands, setCommands] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const catalog = Array.isArray(providers)
    ? (providers as Provider[])
    : providers && typeof providers === "object" && "providers" in providers
      ? (providers as { providers: Provider[] }).providers
      : [];
  const ready = catalog.find((item) => item.id === agent)?.ready === true;
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (action === "resolve") {
        await post(connection, `/incidents/${incident.id}/resolve`, {
          resolution: brief,
        });
        onCompleted();
      } else {
        const checks = parseCommands(commands);
        if (!checks.length)
          throw new Error("Add at least one executable maintenance check.");
        const run = await post<ForgeRun>(
          connection,
          `/incidents/${incident.id}/maintenance`,
          { brief, agent, commands: checks },
        );
        onCompleted(run);
      }
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="feature-dialog"
      aria-labelledby="incident-action-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header>
        <h2 id="incident-action-title">
          {action === "resolve" ? "Resolve incident" : "Start maintenance"}
        </h2>
        <button
          className="quiet"
          onClick={onClose}
          aria-label="Close incident action"
        >
          <X size={18} />
        </button>
      </header>
      <p className="subtle">{incident.summary}</p>
      <form onSubmit={(event) => void submit(event)}>
        {field(
          action === "resolve" ? "Resolution" : "Maintenance instructions",
          "incident-brief",
          <textarea
            id="incident-brief"
            required
            minLength={10}
            rows={5}
            value={brief}
            onChange={(event) => setBrief(event.target.value)}
          />,
        )}
        {action === "maintenance" && (
          <>
            {field(
              "Maintenance agent",
              "incident-agent",
              <select
                id="incident-agent"
                value={agent}
                onChange={(event) => setAgent(event.target.value)}
              >
                {["codex", "claude", "bridge", "agentic"].map((id) => (
                  <option key={id} value={id}>
                    {catalog.find((item) => item.id === id)?.name ?? id}
                  </option>
                ))}
              </select>,
            )}
            {!ready && (
              <p className="integration-status">
                Configure and check this coding provider before starting
                maintenance.
              </p>
            )}
            {field(
              "Maintenance checks",
              "incident-commands",
              <textarea
                id="incident-commands"
                required
                rows={4}
                value={commands}
                onChange={(event) => setCommands(event.target.value)}
                placeholder='[["make","check"]]'
              />,
              "Real commands verify the repair. A maintenance run does not deploy the result automatically.",
            )}
          </>
        )}
        {error && (
          <div className="alert" role="alert">
            {error}
          </div>
        )}
        <div className="feature-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary"
            disabled={busy || (action === "maintenance" && !ready)}
          >
            {action === "resolve"
              ? "Resolve incident"
              : "Queue maintenance run"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
