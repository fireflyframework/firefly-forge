/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { repositoryLabels } from "./RepositoryLabels";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { post, request, type Connection, type Repository } from "./api";
import { KnowledgeUploads } from "./KnowledgeUploads";
import { KoinonKnowledgeSettings } from "./KoinonKnowledgeSettings";
import type { KoinonSettings } from "./KoinonKnowledgeData";
import { LearningRecords } from "./LearningRecords";
import type { IdentitySession } from "./Identity";
import { safeExternalLink } from "./FeatureLinks";
import { RichMarkdown } from "./RichMarkdown";
import { KnowledgeFrameworkPanel } from "./KnowledgeFrameworkPanel";
import {
  FrameworkCorpusSelector,
  FrameworkCorpusSources,
} from "./FrameworkCorpusSources";
import {
  corpusSelectionKey,
  frameworkSearch,
  type FrameworkCorpus,
} from "./FrameworkKnowledgeData";
import "./StudioFeatures.css";

type LegacySettings = {
  provider?: undefined;
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
type Settings = LegacySettings | KoinonSettings;
type Hit = {
  title?: string;
  source_version_id?: string | null;
  provenance?: string;
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
  const projectLabels = repositoryLabels(repositories);
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
      {settings?.provider === "koinon" && (
        <KoinonKnowledgeSettings
          settings={settings}
          connection={connection}
          repositories={repositories}
          onSaved={setSettings}
        />
      )}
      {settings && settings.provider !== "koinon" && (
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
                    {projectLabels.get(item.id)}
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
                maxLength={settings.provider === "koinon" ? 8000 : 4000}
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
                    hit.title ||
                    hit.source_filename ||
                    hit.source_id ||
                    "Retrieved source"}
                </strong>
                <p>{hit.content}</p>
                <small>
                  Workspace: {hit.workspace_id}
                  {settings.provider === "koinon" &&
                    ` · ${hit.source_version_id ? "Source version: " + hit.source_version_id : "Source version not recorded"}`}
                  {typeof hit.score === "number"
                    ? ` · Score: ${hit.score.toFixed(3)}`
                    : ""}
                </small>
                {safeExternalLink(hit.source_uri) && (
                  <a
                    className="text-button"
                    href={safeExternalLink(hit.source_uri)!}
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
              {settings.provider === "koinon"
                ? "No matching source was retrieved from the mapped Koinon scopes. Check their indexed source and current access."
                : "No matching source was retrieved. Index verified project files or add framework references in flycanon."}
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

type BrowseSource = {
  id: string;
  kind: string;
  status: string;
  filename: string;
  content_sha256: string;
  content_bytes: number;
  n_chunks: number;
  created_at: string;
  updated_at: string;
};
type BrowseItem = {
  id: string;
  status: string;
  title: string;
  summary: string;
  domain: string;
  current_version: number;
  created_at: string;
  updated_at: string;
};
type Explore = {
  workspace_id: string;
  scope: "project" | "framework";
  sources: BrowseSource[];
  knowledge: BrowseItem[];
  offset: number;
  limit: number;
  totals: { sources: number; knowledge: number };
  browsing_available: boolean;
  warnings: string[];
};
type CitedHit = Hit & {
  section_path?: string | string[];
  page?: number;
  knowledge_item_id?: string;
  knowledge_version?: number;
  framework?: string;
  framework_version?: string;
  release_tag?: string;
  commit?: string;
  source_sha256?: string;
  license_sha256?: string;
};

const explorerTabs = [
  "sources",
  "knowledge",
  "search",
  "frameworks",
  "history",
  "learning",
] as const;

export function KnowledgeExplorer({
  connection,
  connected,
  repositories,
  onConfigure,
  session,
}: {
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
  onConfigure: () => void;
  session?: IdentitySession | null;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [project, setProject] = useState("");
  const repository =
    repositories.find((item) => item.id === project) ?? repositories[0] ?? null;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [tab, setTab] = useState<(typeof explorerTabs)[number]>("sources");
  const [selectedFramework, setSelectedFramework] = useState<{
    owner: string;
    corpus: FrameworkCorpus;
  } | null>(null);
  const frameworkOwner = JSON.stringify([
    connection.base,
    session?.user.id ?? "",
  ]);
  const corpus =
    selectedFramework?.owner === frameworkOwner
      ? selectedFramework.corpus
      : null;
  const searchController = useRef<AbortController | null>(null);
  const abortSearch = useCallback(() => searchController.current?.abort(), []);
  const [scope, setScope] = useState<"project" | "framework">("project");
  const [offset, setOffset] = useState(0);
  const [browse, setBrowse] = useState<{ key: string; data: Explore } | null>(
    null,
  );
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<{
    repository: string;
    query: string;
    hits: CitedHit[];
    warnings: string[];
  } | null>(null);
  const [history, setHistory] = useState<
    { repository: string; query: string; at: string; count: number }[]
  >([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const id = repository?.id;
  const searchScope = corpus
    ? corpusSelectionKey(connection, session?.user.id ?? "", corpus)
    : JSON.stringify([frameworkOwner, id]);
  const key = `${id}:${scope}:${offset}`;
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    request<Settings>(connection, "/knowledge/configuration", {
      signal: controller.signal,
    })
      .then(setSettings)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      });
    return () => controller.abort();
  }, [connection, connected]);
  useEffect(() => {
    if (
      !connected ||
      !id ||
      tab === "search" ||
      tab === "history" ||
      tab === "learning" ||
      tab === "frameworks" ||
      (tab === "sources" && corpus !== null) ||
      !settings?.enabled
    )
      return;
    const controller = new AbortController();
    setLoading(true);
    request<Explore>(
      connection,
      `/knowledge/explore?repository_id=${encodeURIComponent(id)}&scope=${scope}&offset=${offset}`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (!controller.signal.aborted) {
          setBrowse({ key, data });
          setError("");
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorMessage(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [
    connection,
    connected,
    id,
    key,
    offset,
    scope,
    settings?.enabled,
    tab,
    corpus,
    refresh,
  ]);
  useEffect(() => {
    abortSearch();
    setBusy(false);
    return abortSearch;
  }, [searchScope, abortSearch]);
  async function search(value: string) {
    if ((!id && !corpus) || !value.trim()) return;
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    setBusy(true);
    setError("");
    setTab("search");
    setQuery(value);
    try {
      const response = corpus
        ? await frameworkSearch(
            connection,
            corpus,
            value,
            controller.signal,
          ).then((data) => ({
            hits: data.hits.map((hit) => ({
              ...hit,
              source_title: hit.path,
              workspace_id: corpus.workspace_id,
              chunk_id: hit.chunk_id ?? undefined,
              knowledge_version: hit.knowledge_version ?? undefined,
            })),
            warnings: data.warnings,
          }))
        : await request<{ hits: CitedHit[]; warnings: string[] }>(
            connection,
            "/knowledge/search",
            {
              method: "POST",
              body: JSON.stringify({ repository_id: id, query: value.trim() }),
              signal: controller.signal,
            },
          );
      if (controller.signal.aborted) return;
      setResult({ repository: searchScope, query: value.trim(), ...response });
      setHistory((items) =>
        [
          {
            repository: searchScope,
            query: value.trim(),
            at: new Date().toISOString(),
            count: response.hits.length,
          },
          ...items,
        ].slice(0, 30),
      );
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  const data = browse?.key === key ? browse.data : null;
  const currentResult = result?.repository === searchScope ? result : null;
  function selectCorpus(value: FrameworkCorpus, view: "sources" | "search") {
    setSelectedFramework({ owner: frameworkOwner, corpus: value });
    setOffset(0);
    setTab(view);
    setError("");
  }
  const total = data
    ? tab === "sources"
      ? data.totals.sources
      : data.totals.knowledge
    : 0;
  return (
    <section className="knowledge-explorer">
      <div className="page-heading">
        <div>
          <h1>Knowledge</h1>
          <p>
            Browse indexed project and framework records, then retrieve cited
            excerpts.
          </p>
        </div>
        <button
          className="secondary"
          disabled={!connected}
          onClick={onConfigure}
        >
          Configure knowledge
        </button>
      </div>
      {!connected ? (
        <div className="empty">
          <h3>Connect your factory first</h3>
          <p>
            Knowledge access follows the factory's configured tenant and project
            workspaces.
          </p>
        </div>
      ) : (
        <>
          <div className="feature-toolbar">
            {tab !== "frameworks" &&
              !(corpus && ["sources", "search", "history"].includes(tab)) && (
                <div className="field">
                  <label htmlFor="explorer-project">Knowledge project</label>
                  <select
                    id="explorer-project"
                    value={id ?? ""}
                    disabled={!repositories.length}
                    onChange={(event) => {
                      setProject(event.target.value);
                      setSelectedFramework(null);
                      setOffset(0);
                    }}
                  >
                    <option value="" disabled>
                      Choose a repository
                    </option>
                    {repositories.map((repo) => (
                      <option key={repo.id} value={repo.id}>
                        {projectLabels.get(repo.id)}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            {corpus && ["sources", "search", "history"].includes(tab) && (
              <button
                className="secondary"
                onClick={() => {
                  setSelectedFramework(null);
                  setTab("sources");
                }}
              >
                Return to project knowledge
              </button>
            )}
            {tab === "sources" &&
              id &&
              !corpus &&
              settings?.provider !== "koinon" && (
                <KnowledgeUploads
                  connection={connection}
                  repository={id}
                  enabled={Boolean(settings?.enabled && connected)}
                  onRefresh={() => setRefresh((value) => value + 1)}
                />
              )}
            {((tab === "sources" && !corpus) || tab === "knowledge") && (
              <div className="field">
                <label htmlFor="explorer-scope">Browse scope</label>
                <select
                  id="explorer-scope"
                  value={scope}
                  onChange={(event) => {
                    setScope(event.target.value as "project" | "framework");
                    setOffset(0);
                  }}
                >
                  <option value="project">Current project</option>
                  <option
                    value="framework"
                    disabled={!settings?.framework_workspace}
                  >
                    Shared frameworks
                  </option>
                </select>
              </div>
            )}
          </div>
          {error && tab !== "learning" && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          {!settings?.enabled && tab !== "learning" && tab !== "frameworks" && (
            <p className="integration-status">
              Enable and configure knowledge retrieval before browsing or
              searching.
            </p>
          )}
          <div
            className="feature-tabs"
            role="tablist"
            aria-label="Knowledge views"
          >
            {explorerTabs.map((value) => (
              <button
                key={value}
                role="tab"
                tabIndex={tab === value ? 0 : -1}
                onKeyDown={(event) => {
                  const tabs = explorerTabs;
                  if (
                    ["ArrowLeft", "ArrowRight", "Home", "End"].includes(
                      event.key,
                    )
                  ) {
                    event.preventDefault();
                    const index = tabs.indexOf(value);
                    const next =
                      event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? tabs.length - 1
                          : (index +
                              (event.key === "ArrowRight" ? 1 : -1) +
                              tabs.length) %
                            tabs.length;
                    setTab(tabs[next]);
                    setOffset(0);
                    document
                      .getElementById(`explorer-tab-${tabs[next]}`)
                      ?.focus();
                  }
                }}
                id={`explorer-tab-${value}`}
                aria-selected={tab === value}
                onClick={() => {
                  setTab(value);
                  setOffset(0);
                }}
              >
                {value === "knowledge"
                  ? "Knowledge items"
                  : value === "learning"
                    ? "Learning records"
                    : value === "frameworks"
                      ? "Framework releases"
                      : value[0].toUpperCase() + value.slice(1)}
              </button>
            ))}
          </div>
          {tab === "frameworks" && (
            <KnowledgeFrameworkPanel
              key={frameworkOwner}
              connection={connection}
              connected={connected}
              userId={session?.user.id ?? ""}
              onBrowse={(value) => selectCorpus(value, "sources")}
              onSearch={(value) => selectCorpus(value, "search")}
            />
          )}
          {corpus && ["sources", "search", "history"].includes(tab) && (
            <FrameworkCorpusSelector
              key={`${frameworkOwner}:${corpus.framework}`}
              connection={connection}
              selected={corpus}
              onSelect={(value) =>
                selectCorpus(value, tab === "search" ? "search" : "sources")
              }
            />
          )}
          {tab === "sources" && corpus && (
            <FrameworkCorpusSources
              key={corpusSelectionKey(
                connection,
                session?.user.id ?? "",
                corpus,
              )}
              connection={connection}
              corpus={corpus}
            />
          )}
          {tab === "learning" && (
            <LearningRecords
              key={`${connection.base}:${id}:${session?.user.id ?? ""}`}
              connection={connection}
              repository={id}
              repositoryLabel={id ? projectLabels.get(id) : undefined}
              session={session}
            />
          )}
          {((tab === "sources" && !corpus) || tab === "knowledge") && (
            <section className="feature-record">
              <header>
                <div>
                  <h2>
                    {tab === "sources"
                      ? "Indexed sources"
                      : "Versioned knowledge items"}
                  </h2>
                  <p className="subtle">
                    {data?.workspace_id ??
                      "The selected workspace will be returned by the server."}
                  </p>
                </div>
                <button
                  className="secondary"
                  disabled={!settings?.enabled || loading}
                  onClick={() => setRefresh((value) => value + 1)}
                >
                  Refresh
                </button>
              </header>
              {data?.warnings.map((warning) => (
                <p className="integration-status" key={warning}>
                  {warning}
                </p>
              ))}
              {loading || !data ? (
                <p className="integration-status">
                  {settings?.enabled
                    ? "Loading workspace records…"
                    : "Browsing is not configured."}
                </p>
              ) : !data.browsing_available ? (
                <p className="integration-status">
                  Workspace listing is unavailable for these credentials. Cited
                  search remains available when retrieval is configured.
                </p>
              ) : (
                <>
                  <div className="feature-list">
                    {tab === "sources"
                      ? data.sources.map((source) => (
                          <article key={source.id}>
                            <header>
                              <div>
                                <h3>{source.filename || source.id}</h3>
                                <p className="subtle">
                                  {source.kind} · {source.content_bytes} bytes ·{" "}
                                  {source.n_chunks} indexed chunks
                                </p>
                              </div>
                              <span className="feature-status">
                                {source.status}
                              </span>
                            </header>
                            <details>
                              <summary>Canonical record details</summary>
                              <dl className="wizard-summary">
                                <div>
                                  <dt>Source ID</dt>
                                  <dd>{source.id}</dd>
                                </div>
                                <div>
                                  <dt>Content digest</dt>
                                  <dd>{source.content_sha256}</dd>
                                </div>
                                <div>
                                  <dt>Last updated</dt>
                                  <dd>
                                    {new Date(
                                      source.updated_at,
                                    ).toLocaleString()}
                                  </dd>
                                </div>
                              </dl>
                            </details>
                            <button
                              className="text-button"
                              disabled={busy}
                              onClick={() =>
                                void search(source.filename || source.id)
                              }
                            >
                              Search related excerpts
                            </button>
                          </article>
                        ))
                      : data.knowledge.map((item) => (
                          <article key={item.id}>
                            <header>
                              <div>
                                <h3>{item.title}</h3>
                                <p className="subtle">
                                  {item.domain} · version {item.current_version}
                                </p>
                              </div>
                              <span className="feature-status">
                                {item.status}
                              </span>
                            </header>
                            <p>{item.summary}</p>
                            <details>
                              <summary>Canonical record details</summary>
                              <dl className="wizard-summary">
                                <div>
                                  <dt>Knowledge ID</dt>
                                  <dd>{item.id}</dd>
                                </div>
                                <div>
                                  <dt>Last updated</dt>
                                  <dd>
                                    {new Date(item.updated_at).toLocaleString()}
                                  </dd>
                                </div>
                              </dl>
                            </details>
                            <button
                              className="text-button"
                              onClick={() => void search(item.title)}
                            >
                              Search cited context
                            </button>
                          </article>
                        ))}
                  </div>
                  {!(tab === "sources"
                    ? data.sources.length
                    : data.knowledge.length) && (
                    <p className="integration-status">
                      No {tab === "sources" ? "sources" : "knowledge items"}{" "}
                      returned for this workspace.
                    </p>
                  )}
                  <div className="feature-pagination">
                    <button
                      className="secondary"
                      disabled={offset === 0}
                      onClick={() =>
                        setOffset((value) => Math.max(0, value - data.limit))
                      }
                    >
                      Previous
                    </button>
                    <span>
                      {total
                        ? `${offset + 1}–${Math.min(offset + data.limit, total)} of ${total}`
                        : "0 records"}
                    </span>
                    <button
                      className="secondary"
                      disabled={offset + data.limit >= total}
                      onClick={() => setOffset((value) => value + data.limit)}
                    >
                      Next
                    </button>
                  </div>
                </>
              )}
            </section>
          )}
          {tab === "search" && (
            <section
              className={`feature-record${corpus ? " framework-corpus-record" : ""}`}
            >
              <h2>Cited search</h2>
              <p className="subtle">
                {corpus
                  ? `Search is scoped to ${corpus.framework} ${corpus.framework_version}, commit ${corpus.commit}.`
                  : "Search uses the current project's workspace and the configured shared framework scope."}
              </p>
              {corpus && corpus.state !== "ready" && (
                <p className="integration-status" role="status">
                  This selected release is not ready for search. Inspect its
                  ingestion state or choose a retained ready corpus.
                </p>
              )}
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void search(query);
                }}
              >
                <div className="field">
                  <label htmlFor="explorer-query">Knowledge query</label>
                  <textarea
                    id="explorer-query"
                    required
                    maxLength={corpus ? 2000 : 4000}
                    rows={3}
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </div>
                <button
                  className="primary"
                  disabled={
                    busy ||
                    (!id && !corpus) ||
                    (corpus !== null && corpus.state !== "ready") ||
                    !settings?.enabled ||
                    !query.trim()
                  }
                >
                  {busy ? "Retrieving citations…" : "Search knowledge"}
                </button>
              </form>
              {currentResult && (
                <>
                  {currentResult.warnings.map((warning) => (
                    <p className="integration-status" key={warning}>
                      {warning}
                    </p>
                  ))}
                  <p className="integration-status">
                    {currentResult.hits.length} results returned for “
                    {currentResult.query}”.
                  </p>
                  <div className="feature-list">
                    {currentResult.hits.map((hit, index) => (
                      <article
                        key={`${hit.workspace_id}-${hit.chunk_id ?? index}`}
                      >
                        <h3>
                          {hit.source_title ||
                            hit.source_filename ||
                            hit.source_id ||
                            "Retrieved excerpt"}
                        </h3>
                        <div className="feature-meta">
                          <span>Workspace {hit.workspace_id}</span>
                          {hit.framework_version && (
                            <span>
                              {hit.framework} {hit.framework_version}
                            </span>
                          )}
                          {hit.knowledge_version && (
                            <span>
                              Knowledge version {hit.knowledge_version}
                            </span>
                          )}
                          {hit.page !== undefined && (
                            <span>Page {hit.page}</span>
                          )}
                        </div>
                        <div className="knowledge-excerpt">
                          {hit.framework ? (
                            <RichMarkdown value={hit.content} />
                          ) : (
                            hit.content
                              .split(/\n\s*\n/)
                              .map((paragraph, paragraphIndex) => (
                                <p key={paragraphIndex}>{paragraph}</p>
                              ))
                          )}
                        </div>
                        <details>
                          <summary>Citation details</summary>
                          <dl className="wizard-summary">
                            <div>
                              <dt>Source ID</dt>
                              <dd>{hit.source_id ?? "Not returned"}</dd>
                            </div>
                            <div>
                              <dt>Chunk ID</dt>
                              <dd>{hit.chunk_id ?? "Not returned"}</dd>
                            </div>
                            {hit.commit && (
                              <div>
                                <dt>Framework source commit</dt>
                                <dd>
                                  <code>{hit.commit}</code>
                                </dd>
                              </div>
                            )}
                            {hit.source_sha256 && (
                              <div>
                                <dt>Source SHA-256</dt>
                                <dd>
                                  <code>{hit.source_sha256}</code>
                                </dd>
                              </div>
                            )}
                            {hit.license_sha256 && (
                              <div>
                                <dt>Source license SHA-256</dt>
                                <dd>
                                  <code>{hit.license_sha256}</code>
                                </dd>
                              </div>
                            )}
                            {hit.section_path && (
                              <div>
                                <dt>Section</dt>
                                <dd>
                                  {Array.isArray(hit.section_path)
                                    ? hit.section_path.join(" / ")
                                    : hit.section_path}
                                </dd>
                              </div>
                            )}
                            {hit.source_uri && (
                              <div>
                                <dt>Canonical URI</dt>
                                <dd>
                                  {safeExternalLink(hit.source_uri) ? (
                                    <a
                                      href={safeExternalLink(hit.source_uri)!}
                                      target="_blank"
                                      rel="noreferrer"
                                    >
                                      {hit.source_uri}
                                    </a>
                                  ) : (
                                    hit.source_uri
                                  )}
                                </dd>
                              </div>
                            )}
                            {hit.score !== undefined && (
                              <div>
                                <dt>Retrieval score</dt>
                                <dd>{hit.score}</dd>
                              </div>
                            )}
                          </dl>
                        </details>
                      </article>
                    ))}
                  </div>
                </>
              )}
            </section>
          )}
          {tab === "history" && (
            <section className="feature-record">
              <h2>Searches in this tab</h2>
              <p className="subtle">
                History is kept while this view is open. Selecting an entry runs
                a new search.
              </p>
              <div className="feature-list">
                {history
                  .filter((item) => item.repository === searchScope)
                  .map((item, index) => (
                    <article key={`${item.at}-${index}`}>
                      <h3>{item.query}</h3>
                      <p className="subtle">
                        {new Date(item.at).toLocaleString()} · {item.count}{" "}
                        results returned
                      </p>
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() => void search(item.query)}
                      >
                        Run search again
                      </button>
                    </article>
                  ))}
              </div>
              {!history.some((item) => item.repository === searchScope) && (
                <p className="integration-status">
                  No searches recorded for this selected scope in this tab.
                </p>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}
