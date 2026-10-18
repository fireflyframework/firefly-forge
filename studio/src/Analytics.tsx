/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Database,
  KeyRound,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import { readProjects, type ToolProject } from "./ExternalToolsSettings";
import { Wizard } from "./Wizard";
import {
  analyticsPayload,
  analyticsReadError,
  analyticsUuid,
  loadAnalyticsConfiguration,
  loadAnalyticsCatalog,
  loadAnalyticsTable,
  loadAnalyticsObject,
  saveAnalyticsConfiguration,
  type AnalyticsConfiguration,
  type AnalyticsDraft,
  type AnalyticsCatalog,
  type AnalyticsTable,
  type AnalyticsObject,
  type AnalyticsObservation,
} from "./AnalyticsData";
import "./Analytics.css";

const emptyDraft: AnalyticsDraft = {
  enabled: true,
  endpoint: "",
  tenant_id: "",
  workspace_id: "",
  dataset_id: "",
  agent_token: "",
};
export function Analytics({
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
          <h1>Structured analytics</h1>
          <p>
            Inspect the tables and schema reported by your project’s Flyquery
            connection.
          </p>
        </div>
        <Database size={26} />
      </div>
      {!session ? (
        <section className="access-notice">
          <ShieldCheck size={22} />
          <div>
            <h2>Sign in to read project analytics</h2>
            <p>
              Current project read access is required. A factory administrator
              manages the connection.
            </p>
          </div>
        </section>
      ) : (
        <ProjectSelection
          key={`${connection.base}:${session.user.id}`}
          connection={connection}
          administrator={session.global_admin}
        />
      )}
    </>
  );
}
function ProjectSelection({
  connection,
  administrator,
}: {
  connection: Connection;
  administrator: boolean;
}) {
  const [projects, setProjects] = useState<ToolProject[]>([]),
    [selected, setSelected] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setProjects([]);
    setSelected("");
    setError("");
    void request(connection, "/projects", {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
    })
      .then(readProjects)
      .then((items) => {
        if (active) {
          setProjects(items);
          setSelected(items[0]?.id ?? "");
        }
      })
      .catch(() => {
        if (active)
          setError(
            "Project choices could not be read. Check your session and reload projects.",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [connection, reload]);
  return (
    <div className="analytics-page">
      <section className="analytics-project" aria-label="Analytics project">
        <div className="field">
          <label htmlFor="analytics-project">Project</label>
          <select
            id="analytics-project"
            value={selected}
            disabled={loading || !projects.length}
            onChange={(e) => setSelected(e.target.value)}
          >
            <option value="">
              {loading ? "Loading projects…" : "Choose a project"}
            </option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.definition.name}
              </option>
            ))}
          </select>
        </div>
        <button
          className="secondary"
          disabled={loading}
          onClick={() => setReload((n) => n + 1)}
        >
          <RefreshCw size={16} />
          Reload projects
        </button>
      </section>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!loading && !projects.length && !error && (
        <section className="analytics-notice">
          <h2>No accessible projects</h2>
          <p>
            Ask a project owner to grant read access, or create a project before
            configuring analytics.
          </p>
        </section>
      )}
      {selected && (
        <AnalyticsProject
          key={`${selected}:${administrator}`}
          project={selected}
          connection={connection}
          administrator={administrator}
        />
      )}
    </div>
  );
}
function AnalyticsProject({
  project,
  connection,
  administrator,
}: {
  project: string;
  connection: Connection;
  administrator: boolean;
}) {
  const [saved, setSaved] = useState<AnalyticsConfiguration | null>(null),
    [draft, setDraft] = useState<AnalyticsDraft>(emptyDraft);
  const [editing, setEditing] = useState(false),
    [step, setStep] = useState(0),
    [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [uncertain, setUncertain] = useState(false);
  const [catalog, setCatalog] = useState<AnalyticsCatalog | null>(null),
    [table, setTable] = useState<AnalyticsObservation<AnalyticsTable> | null>(
      null,
    ),
    [object, setObject] =
      useState<AnalyticsObservation<AnalyticsObject> | null>(null),
    [objectId, setObjectId] = useState("");
  const live = useRef({ active: true, connection });
  live.current.connection = connection;
  const pending = useRef(false);
  useEffect(() => {
    const lifecycle = live.current;
    lifecycle.active = true;
    const controller = new AbortController();
    let active = true;
    if (administrator) {
      setBusy("configuration");
      setSaved(null);
      setCatalog(null);
      setTable(null);
      setObject(null);
      setDraft(emptyDraft);
      setEditing(false);
      void loadAnalyticsConfiguration(connection, project, controller.signal)
        .then((value) => {
          if (active) setSaved(value);
        })
        .catch(() => {
          if (active)
            setError(
              "Saved configuration could not be read. Reload it before editing.",
            );
        })
        .finally(() => {
          if (active) setBusy("");
        });
    }
    return () => {
      active = false;
      lifecycle.active = false;
      controller.abort();
    };
  }, [administrator, connection, project]);
  const current = () =>
    live.current.active && live.current.connection === connection;
  function clearReads() {
    setCatalog(null);
    setTable(null);
    setObject(null);
    setObjectId("");
  }
  async function readConfiguration() {
    if (pending.current) return;
    pending.current = true;
    setBusy("configuration");
    setError("");
    setNotice("");
    setDraft(emptyDraft);
    setEditing(false);
    clearReads();
    try {
      const result = await loadAnalyticsConfiguration(connection, project);
      if (current()) {
        setSaved(result);
        setUncertain(false);
        setNotice(
          "Current saved configuration loaded. An earlier save may still settle; revision checks reject conflicts.",
        );
      }
    } catch {
      if (current()) {
        setSaved(null);
        setError(
          "Saved configuration could not be read. Check administrator access and reload before editing.",
        );
      }
    } finally {
      pending.current = false;
      if (current()) setBusy("");
    }
  }
  function edit() {
    if (!saved || uncertain || busy) return;
    setDraft({
      ...(saved.configuration ?? emptyDraft),
      ...(saved.revision === 32 ? { enabled: false } : {}),
      agent_token: "",
    });
    setEditing(true);
    setStep(0);
    setReviewed(false);
    setError("");
    setNotice("");
    clearReads();
  }
  function cancel() {
    setDraft(emptyDraft);
    setEditing(false);
    setError("");
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending.current || busy || uncertain || !saved) return;
    if (!reviewed) {
      setError("Confirm the prerequisites before continuing.");
      return;
    }
    if (step === 0) {
      setStep(1);
      return;
    }
    try {
      analyticsPayload(saved, draft);
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Review the connection fields.",
      );
      return;
    }
    if (step === 1) {
      setError("");
      setStep(2);
      return;
    }
    pending.current = true;
    setBusy("save");
    setError("");
    setNotice("");
    const submitted = draft;
    setDraft((value) => ({ ...value, agent_token: "" }));
    clearReads();
    try {
      const result = await saveAnalyticsConfiguration(
        connection,
        project,
        saved,
        submitted,
      );
      if (current()) {
        setSaved(result);
        setEditing(false);
        setDraft(emptyDraft);
        setNotice(
          "Configuration saved. Read the catalog explicitly to check current access.",
        );
      }
    } catch {
      if (current()) {
        setUncertain(true);
        setError(
          "Save not confirmed. Reload the saved configuration before another save. The token has been cleared; an earlier save may still settle.",
        );
      }
    } finally {
      pending.current = false;
      if (current()) setBusy("");
    }
  }
  async function read(
    kind: "catalog" | "table" | "object",
    selected?: AnalyticsTable,
  ) {
    if (pending.current || busy || editing || uncertain) return;
    if (kind === "object") {
      try {
        analyticsUuid(objectId);
      } catch {
        setError(
          "Enter an exact schema-object UUID supplied by your data administrator.",
        );
        return;
      }
    }
    pending.current = true;
    setBusy(kind);
    setError("");
    setNotice("");
    setObject(null);
    if (kind === "catalog") clearReads();
    else if (kind === "table") {
      setTable(null);
      setObjectId("");
    }
    try {
      if (kind === "catalog") {
        const value = await loadAnalyticsCatalog(connection, project);
        if (current()) setCatalog(value);
      } else if (kind === "table" && selected && catalog) {
        const value = await loadAnalyticsTable(
          connection,
          project,
          selected.id,
          catalog,
        );
        if (current()) setTable(value);
      } else if (kind === "object" && table && catalog) {
        const value = await loadAnalyticsObject(
          connection,
          project,
          table.data,
          objectId,
          catalog,
        );
        if (current()) setObject(value);
      }
    } catch (e) {
      if (current()) {
        clearReads();
        setError(analyticsReadError(e));
      }
    } finally {
      pending.current = false;
      if (current()) setBusy("");
    }
  }
  return (
    <>
      <section
        className="analytics-notice"
        aria-label="Analytics access and limits"
      >
        <ShieldCheck size={22} />
        <div>
          <h2>Optional, read-only access</h2>
          <p>
            Flycanon remains required for knowledge and memory. This explorer
            reads the saved Flyquery scope; it cannot run SQL, ask a model,
            create data, or publish memory.
          </p>
          <p>
            Full field discovery and prepared results are unavailable in this
            SDK. Exact schema-object lookup requires a known object UUID.
          </p>
        </div>
      </section>
      {administrator ? (
        <section
          className="analytics-connection"
          aria-label="Saved analytics connection"
        >
          <div>
            <h2>Project connection</h2>
            <p>
              {saved
                ? `${saved.status === "configured" ? "Configured" : "Disabled"} · revision ${saved.revision}. ${saved.revision >= 32 ? (saved.configuration?.enabled ? "History is full. Only disabling this unchanged connection is available." : "History is full. This connection cannot be edited or re-enabled.") : "Saving does not verify remote access."}`
                : "Read the saved configuration before editing."}
            </p>
            {saved?.configuration && (
              <ScopeDetails scope={saved.configuration} />
            )}
          </div>
          <div className="analytics-actions">
            <button
              className="primary"
              disabled={
                !saved ||
                Boolean(busy) ||
                uncertain ||
                editing ||
                (saved.revision >= 32 && !saved.configuration?.enabled)
              }
              onClick={edit}
            >
              {saved?.revision === 32 && saved.configuration?.enabled
                ? "Disable connection"
                : saved && saved.revision >= 32
                  ? "Connection disabled"
                  : saved?.revision
                    ? "Edit connection"
                    : "Set up Flyquery"}
              <ArrowRight size={16} />
            </button>
            <button
              className="secondary"
              disabled={Boolean(busy)}
              onClick={() => void readConfiguration()}
            >
              <RefreshCw size={16} />
              Reload saved configuration
            </button>
          </div>
        </section>
      ) : (
        <p className="analytics-reader">
          You can read this project’s bound scope. Ask a factory administrator
          to configure or disable the connection.
        </p>
      )}
      {notice && (
        <p className="analytics-notice-text" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {editing && saved && (
        <AnalyticsWizard
          current={saved}
          draft={draft}
          onChange={setDraft}
          step={step}
          onStep={(next) => {
            setStep(next);
            setError("");
          }}
          reviewed={reviewed}
          onReviewed={setReviewed}
          busy={Boolean(busy)}
          blocked={uncertain}
          onCancel={cancel}
          onSubmit={(event) => void submit(event)}
        />
      )}
      {!editing && (
        <AnalyticsExplorer
          catalog={catalog}
          table={table}
          object={object}
          objectId={objectId}
          onObjectId={setObjectId}
          busy={Boolean(busy)}
          disabled={
            uncertain ||
            (administrator && (!saved || saved.status === "disabled"))
          }
          onCatalog={() => void read("catalog")}
          onTable={(value) => void read("table", value)}
          onObject={() => void read("object")}
        />
      )}
    </>
  );
}
export function ScopeDetails({
  scope,
}: {
  scope: { tenant_id: string; workspace_id: string; dataset_id: string };
}) {
  return (
    <dl className="analytics-scope">
      <div>
        <dt>Tenant</dt>
        <dd>{scope.tenant_id}</dd>
      </div>
      <div>
        <dt>Workspace</dt>
        <dd>{scope.workspace_id}</dd>
      </div>
      <div>
        <dt>Dataset</dt>
        <dd>{scope.dataset_id}</dd>
      </div>
    </dl>
  );
}
export function AnalyticsWizard({
  current,
  draft,
  onChange,
  step,
  onStep,
  reviewed,
  onReviewed,
  busy,
  blocked = false,
  onCancel,
  onSubmit,
}: {
  current: AnalyticsConfiguration;
  draft: AnalyticsDraft;
  onChange: (draft: AnalyticsDraft) => void;
  step: number;
  onStep: (step: number) => void;
  reviewed: boolean;
  onReviewed: (value: boolean) => void;
  busy: boolean;
  blocked?: boolean;
  onCancel: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  const update = (change: Partial<AnalyticsDraft>) =>
    onChange({ ...draft, ...change });
  const locked = busy || blocked;
  const terminalDisable = current.revision === 32;
  return (
    <Wizard
      pageFlow
      minimumFieldHeight={160}
      className="analytics-wizard"
      steps={["Requirements", "Connection", "Review"]}
      active={step}
      onStep={onStep}
      locked={locked}
      title={
        [
          terminalDisable
            ? "Prepare terminal disable"
            : "Prepare read-only access",
          terminalDisable
            ? "Disable the saved connection"
            : "Bind this project to Flyquery",
          terminalDisable
            ? "Review terminal disable"
            : "Review the project connection",
        ][step]
      }
      description={
        [
          terminalDisable
            ? "Review the final disable. The remote service does not need to be available."
            : "Confirm the service, SDK and scoped token before saving anything.",
          terminalDisable
            ? "Keep the saved scope unchanged and supply a token explicitly. This operation does not read the remote service."
            : "A factory administrator chooses the exact remote scope. Readers cannot override it.",
          terminalDisable
            ? "Commit terminal disabled revision 33. This binding cannot be edited or re-enabled afterward."
            : "Save one configuration revision. The catalog is read only when you choose Read catalog.",
        ][step]
      }
      guide={
        <>
          <Database size={32} />
          <h3>One project, one data scope</h3>
          <p>
            {terminalDisable
              ? "Disabling uses only the saved binding and current native administrator authority. It does not import the SDK or contact Flyquery."
              : "Use an already provisioned Flyquery service and dataset. The analytics extra must be installed in Forge."}
          </p>
          <p>
            The supported connection is an HTTPS origin on port 443 with a
            normally trusted certificate. Private CA settings and custom ports
            are unavailable here.
          </p>
          <p>
            Catalog reads are limited to 50 tables in a complete page. Schema
            objects are checked against the table’s reported current snapshot.
          </p>
        </>
      }
    >
      <form onSubmit={onSubmit}>
        {step === 0 && (
          <section className="form-section">
            <h3>Before you connect</h3>
            {terminalDisable && (
              <p>
                The 32 ordinary revisions are used. You can disable this saved
                scope once, in terminal revision 33. It cannot be edited or
                re-enabled afterward. Disabling does not contact Flyquery.
              </p>
            )}
            <ul className="analytics-requirements">
              {terminalDisable ? (
                <>
                  <li>Current factory administrator access.</li>
                  <li>The existing scope stays unchanged.</li>
                  <li>
                    Supply a scoped token explicitly, as required for every
                    save. It will not be checked with Flyquery.
                  </li>
                </>
              ) : (
                <>
                  <li>
                    Flyquery 26.10.4 with an existing tenant, workspace and
                    dataset.
                  </li>
                  <li>
                    Forge’s optional analytics SDK, installed by the operator.
                  </li>
                  <li>
                    A scoped agent token granting{" "}
                    <code>flyquery.schema:read</code> for the intended
                    workspace.
                  </li>
                  <li>
                    Current factory administrator access to save this binding.
                  </li>
                </>
              )}
            </ul>
            <label className="analytics-choice">
              <input
                type="checkbox"
                checked={reviewed}
                onChange={(e) => onReviewed(e.target.checked)}
                disabled={locked}
              />
              <span>
                These prerequisites are in place. This confirmation does not
                test the remote service.
              </span>
            </label>
          </section>
        )}
        {step === 1 && (
          <section className="form-section">
            <div className="field">
              <label htmlFor="analytics-endpoint">Flyquery HTTPS origin</label>
              <input
                id="analytics-endpoint"
                type="url"
                required
                value={draft.endpoint}
                maxLength={2048}
                placeholder="https://analytics.example"
                onChange={(e) => update({ endpoint: e.target.value })}
                disabled={locked || terminalDisable}
              />
            </div>
            <div className="field">
              <label htmlFor="analytics-tenant">Tenant identifier</label>
              <input
                id="analytics-tenant"
                required
                value={draft.tenant_id}
                maxLength={128}
                onChange={(e) => update({ tenant_id: e.target.value })}
                disabled={locked || terminalDisable}
              />
            </div>
            <div className="two-columns">
              <div className="field">
                <label htmlFor="analytics-workspace">Workspace UUID</label>
                <input
                  id="analytics-workspace"
                  required
                  value={draft.workspace_id}
                  maxLength={36}
                  onChange={(e) => update({ workspace_id: e.target.value })}
                  disabled={locked || terminalDisable}
                />
              </div>
              <div className="field">
                <label htmlFor="analytics-dataset">Dataset UUID</label>
                <input
                  id="analytics-dataset"
                  required
                  value={draft.dataset_id}
                  maxLength={36}
                  onChange={(e) => update({ dataset_id: e.target.value })}
                  disabled={locked || terminalDisable}
                />
              </div>
            </div>
            <div className="field">
              <label htmlFor="analytics-token">
                <KeyRound size={16} />
                Scoped agent token
              </label>
              <input
                id="analytics-token"
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                required
                value={draft.agent_token}
                maxLength={4096}
                onChange={(e) => update({ agent_token: e.target.value })}
                disabled={locked}
                aria-describedby="analytics-token-note"
              />
              <small id="analytics-token-note">
                {current.agent_token_configured
                  ? "A token is saved; its value is never returned. "
                  : ""}
                Enter a token for every save, including disabling. It is cleared
                after submission and not saved in browser storage.
              </small>
            </div>
            <label className="analytics-choice">
              <input
                type="checkbox"
                checked={draft.enabled}
                disabled={locked || terminalDisable}
                onChange={(e) => update({ enabled: e.target.checked })}
              />
              <span>Enable project readers to inspect this scope</span>
            </label>
          </section>
        )}
        {step === 2 && (
          <section className="form-section">
            <div className="analytics-review">
              <p>
                <strong>
                  {draft.enabled ? "Enable" : "Disable"} read-only access
                </strong>
              </p>
              <p className="analytics-address">{draft.endpoint}</p>
              <ScopeDetails scope={draft} />
              <p>
                Token:{" "}
                {draft.agent_token
                  ? "Replacement entered; value hidden"
                  : "Cleared after submission"}
              </p>
              <p>
                Expected saved revision: {current.revision}. A conflicting save
                is refused.
              </p>
            </div>
            <p>
              Saving does not create a dataset or verify a connection. Prepared
              results, full field discovery, SQL and model queries remain
              unavailable.
            </p>
          </section>
        )}
        <div className="wizard-actions">
          <button
            type="button"
            className="secondary"
            disabled={locked}
            onClick={step ? () => onStep(step - 1) : onCancel}
          >
            <ArrowLeft size={16} />
            {step ? "Back" : "Cancel"}
          </button>
          {step > 0 && (
            <button
              type="button"
              className="secondary"
              disabled={locked}
              onClick={onCancel}
            >
              Cancel
            </button>
          )}
          <button
            type="submit"
            className="primary"
            disabled={locked || !reviewed}
          >
            {busy
              ? "Saving…"
              : step === 2
                ? terminalDisable
                  ? "Disable connection"
                  : "Save connection"
                : "Continue"}
            <ArrowRight size={16} />
          </button>
        </div>
      </form>
    </Wizard>
  );
}
export function AnalyticsExplorer({
  catalog,
  table,
  object,
  objectId,
  onObjectId,
  busy,
  disabled,
  onCatalog,
  onTable,
  onObject,
}: {
  catalog: AnalyticsCatalog | null;
  table: AnalyticsObservation<AnalyticsTable> | null;
  object: AnalyticsObservation<AnalyticsObject> | null;
  objectId: string;
  onObjectId: (value: string) => void;
  busy: boolean;
  disabled: boolean;
  onCatalog: () => void;
  onTable: (table: AnalyticsTable) => void;
  onObject: () => void;
}) {
  return (
    <section
      className="analytics-explorer"
      aria-label="Read-only analytics explorer"
    >
      <header>
        <div>
          <h2>Project catalog</h2>
          <p>
            Reported table and snapshot identities. No queries are executed.
          </p>
        </div>
        <button
          className="primary"
          disabled={busy || disabled}
          onClick={onCatalog}
        >
          <RefreshCw size={16} />
          {busy ? "Working…" : "Read catalog"}
        </button>
      </header>
      {disabled && (
        <p className="analytics-reader">
          Save an enabled connection before reading this catalog.
        </p>
      )}
      {!catalog ? (
        <div className="analytics-empty">
          <Database size={30} />
          <h3>No catalog read yet</h3>
          <p>
            Choose Read catalog to make one bounded read of the saved scope.
            This page does not poll or retry automatically.
          </p>
        </div>
      ) : (
        <>
          <ObservationDetails observation={catalog} />
          <div className="analytics-grid">
            <nav
              className="analytics-tables"
              aria-label="Tables in the saved dataset"
            >
              <h3>
                {catalog.data.items.length}{" "}
                {catalog.data.items.length === 1 ? "table" : "tables"} reported
              </h3>
              {!catalog.data.items.length && (
                <p>
                  No tables returned. Confirm the dataset and token scope with
                  your data administrator; this explorer does not create missing
                  data.
                </p>
              )}
              {catalog.data.items.map((item) => (
                <button
                  className={
                    table?.data.id === item.id
                      ? "analytics-table selected"
                      : "analytics-table"
                  }
                  key={item.id}
                  disabled={busy}
                  onClick={() => onTable(item)}
                  aria-pressed={table?.data.id === item.id}
                >
                  <Database size={18} />
                  <span>
                    <strong>{item.name}</strong>
                    <small>{item.qualified_name}</small>
                    <small>
                      {item.n_columns === null
                        ? "Field count not reported"
                        : `${item.n_columns} fields reported`}
                    </small>
                  </span>
                  <ArrowRight size={16} />
                </button>
              ))}
            </nav>
            <div className="analytics-inspector">
              {!table ? (
                <div className="analytics-empty">
                  <h3>Choose a table</h3>
                  <p>
                    Read one table to inspect its current reported snapshot and
                    look up an exact schema object.
                  </p>
                </div>
              ) : (
                <>
                  <h3>{table.data.qualified_name}</h3>
                  <p>{table.data.description || "No description reported."}</p>
                  <dl className="analytics-scope">
                    <div>
                      <dt>Table UUID</dt>
                      <dd>{table.data.id}</dd>
                    </div>
                    <div>
                      <dt>Reported snapshot UUID</dt>
                      <dd>
                        {table.data.current_snapshot_id ?? "Not recorded"}
                      </dd>
                    </div>
                  </dl>
                  <p className="analytics-reader">
                    A snapshot UUID identifies a reported snapshot. It does not
                    prove the underlying data content or an atomic current cut.
                  </p>
                  <ObservationDetails observation={table} />
                  <form
                    className="analytics-object-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      onObject();
                    }}
                  >
                    <div className="field">
                      <label htmlFor="analytics-object">
                        Known schema-object UUID
                      </label>
                      <input
                        id="analytics-object"
                        value={objectId}
                        maxLength={36}
                        onChange={(e) => onObjectId(e.target.value)}
                        disabled={busy || !table.data.current_snapshot_id}
                        placeholder="Ask your data administrator for this UUID"
                        aria-describedby="analytics-object-help"
                      />
                      <small id="analytics-object-help">
                        Full field discovery is unavailable. A reported current
                        table snapshot is required for this exact lookup.
                      </small>
                    </div>
                    <button
                      className="secondary"
                      type="submit"
                      disabled={
                        busy || !objectId || !table.data.current_snapshot_id
                      }
                    >
                      Read schema object
                    </button>
                  </form>
                  {object && (
                    <section
                      className="analytics-object"
                      aria-label="Reported schema object"
                    >
                      <h4>{object.data.qualified_name}</h4>
                      <p>
                        {object.data.description || "No description reported."}
                      </p>
                      <dl className="analytics-scope">
                        <div>
                          <dt>Object UUID</dt>
                          <dd>{object.data.id}</dd>
                        </div>
                        <div>
                          <dt>Type</dt>
                          <dd>{object.data.data_type ?? "Not recorded"}</dd>
                        </div>
                        <div>
                          <dt>Nullable</dt>
                          <dd>
                            {object.data.is_nullable === null
                              ? "Not recorded"
                              : object.data.is_nullable
                                ? "Yes"
                                : "No"}
                          </dd>
                        </div>
                      </dl>
                      <ObservationDetails observation={object} />
                    </section>
                  )}
                </>
              )}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
function ObservationDetails({
  observation,
}: {
  observation: AnalyticsObservation<unknown>;
}) {
  return (
    <details className="analytics-observation">
      <summary>Observed scope and response digest</summary>
      <ScopeDetails scope={observation} />
      <dl className="analytics-scope">
        <div>
          <dt>Configuration revision</dt>
          <dd>{observation.configuration_revision}</dd>
        </div>
        <div>
          <dt>Observed at</dt>
          <dd>{observation.observed_at}</dd>
        </div>
        <div>
          <dt>Response digest</dt>
          <dd>{observation.response_digest}</dd>
        </div>
      </dl>
    </details>
  );
}
