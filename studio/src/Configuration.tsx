/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState, type FormEvent } from "react";
import {
  BookOpen,
  Check,
  ExternalLink,
  Loader2,
  Settings2,
  ShieldCheck,
  Terminal,
} from "lucide-react";
import { post, request, type Connection, type Repository } from "./api";

export type CodingProvider = {
  id: string;
  name: string;
  ready: boolean;
  detail: string;
  auth_mode?: string;
  model?: string;
  endpoint?: string;
  tools?: string[];
  capabilities?: string[];
  setup_url?: string;
  config?: Record<string, string | boolean>;
  fields?: {
    key: string;
    label: string;
    secret: boolean;
    required: boolean;
    placeholder: string;
  }[];
  readiness?: { state: string; detail: string; checked_at: string | null };
};
export type Policy = {
  id: string;
  name: string;
  description: string;
  instructions: string;
  category: string;
  kind: string;
  default: boolean;
};
type Catalog = {
  skills: Policy[];
  revision: string;
  digest: string;
  source: { kind: string; repository_url?: string };
};
type Workflow = {
  stages: {
    id: string;
    name: string;
    description: string;
    roles: string[];
    tools: string[];
  }[];
  agents: {
    id: string;
    name: string;
    description: string;
    provider_independent: boolean;
  }[];
  tools: { id: string; name: string; description: string }[];
};
const message = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The request could not be completed.";

export function ProviderConfiguration({
  provider,
  connection,
  onUpdated,
}: {
  provider: CodingProvider;
  connection: Connection;
  onUpdated: () => void;
}) {
  const [current, setCurrent] = useState(provider);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [edit, setEdit] = useState(false);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    const data = new FormData(event.currentTarget);
    const values = Object.fromEntries(
      (current.fields ?? []).map((field) => [
        field.key,
        String(data.get(field.key) ?? "").trim(),
      ]),
    );
    const clear_secrets = (current.fields ?? [])
      .filter((field) => field.secret && data.get("clear-" + field.key))
      .map((field) => field.key);
    try {
      const result = await request<CodingProvider>(
        connection,
        `/providers/${current.id}/configuration`,
        { method: "PUT", body: JSON.stringify({ values, clear_secrets }) },
      );
      setCurrent(result);
      setEdit(false);
      setNotice("Settings saved. Check the connection before starting a run.");
      onUpdated();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function check() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await post<CodingProvider>(
        connection,
        `/providers/${current.id}/test`,
      );
      setCurrent(result);
      setNotice(result.readiness?.detail ?? result.detail);
      onUpdated();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className="provider-card">
      <div className="provider-card-heading">
        <span className="provider-icon">
          <Terminal size={20} />
        </span>
        <div>
          <h3>{current.name}</h3>
          <p>
            {current.auth_mode?.replaceAll("_", " ") ||
              "Server execution provider"}
          </p>
        </div>
        <span
          className={
            "badge " +
            (["authenticated", "connected"].includes(
              current.readiness?.state ?? "",
            )
              ? "success"
              : "")
          }
        >
          {current.readiness?.state ??
            (current.ready ? "configured" : "missing")}
        </span>
      </div>
      <p className="provider-detail">{current.detail}</p>
      {current.model && (
        <div className="configuration-value">
          <span>Model</span>
          <strong>{current.model}</strong>
        </div>
      )}
      {!!current.tools?.length && (
        <div className="capability-list" aria-label={`${current.name} tools`}>
          {current.tools.map((tool) => (
            <span key={tool}>{tool.replaceAll("_", " ")}</span>
          ))}
        </div>
      )}
      <div className="card-actions">
        <button
          className="secondary"
          type="button"
          disabled={busy}
          onClick={() => setEdit(!edit)}
        >
          <Settings2 size={15} />
          {edit ? "Close settings" : "Configure"}
        </button>
        <button
          className="quiet"
          type="button"
          disabled={busy}
          onClick={() => void check()}
        >
          {busy ? (
            <Loader2 size={15} className="spin" />
          ) : (
            <ShieldCheck size={15} />
          )}
          Check connection
        </button>
        {current.setup_url && (
          <a href={current.setup_url} target="_blank" rel="noreferrer">
            Provider setup <ExternalLink size={13} />
          </a>
        )}
      </div>
      {edit && (
        <form
          className="provider-config-form"
          onSubmit={(event) => void save(event)}
        >
          {(current.fields ?? []).map((field) => (
            <div className="field" key={field.key}>
              <label htmlFor={`${current.id}-${field.key}`}>
                {field.label}
              </label>
              <input
                id={`${current.id}-${field.key}`}
                name={field.key}
                type={field.secret ? "password" : "text"}
                defaultValue={
                  field.secret ? "" : String(current.config?.[field.key] ?? "")
                }
                autoComplete="off"
                placeholder={
                  field.secret && current.config?.[field.key + "_configured"]
                    ? "Configured; leave blank to keep it"
                    : field.placeholder
                }
              />
              {field.secret && current.config?.[field.key + "_configured"] && (
                <label className="check-choice">
                  <input type="checkbox" name={"clear-" + field.key} />
                  Remove the stored {field.label.toLowerCase()}
                </label>
              )}
            </div>
          ))}
          <small>
            Secrets are stored on the server and never returned to this page.
          </small>
          <button className="primary" disabled={busy}>
            <Check size={15} />
            Save {current.name} settings
          </button>
        </form>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="configuration-feedback" role="status">
          {notice}
        </p>
      )}
    </article>
  );
}

