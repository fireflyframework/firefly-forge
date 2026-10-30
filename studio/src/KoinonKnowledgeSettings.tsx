/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState, type FormEvent } from "react";
import { post, request, type Connection, type Repository } from "./api";
import {
  koinonConfiguration,
  type KoinonSettings,
} from "./KoinonKnowledgeData";
import { repositoryLabels } from "./RepositoryLabels";

export function KoinonKnowledgeSettings({
  settings,
  connection,
  repositories,
  onSaved,
}: {
  settings: KoinonSettings;
  connection: Connection;
  repositories: Repository[];
  onSaved: (value: KoinonSettings) => void;
}) {
  const [repository, setRepository] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const labels = repositoryLabels(repositories);
  const scope = settings.project_scopes[repository];
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    const form = event.currentTarget;
    try {
      const body = koinonConfiguration(new FormData(form), settings);
      const result = await request<KoinonSettings>(
        connection,
        "/knowledge/koinon/configuration",
        { method: "PUT", body: JSON.stringify(body) },
      );
      onSaved(result);
      const token = form.elements.namedItem("access_token");
      if (token instanceof HTMLInputElement) token.value = "";
      setNotice(
        "Koinon mapping saved. Check access, then run a cited project search.",
      );
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Koinon configuration could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function check() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await post<KoinonSettings>(connection, "/knowledge/test", {
        repository_id: repository,
      });
      onSaved(result);
      setNotice(result.readiness.detail);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Koinon access could not be checked.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className="integration-card koinon-knowledge-settings">
      <div>
        <h3>koinon knowledge connection</h3>
        <p>
          Map each repository to its existing Koinon catalog scope. Use a scoped
          machine token; browser login cookies are not used.
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
            <label htmlFor="koinon-endpoint">Koinon API address</label>
            <input
              id="koinon-endpoint"
              name="endpoint"
              type="url"
              defaultValue={settings.endpoint}
              placeholder="https://knowledge.example/api/v1"
              required
            />
          </div>
          <div className="field">
            <label htmlFor="koinon-tenant">Tenant ID</label>
            <input
              id="koinon-tenant"
              name="tenant_id"
              defaultValue={settings.tenant_id}
              required
            />
          </div>
        </div>
        <div className="field">
          <label htmlFor="koinon-token">Scoped machine token</label>
          <input
            id="koinon-token"
            name="access_token"
            type="password"
            autoComplete="off"
            placeholder={
              settings.access_token_configured
                ? "Stored; leave blank to keep"
                : "Enter the Koinon machine token"
            }
          />
          {settings.access_token_configured && (
            <label className="check-choice">
              <input type="checkbox" name="clear_access_token" />
              Clear stored token
            </label>
          )}
        </div>
        <div className="field">
          <label htmlFor="koinon-repository">Repository mapping</label>
          <select
            id="koinon-repository"
            name="repository_id"
            value={repository}
            onChange={(event) => setRepository(event.target.value)}
            required
          >
            <option value="">Choose a repository</option>
            {repositories.map((item) => (
              <option key={item.id} value={item.id}>
                {labels.get(item.id)}
              </option>
            ))}
          </select>
          <small>
            Use catalog IDs from Koinon. A repository name does not establish a
            workspace mapping.
          </small>
        </div>
        <fieldset key={repository} disabled={!repository}>
          <legend>Selected repository scope</legend>
          <div className="two-columns">
            <div className="field">
              <label htmlFor="koinon-workspace">Workspace catalog ID</label>
              <input
                id="koinon-workspace"
                name="workspace_id"
                defaultValue={scope?.workspace_id ?? ""}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="koinon-project">Project catalog ID</label>
              <input
                id="koinon-project"
                name="project_id"
                defaultValue={scope?.project_id ?? ""}
                required
              />
            </div>
          </div>
          <div className="field">
            <label htmlFor="koinon-collection">
              Collection catalog ID (optional)
            </label>
            <input
              id="koinon-collection"
              name="collection_id"
              defaultValue={scope?.collection_id ?? ""}
            />
          </div>
          <div className="field">
            <label htmlFor="koinon-datasets">
              Dataset catalog IDs (optional)
            </label>
            <input
              id="koinon-datasets"
              name="dataset_ids"
              defaultValue={scope?.dataset_ids.join(", ") ?? ""}
            />
            <small>
              Comma-separated IDs. Schema and SQL access stays within these
              datasets.
            </small>
          </div>
        </fieldset>
        <details>
          <summary>Shared framework scope and private CA</summary>
          <div className="two-columns">
            <div className="field">
              <label htmlFor="koinon-framework-workspace">
                Framework workspace ID
              </label>
              <input
                id="koinon-framework-workspace"
                name="framework_workspace_id"
                defaultValue={settings.framework_scope?.workspace_id ?? ""}
              />
            </div>
            <div className="field">
              <label htmlFor="koinon-framework-project">
                Framework project ID
              </label>
              <input
                id="koinon-framework-project"
                name="framework_project_id"
                defaultValue={settings.framework_scope?.project_id ?? ""}
              />
            </div>
          </div>
          <div className="field">
            <label htmlFor="koinon-framework-collection">
              Framework collection ID
            </label>
            <input
              id="koinon-framework-collection"
              name="framework_collection_id"
              defaultValue={settings.framework_scope?.collection_id ?? ""}
            />
          </div>
          {settings.framework_scope && (
            <label className="check-choice">
              <input type="checkbox" name="clear_framework_scope" />
              Remove the shared framework mapping
            </label>
          )}
          <div className="field">
            <label htmlFor="koinon-ca">Private CA certificate (optional)</label>
            <textarea
              id="koinon-ca"
              name="ca_certificate"
              defaultValue={settings.ca_certificate}
              rows={3}
            />
          </div>
        </details>
        <div className="field">
          <label htmlFor="koinon-results">Results per mapped scope</label>
          <input
            id="koinon-results"
            name="top_k"
            type="number"
            min={1}
            max={100}
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
          Pause new work when required context is unavailable
        </label>
        <div className="integration-actions">
          <button className="primary" disabled={busy || !repository}>
            Save Koinon mapping
          </button>
          <button
            type="button"
            className="secondary"
            disabled={busy || !repository || !scope}
            onClick={() => void check()}
          >
            Check mapped access
          </button>
        </div>
      </form>
      <p className="integration-status">{settings.readiness.detail}</p>
      <p className="integration-status">
        This candidate supports scoped retrieval, versioned chunk reads, schema
        and explicit SQL. Source ingestion, memory publication, framework
        refresh and model-generated queries still require their migration
        workflows. A successful access check verifies the mapped catalog only.
      </p>
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
    </article>
  );
}
