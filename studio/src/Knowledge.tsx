/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState, type FormEvent } from "react";
import { post, request, type Connection, type Repository } from "./api";

type Settings = {
  enabled: boolean;
  endpoint: string;
  tenant_id: string;
  framework_workspace: string;
  required: boolean;
  auto_memory: boolean;
  api_key_configured: boolean;
  agent_token_configured: boolean;
  top_k: number;
  readiness: { state: string; detail: string };
};
type Hit = {
  source_id?: string;
  source_title?: string;
  source_filename?: string;
  source_uri?: string;
  workspace_id: string;
  content: string;
  score?: number;
  chunk_id?: string;
};
const errorMessage = (cause: unknown) =>
  cause instanceof Error ? cause.message : "Knowledge request failed.";

export function Knowledge({
  connection,
  repositories,
}: {
  connection: Connection;
  repositories: Repository[];
}) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [project, setProject] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [searched, setSearched] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    request<Settings>(connection, "/knowledge/configuration", {
      signal: controller.signal,
    })
      .then(setSettings)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      });
    return () => controller.abort();
  }, [connection]);
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const values = {
        enabled: data.has("enabled"),
        endpoint: String(data.get("endpoint")),
        tenant_id: String(data.get("tenant_id")),
        framework_workspace: String(data.get("framework_workspace")),
        required: data.has("required"),
        auto_memory: data.has("auto_memory"),
        api_key: String(data.get("api_key")),
        agent_token: String(data.get("agent_token")),
        top_k: Number(data.get("top_k")),
      };
      setSettings(
        await request<Settings>(connection, "/knowledge/configuration", {
          method: "PUT",
          body: JSON.stringify({
            values,
            clear_secrets: data.getAll("clear_secrets"),
          }),
        }),
      );
      for (const name of ["api_key", "agent_token"]) {
        const input = form.elements.namedItem(name);
        if (input instanceof HTMLInputElement) input.value = "";
      }
      setNotice(
        "Knowledge settings saved. Check access and search to verify retrieval.",
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  async function check() {
    setBusy(true);
    setError("");
    try {
      const result = await post<Settings>(connection, "/knowledge/test", {});
      setSettings(result);
      setNotice(result.readiness.detail);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSearched(false);
    const query = String(new FormData(event.currentTarget).get("query"));
    try {
      const result = await post<{ hits: Hit[]; warnings: string[] }>(
        connection,
        "/knowledge/search",
        { repository_id: project, query },
      );
      setHits(result.hits);
      setSearched(true);
      setNotice(
        result.warnings.join(" ") ||
          `${result.hits.length} cited results retrieved.`,
      );
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="knowledge-setup">
      {settings && (
        <article className="integration-card">
          <div>
            <h3>flycanon knowledge and memory</h3>
            <p>
              Retrieve framework guidance and project context. Verified run
              summaries can be saved in each project's own workspace.
            </p>
          </div>
          <form onSubmit={(event) => void save(event)}>
            <label className="check-choice">
              <input
                type="checkbox"
                name="enabled"
                defaultChecked={settings.enabled}
              />
              Enable knowledge retrieval
            </label>
            <div className="two-columns">
              <div className="field">
                <label htmlFor="knowledge-endpoint">flycanon address</label>
                <input
                  id="knowledge-endpoint"
                  name="endpoint"
                  defaultValue={settings.endpoint}
                  placeholder="https://knowledge.example.com"
                />
              </div>
              <div className="field">
                <label htmlFor="knowledge-tenant">Tenant</label>
                <input
                  id="knowledge-tenant"
                  name="tenant_id"
                  defaultValue={settings.tenant_id}
                  placeholder="engineering"
                />
              </div>
            </div>
            <div className="field">
              <label htmlFor="knowledge-frameworks">
                Shared framework workspace
              </label>
              <input
                id="knowledge-frameworks"
                name="framework_workspace"
                defaultValue={settings.framework_workspace}
                placeholder="frameworks"
              />
              <small>
                Optional. Project source and memory use a separate workspace for
                each repository.
              </small>
            </div>
            <div className="two-columns">
              {(["api_key", "agent_token"] as const).map((key) => (
                <div className="field" key={key}>
                  <label htmlFor={`knowledge-${key}`}>
                    {key === "api_key"
                      ? "Platform API key"
                      : "Scoped agent token"}
                  </label>
                  <input
                    id={`knowledge-${key}`}
                    name={key}
                    type="password"
                    autoComplete="off"
                    placeholder={
                      settings[`${key}_configured`]
                        ? "Stored; leave blank to keep"
                        : "Enter credential"
                    }
                  />
                  {settings[`${key}_configured`] && (
                    <label className="check-choice">
                      <input type="checkbox" name="clear_secrets" value={key} />
                      Clear stored credential
                    </label>
                  )}
                </div>
              ))}
            </div>
            <div className="field">
              <label htmlFor="knowledge-top-k">Results per workspace</label>
              <input
                id="knowledge-top-k"
                name="top_k"
                type="number"
                min={1}
                max={8}
                defaultValue={settings.top_k}
                required
              />
            </div>
            <label className="check-choice">
              <input
                type="checkbox"
                name="required"
                defaultChecked={settings.required}
              />
              Pause new work if required context cannot be retrieved
            </label>
            <label className="check-choice">
              <input
                type="checkbox"
                name="auto_memory"
                defaultChecked={settings.auto_memory}
              />
              Save verified run summaries as project memory
            </label>
            <div className="integration-actions">
              <button className="primary" disabled={busy}>
                Save knowledge settings
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void check()}
              >
                Check knowledge access
              </button>
            </div>
          </form>
          <p className="integration-status">{settings.readiness.detail}</p>
        </article>
      )}
      {settings?.enabled && (
        <section className="integration-section">
          <h2>Try a project search</h2>
          <form
            className="integration-card"
            onSubmit={(event) => void search(event)}
          >
            <div className="field">
              <label htmlFor="knowledge-project">Search project</label>
              <select
                id="knowledge-project"
                value={project}
                onChange={(event) => setProject(event.target.value)}
                required
              >
                <option value="">Choose a project</option>
                {repositories.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="knowledge-query">Search question</label>
              <input
                id="knowledge-query"
                name="query"
                required
                maxLength={4000}
                placeholder="How does this product validate reservations?"
              />
            </div>
            <button
              className="secondary"
              disabled={busy || !repositories.length}
            >
              Search project knowledge
            </button>
          </form>
          <div className="integration-section integration-grid">
            {hits.map((hit, index) => (
              <article
                className="integration-result"
                key={`${hit.chunk_id}-${index}`}
              >
                <strong>
                  {hit.source_title ||
                    hit.source_filename ||
                    hit.source_id ||
                    "Retrieved source"}
                </strong>
                <p>{hit.content}</p>
                <small>
                  Workspace: {hit.workspace_id}
                  {typeof hit.score === "number"
                    ? ` · Score: ${hit.score.toFixed(3)}`
                    : ""}
                </small>
                {hit.source_uri && /^https?:\/\//.test(hit.source_uri) && (
                  <a
                    className="text-button"
                    href={hit.source_uri}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Read source
                  </a>
                )}
              </article>
            ))}
          </div>
          {searched && !hits.length && (
            <p className="integration-status">
              No matching source was retrieved. Index verified project files or
              add framework references in flycanon.
            </p>
          )}
        </section>
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
    </div>
  );
}