export function Workshop({
  connection,
  connected,
  repositories,
}: {
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [repository, setRepository] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    Promise.all([
      request<Catalog>(connection, "/policies", { signal: controller.signal }),
      request<Workflow>(connection, "/workflow", { signal: controller.signal }),
    ])
      .then(([policies, stages]) => {
        setCatalog(policies);
        setWorkflow(stages);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(message(cause));
      });
    return () => controller.abort();
  }, [connection, connected]);
  useEffect(() => {
    if (!repository) return;
    const controller = new AbortController();
    request<{ skills: string[] }>(
      connection,
      `/repositories/${repository}/policies`,
      { signal: controller.signal },
    )
      .then((result) => setSelected(result.skills))
      .catch((cause) => {
        if (!controller.signal.aborted) setError(message(cause));
      });
    return () => controller.abort();
  }, [connection, repository]);
  async function saveProject() {
    setBusy(true);
    setError("");
    try {
      await request(connection, `/repositories/${repository}/policies`, {
        method: "PUT",
        body: JSON.stringify({ skills: selected }),
      });
      setNotice("Project policies saved. New runs inherit this selection.");
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function sync(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      setCatalog(
        await post<Catalog>(connection, "/policies/sync", {
          repository_url: String(data.get("repository_url")),
          ref: String(data.get("ref")),
          catalog_path: "catalog.json",
        }),
      );
      setNotice(
        "The catalog is pinned to the selected commit. Existing runs retain their policy snapshot.",
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Agents, skills and policies</h1>
          <p>
            Choose project guidance and inspect the tools and stages that
            execute each run.
          </p>
        </div>
        <BookOpen size={26} />
      </div>
      {!connected && (
        <div className="empty">
          <h3>Connect your API first</h3>
          <p>The catalog and project settings come from your forge server.</p>
        </div>
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
      {catalog && (
        <section className="panel catalog-panel">
          <div className="section-heading">
            <div>
              <h2>Project policies</h2>
              <p className="subtle">
                Catalog {catalog.revision}. Recommendations guide the worker;
                quality gates execute commands.
              </p>
            </div>
            <span className="badge">
              {catalog.source?.kind === "git"
                ? "Central repository"
                : "Local catalog"}
            </span>
          </div>
          <div className="field">
            <label htmlFor="policy-project">Project repository</label>
            <select
              id="policy-project"
              value={repository}
              onChange={(event) => {
                setRepository(event.target.value);
                setNotice("");
              }}
            >
              <option value="">Select a project to edit its defaults</option>
              {repositories.map((repo) => (
                <option value={repo.id} key={repo.id}>
                  {repo.name}
                </option>
              ))}
            </select>
          </div>
          <div className="policy-grid">
            {catalog.skills.map((policy) => (
              <label className="policy-choice" key={policy.id}>
                <input
                  type="checkbox"
                  checked={
                    repository ? selected.includes(policy.id) : policy.default
                  }
                  disabled={!repository || busy}
                  onChange={(event) =>
                    setSelected((values) =>
                      event.target.checked
                        ? [...values, policy.id]
                        : values.filter((id) => id !== policy.id),
                    )
                  }
                />
                <span>
                  <strong>{policy.name}</strong>
                  <small>{policy.description}</small>
                  <em>
                    {policy.kind === "quality_gate"
                      ? "Executable quality gate"
                      : "Worker recommendation"}
                  </em>
                </span>
              </label>
            ))}
          </div>
          <div className="form-footer">
            <p>
              Workspace isolation, credential protection and passing
              verification remain mandatory.
            </p>
            <button
              className="primary"
              disabled={!repository || busy}
              onClick={() => void saveProject()}
            >
              Save project policies
            </button>
          </div>
          <details className="catalog-sync">
            <summary>Use a central policy repository</summary>
            <form onSubmit={(event) => void sync(event)}>
              <div className="field">
                <label htmlFor="catalog-repository">
                  Policy repository URL
                </label>
                <input
                  id="catalog-repository"
                  name="repository_url"
                  defaultValue="https://github.com/fireflyframework/firefly-forge-policies.git"
                  required
                />
              </div>
              <div className="field">
                <label htmlFor="catalog-ref">Pinned commit</label>
                <input
                  id="catalog-ref"
                  name="ref"
                  pattern="[a-f0-9]{40}"
                  placeholder="Full 40-character Git commit"
                  required
                />
              </div>
              <p className="subtle">
                Only the validated JSON catalog is imported. Source code in the
                policy repository is never executed.
              </p>
              <button className="secondary" disabled={busy}>
                Sync catalog
              </button>
            </form>
          </details>
        </section>
      )}
      {workflow && (
        <>
          <section className="workflow-section">
            <h2>Execution stages</h2>
            <ol className="workflow-stages">
              {workflow.stages?.map((stage) => (
                <li key={stage.id}>
                  <strong>{stage.name}</strong>
                  <p>{stage.description}</p>
                  <small>{stage.roles?.join(", ")}</small>
                </li>
              ))}
            </ol>
          </section>
          <section className="workflow-section">
            <h2>Execution roles</h2>
            <div className="role-grid">
              {workflow.agents?.map((agent) => (
                <article className="panel" key={agent.id}>
                  <h3>{agent.name}</h3>
                  <p>{agent.description}</p>
                  <span className="badge">
                    {agent.provider_independent
                      ? "Factory service"
                      : "Selected coding provider"}
                  </span>
                </article>
              ))}
            </div>
          </section>
          <section className="workflow-section">
            <h2>Available tools</h2>
            <dl className="tool-list">
              {workflow.tools?.map((tool) => (
                <div key={tool.id}>
                  <dt>{tool.name}</dt>
                  <dd>{tool.description}</dd>
                </div>
              ))}
            </dl>
          </section>
        </>
      )}
    </>
  );
}
