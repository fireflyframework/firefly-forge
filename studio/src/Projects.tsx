/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { repositoryLabels } from "./RepositoryLabels";
import { useEffect, useState, type FormEvent } from "react";
import {
  ArrowRight,
  Box,
  GitBranch,
  Layers3,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import {
  request,
  parseCommands,
  type Connection,
  type Repository,
  type ForgeRun,
  type ProjectSnapshot,
  type ProjectComponent,
  type ProjectDefinitionData,
  type ComponentRunContext,
} from "./api";
import { SourceAnalysis } from "./SourceAnalysis";
import { Wizard } from "./Wizard";
import { safeExternalLink } from "./FeatureLinks";
import "./StudioFeatures.css";
import { CampaignReports } from "./CampaignReports";
import type { IdentitySession } from "./Identity";
type ComponentOverview = ProjectComponent & {
  latest_status: string;
  repository_run_count: number;
  runs: {
    id: string;
    status: string;
    branch?: string;
    pr_url?: string;
    created_at: string;
  }[];
};
type Overview = ProjectSnapshot & {
  components: ComponentOverview[];
  counts: Record<string, number>;
};
const fail = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The product project request failed.";
const lines = (value: string) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
const label = (value: string) => value.replaceAll("_", " ");
export function Projects({
  connection,
  connected,
  repositories,
  onStart,
  onOpen,
  onProduct,
  session,
}: {
  session: IdentitySession | null;
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
  onStart: (context: ComponentRunContext) => void;
  onOpen: (run: ForgeRun) => void;
  onProduct: (repositoryId: string, project: ProjectSnapshot) => void;
}) {
  const [items, setItems] = useState<ProjectSnapshot[]>([]),
    [selected, setSelected] = useState(""),
    [overview, setOverview] = useState<Overview | null>(null),
    [editing, setEditing] = useState<ProjectSnapshot | "new" | null>(null),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0),
    [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    setLoading(true);
    request<ProjectSnapshot[]>(connection, "/projects", {
      signal: controller.signal,
    })
      .then((records) => {
        if (!controller.signal.aborted) {
          setItems(records);
          setError("");
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(fail(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, connected, revision]);
  const id = items.some((item) => item.id === selected)
    ? selected
    : (items[0]?.id ?? "");
  useEffect(() => {
    if (!connected || !id || editing) return;
    const controller = new AbortController();
    const refresh = () =>
      request<Overview>(connection, `/projects/${id}`, {
        signal: controller.signal,
      })
        .then((value) => {
          if (!controller.signal.aborted) {
            setOverview(value);
            setError("");
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(fail(cause));
        });
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [connection, connected, id, editing, revision]);
  async function open(id: string) {
    try {
      onOpen(await request<ForgeRun>(connection, `/runs/${id}`));
    } catch (cause) {
      setError(fail(cause));
    }
  }
  const current = overview?.id === id ? overview : null;
  return (
    <section className="projects-page">
      <div className="page-heading">
        <div>
          <h1>Product projects</h1>
          <p>
            Group source components into one logical product with explicit
            ownership, dependencies and a shared definition.
          </p>
        </div>
        <button
          className="primary"
          disabled={!connected || !repositories.length}
          onClick={() => setEditing("new")}
        >
          <Plus size={18} />
          Create product project
        </button>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {!connected ? (
        <div className="empty">
          <h3>Connect your factory first</h3>
          <p>
            Project configuration and component run evidence are stored on the
            server.
          </p>
        </div>
      ) : !repositories.length ? (
        <div className="empty">
          <h3>Register a source repository first</h3>
          <p>Each component belongs to one registered repository.</p>
        </div>
      ) : editing ? (
        <ProjectEditor
          key={editing === "new" ? "new" : `${editing.id}/${editing.revision}`}
          connection={connection}
          repositories={repositories}
          initial={editing === "new" ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={(project) => {
            setItems((values) => [
              project,
              ...values.filter((item) => item.id !== project.id),
            ]);
            setSelected(project.id);
            setEditing(null);
            setRevision((value) => value + 1);
          }}
        />
      ) : (
        <>
          {items.length > 0 && (
            <div className="feature-toolbar">
              <div className="field">
                <label htmlFor="logical-project">Product project</label>
                <select
                  id="logical-project"
                  value={id}
                  onChange={(event) => {
                    setSelected(event.target.value);
                    setOverview(null);
                  }}
                >
                  {items.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.definition.name}
                    </option>
                  ))}
                </select>
              </div>
              <button
                className="secondary"
                onClick={() => setRevision((value) => value + 1)}
              >
                <RefreshCw size={18} />
                Refresh evidence
              </button>
            </div>
          )}
          {loading && !items.length ? (
            <p role="status">Loading product projects…</p>
          ) : !items.length ? (
            <div className="empty">
              <h3>No logical products configured</h3>
              <p>
                Create a monorepo or multiple-repository product. Existing
                standalone repository runs remain available in the factory.
              </p>
            </div>
          ) : current ? (
            <>
              <header className="project-overview-header">
                <div>
                  <h2>{current.definition.name}</h2>
                  <p>{current.definition.description}</p>
                  <p className="feature-meta">
                    {label(current.definition.topology)} · revision
                    {current.revision} · owners:
                    {current.definition.owners.join(", ")}
                  </p>
                </div>
                <div className="project-overview-actions">
                  <button
                    className="secondary"
                    onClick={() => setEditing(current)}
                  >
                    Edit component contracts
                  </button>
                  <button
                    className="secondary"
                    onClick={() =>
                      onProduct(
                        current.definition.product_repository_id ||
                          current.definition.components[0].repository_id,
                        current,
                      )
                    }
                  >
                    <Layers3 size={18} />
                    Product and design system
                  </button>
                </div>
              </header>
              <p className="integration-status">
                One shared product definition, brand and grid come from{" "}
                {repositories.find(
                  (item) =>
                    item.id ===
                    (current.definition.product_repository_id ||
                      current.definition.components[0].repository_id),
                )?.name || "the configured member repository"}
                . Each component keeps its own source root, stack and executable
                checks.
              </p>
              <ProjectMap
                components={current.definition.components}
                repositories={repositories}
                activities={Object.fromEntries(
                  current.components.map((component) => [
                    component.id,
                    component.latest_status,
                  ]),
                )}
              />
              {session && (
                <CampaignReports
                  key={JSON.stringify([
                    connection.base,
                    connection.token,
                    session.user.id,
                    session.global_admin,
                    session.memberships,
                    current.id,
                    current.digest,
                  ])}
                  connection={connection}
                  project={current}
                  session={session}
                />
              )}
              <SourceAnalysis
                key={current.id}
                connection={connection}
                project={current}
              />
              <div className="project-components">
                {current.components.map((component) => (
                  <article className="feature-record" key={component.id}>
                    <header>
                      <div>
                        <h3>
                          <Box size={20} />
                          {component.name}
                        </h3>
                        <p>{component.responsibility}</p>
                      </div>
                      <span className="feature-status">
                        {label(component.latest_status)}
                      </span>
                    </header>
                    <dl className="project-component-facts">
                      <div>
                        <dt>Repository / root</dt>
                        <dd>
                          {repositories.find(
                            (item) => item.id === component.repository_id,
                          )?.name || component.repository_id}
                          <code>
                            {component.path === "."
                              ? "Repository root"
                              : component.path}
                          </code>
                        </dd>
                      </div>
                      <div>
                        <dt>Component stack</dt>
                        <dd>
                          {component.backend} / {component.frontend}
                        </dd>
                      </div>
                      <div>
                        <dt>Dependencies</dt>
                        <dd>
                          {component.depends_on
                            .map(
                              (dependency) =>
                                current.definition.components.find(
                                  (item) => item.id === dependency,
                                )?.name || dependency,
                            )
                            .join(", ") || "None configured"}
                        </dd>
                      </div>
                    </dl>
                    <button
                      className="primary"
                      onClick={() => onStart({ project: current, component })}
                    >
                      Start component run
                      <ArrowRight size={18} />
                    </button>
                    <section
                      className="project-run-history"
                      aria-label={`${component.name} recorded runs`}
                    >
                      {component.runs.length ? (
                        component.runs.slice(0, 10).map((run) => (
                          <article key={run.id}>
                            <button
                              className="text-button"
                              onClick={() => void open(run.id)}
                            >
                              {run.id.slice(0, 8)} · {label(run.status)}
                            </button>
                            {run.branch && (
                              <span>
                                <GitBranch size={15} />
                                {run.branch}
                              </span>
                            )}
                            {safeExternalLink(run.pr_url) && (
                              <a
                                href={safeExternalLink(run.pr_url)!}
                                target="_blank"
                                rel="noreferrer noopener"
                              >
                                Pull request
                              </a>
                            )}
                          </article>
                        ))
                      ) : (
                        <p>
                          No component-scoped run has been recorded.{" "}
                          {component.repository_run_count > 0 &&
                            `${component.repository_run_count} standalone repository runs are not attributed to this component.`}
                        </p>
                      )}
                    </section>
                  </article>
                ))}
              </div>
            </>
          ) : (
            <p role="status">Loading component evidence…</p>
          )}
        </>
      )}
    </section>
  );
}
function ProjectMap({
  components,
  repositories,
  activities,
}: {
  components: ProjectComponent[];
  repositories: Repository[];
  activities?: Record<string, string>;
}) {
  const component = components[0];
  if (components.length === 1)
    return (
      <section
        className="project-topology-map project-topology-single"
        aria-label="Product component topology"
      >
        <h3>
          <GitBranch size={24} />
          Source topology
        </h3>
        <dl className="project-source-row">
          <div>
            <dt>Component</dt>
            <dd>{component.name}</dd>
          </div>
          <div>
            <dt>Repository</dt>
            <dd>
              {repositories.find((item) => item.id === component.repository_id)
                ?.name || component.repository_id}
            </dd>
          </div>
          <div>
            <dt>Source root</dt>
            <dd>
              {component.path === "." ? "Repository root" : component.path}
            </dd>
          </div>
          <div>
            <dt>Stack</dt>
            <dd>
              {component.backend === "none" ? "No backend" : component.backend}{" "}
              /{" "}
              {component.frontend === "none" ? "API only" : component.frontend}
            </dd>
          </div>
          <div>
            <dt>Actual activity</dt>
            <dd>
              {activities
                ? label(activities[component.id])
                : "Configuration only"}
            </dd>
          </div>
        </dl>
      </section>
    );
  return (
    <section
      className="project-topology-map"
      aria-label="Product component topology"
    >
      <h3>
        <GitBranch size={24} />
        Source topology
      </h3>
      <div>
        {components.map((component) => (
          <article key={component.id}>
            <Box size={20} />
            <strong>{component.name}</strong>
            <span>
              {repositories.find((item) => item.id === component.repository_id)
                ?.name || component.repository_id}
            </span>
            <code>
              {component.path === "." ? "Repository root" : component.path}
            </code>
            {component.depends_on.length > 0 && (
              <small>
                After{" "}
                {component.depends_on
                  .map(
                    (id) =>
                      components.find((item) => item.id === id)?.name || id,
                  )
                  .join(", ")}
              </small>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
function ProjectEditor({
  connection,
  repositories,
  initial,
  onSaved,
  onCancel,
}: {
  connection: Connection;
  repositories: Repository[];
  initial: ProjectSnapshot | null;
  onSaved: (project: ProjectSnapshot) => void;
  onCancel: () => void;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [step, setStep] = useState(0),
    [name, setName] = useState(initial?.definition.name ?? ""),
    [description, setDescription] = useState(
      initial?.definition.description ?? "",
    ),
    [owners, setOwners] = useState(initial?.definition.owners.join("\n") ?? ""),
    [topology, setTopology] = useState<ProjectDefinitionData["topology"]>(
      initial?.definition.topology ?? "monorepo",
    ),
    [components, setComponents] = useState<ProjectComponent[]>(
      initial?.definition.components ?? [
        {
          id: "api",
          name: "Product API",
          repository_id: repositories[0].id,
          path: ".",
          kind: "api",
          backend: "pyfly",
          frontend: "none",
          verification: [],
          depends_on: [],
          responsibility: "",
        },
      ],
    ),
    [selected, setSelected] = useState(
      initial?.definition.components[0].id ?? "api",
    ),
    [productSource, setProductSource] = useState(
      initial?.definition.product_repository_id ??
        initial?.definition.components[0].repository_id ??
        repositories[0].id,
    ),
    [checks, setChecks] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const component =
    components.find((item) => item.id === selected) || components[0];
  function update(change: Partial<ProjectComponent>) {
    setComponents((values) =>
      values.map((item) =>
        item.id === component.id ? { ...item, ...change } : item,
      ),
    );
  }
  function add() {
    let number = components.length + 1;
    while (components.some((item) => item.id === `component-${number}`))
      number++;
    const id = `component-${number}`;
    setComponents((items) => [
      ...items,
      {
        id,
        name: "",
        repository_id:
          topology === "monorepo"
            ? items[0].repository_id
            : repositories.find(
                (repo) => !items.some((item) => item.repository_id === repo.id),
              )?.id || repositories[0].id,
        path: `services/${id}`,
        kind: "api",
        backend: "pyfly",
        frontend: "none",
        verification: [],
        depends_on: [],
        responsibility: "",
      },
    ]);
    setSelected(id);
  }
  function definition(): ProjectDefinitionData {
    return {
      name: name.trim(),
      description: description.trim(),
      owners: lines(owners),
      topology,
      product_repository_id: components.some(
        (item) => item.repository_id === productSource,
      )
        ? productSource
        : components[0].repository_id,
      components: components.map((item) => ({
        ...item,
        verification:
          checks[item.id] === undefined
            ? item.verification
            : parseCommands(checks[item.id]),
      })),
    };
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const body = definition();
      if (
        step >= 1 &&
        body.components.some(
          (item) => !item.name.trim() || !item.responsibility.trim(),
        )
      )
        throw new Error(
          "Name every component and describe its responsibility.",
        );
      if (
        step >= 1 &&
        topology === "monorepo" &&
        new Set(body.components.map((item) => item.repository_id)).size !== 1
      )
        throw new Error(
          "A monorepo's components must use the same source repository.",
        );
      if (
        step >= 1 &&
        topology === "multi_repo" &&
        new Set(body.components.map((item) => item.repository_id)).size < 2
      )
        throw new Error(
          "A multiple-repository product needs at least two source repositories.",
        );
      if (step < 3) {
        setStep(step + 1);
        return;
      }
      setBusy(true);
      const saved = await request<ProjectSnapshot>(
        connection,
        initial ? `/projects/${initial.id}` : "/projects",
        { method: initial ? "PUT" : "POST", body: JSON.stringify(body) },
      );
      onSaved(saved);
    } catch (cause) {
      setError(fail(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Wizard
      locked={busy}
      steps={["Product", "Components", "Dependencies", "Review"]}
      active={step}
      onStep={setStep}
      title={
        [
          "Define the logical product",
          "Configure source components",
          "Connect component dependencies",
          "Review the product topology",
        ][step]
      }
      description={
        [
          "Choose ownership and whether components share a repository or live in multiple repositories.",
          "Each component has one source root, one framework contract and explicit executable checks.",
          "Dependencies reference configured components. Cycles and overlapping source roots are rejected by the server.",
          "Saving creates an immutable configuration revision. New component runs freeze that revision and the shared product design system.",
        ][step]
      }
    >
      <form onSubmit={(event) => void submit(event)}>
        {step === 0 && (
          <>
            <div className="field">
              <label htmlFor="project-name">Product project name</label>
              <input
                id="project-name"
                required
                maxLength={120}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="project-description">Product scope</label>
              <textarea
                id="project-description"
                required
                maxLength={4000}
                rows={3}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>
            <div className="two-columns">
              <div className="field">
                <label htmlFor="project-owners">Project owners</label>
                <textarea
                  id="project-owners"
                  required
                  rows={3}
                  value={owners}
                  onChange={(event) => setOwners(event.target.value)}
                />
                <small>One accountable person or team per line.</small>
              </div>
              <div className="field">
                <label htmlFor="project-topology">Repository topology</label>
                <select
                  id="project-topology"
                  value={topology}
                  onChange={(event) =>
                    setTopology(
                      event.target.value as ProjectDefinitionData["topology"],
                    )
                  }
                >
                  <option value="monorepo">Monorepo · shared repository</option>
                  <option value="multi_repo" disabled={repositories.length < 2}>
                    Multiple repositories
                  </option>
                </select>
                {repositories.length < 2 && (
                  <small>
                    Register a second repository to select multiple
                    repositories.
                  </small>
                )}
              </div>
            </div>
          </>
        )}
        {step === 1 && (
          <div className="component-editor-layout">
            <nav aria-label="Configured components">
              <ol>
                {components.map((item, index) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      aria-pressed={component.id === item.id}
                      onClick={() => setSelected(item.id)}
                    >
                      {index + 1}. {item.name || item.id}
                    </button>
                  </li>
                ))}
              </ol>
              <button
                type="button"
                className="secondary"
                disabled={components.length >= 50}
                onClick={add}
              >
                <Plus size={16} />
                Add component
              </button>
            </nav>
            <section aria-label="Selected component contract">
              <div className="two-columns">
                <div className="field">
                  <label htmlFor="component-name">Component name</label>
                  <input
                    id="component-name"
                    required
                    value={component.name}
                    maxLength={120}
                    onChange={(event) => update({ name: event.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="component-kind">Component kind</label>
                  <select
                    id="component-kind"
                    value={component.kind}
                    onChange={(event) => {
                      const kind = event.target
                        .value as ProjectComponent["kind"];
                      update({
                        kind,
                        backend:
                          kind === "web"
                            ? "none"
                            : component.backend === "none"
                              ? "pyfly"
                              : component.backend,
                        frontend:
                          kind === "web" || kind === "fullstack"
                            ? component.frontend === "none"
                              ? "react"
                              : component.frontend
                            : "none",
                      });
                    }}
                  >
                    <option value="api">API service</option>
                    <option value="web">Standalone web / SPA</option>
                    <option value="worker">Worker</option>
                    <option value="library">Library</option>
                    <option value="fullstack">Full product</option>
                  </select>
                </div>
              </div>
              <div className="two-columns">
                <div className="field">
                  <label htmlFor="component-repository">
                    Component repository
                  </label>
                  <select
                    id="component-repository"
                    value={component.repository_id}
                    onChange={(event) => {
                      const id = event.target.value;
                      if (topology === "monorepo")
                        setComponents((items) =>
                          items.map((item) => ({ ...item, repository_id: id })),
                        );
                      else update({ repository_id: id });
                    }}
                  >
                    {repositories.map((item) => (
                      <option key={item.id} value={item.id}>
                        {projectLabels.get(item.id)}
                      </option>
                    ))}
                  </select>
                  <small>
                    {topology === "monorepo"
                      ? "Changing this updates the repository for all components."
                      : "Use a registered repository for this component."}
                  </small>
                </div>
                <div className="field">
                  <label htmlFor="component-path">Source root</label>
                  <input
                    id="component-path"
                    required
                    value={component.path}
                    onChange={(event) => update({ path: event.target.value })}
                  />
                  <small>
                    Relative path; use . for a whole repository. Roots cannot
                    overlap.
                  </small>
                </div>
              </div>
              <div className="two-columns">
                <div className="field">
                  <label htmlFor="component-backend">Component backend</label>
                  <select
                    id="component-backend"
                    value={component.backend}
                    disabled={component.kind === "web"}
                    onChange={(event) =>
                      update({ backend: event.target.value })
                    }
                  >
                    {["pyfly", "larafly", "java", "rust", "dotnet", "none"]
                      .filter(
                        (value) => value !== "none" || component.kind === "web",
                      )
                      .map((value) => (
                        <option key={value}>{value}</option>
                      ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="component-frontend">Component frontend</label>
                  <select
                    id="component-frontend"
                    value={component.frontend}
                    disabled={["api", "worker", "library"].includes(
                      component.kind,
                    )}
                    onChange={(event) =>
                      update({ frontend: event.target.value })
                    }
                  >
                    {["none", "react", "angular", "nextjs", "vue"].map(
                      (value) => (
                        <option key={value}>{value}</option>
                      ),
                    )}
                  </select>
                </div>
              </div>
              <div className="field">
                <label htmlFor="component-responsibility">
                  Component responsibility
                </label>
                <textarea
                  id="component-responsibility"
                  required
                  maxLength={2000}
                  rows={3}
                  value={component.responsibility}
                  onChange={(event) =>
                    update({ responsibility: event.target.value })
                  }
                />
              </div>
              <div className="field">
                <label htmlFor="component-checks">
                  Component verification commands
                </label>
                <textarea
                  id="component-checks"
                  rows={3}
                  value={
                    checks[component.id] ??
                    JSON.stringify(
                      component.verification.map((item) => item.argv),
                    )
                  }
                  onChange={(event) =>
                    setChecks((values) => ({
                      ...values,
                      [component.id]: event.target.value,
                    }))
                  }
                  placeholder='[["make","check"]]'
                />
                <small>
                  JSON argument arrays run from the component source root.
                  Existing timeouts are retained until these commands are
                  edited.
                </small>
              </div>
              <p className="feature-meta">
                Stable component ID: {component.id}
              </p>
              {components.length > 1 && (
                <button
                  type="button"
                  className="quiet"
                  onClick={() => {
                    const remaining = components
                      .filter((item) => item.id !== component.id)
                      .map((item) => ({
                        ...item,
                        depends_on: item.depends_on.filter(
                          (id) => id !== component.id,
                        ),
                      }));
                    setComponents(remaining);
                    setSelected(remaining[0].id);
                  }}
                >
                  <Trash2 size={16} />
                  Remove component
                </button>
              )}
            </section>
          </div>
        )}
        {step === 2 && (
          <>
            <div className="field">
              <label htmlFor="project-product-source">
                Product definition and design system
              </label>
              <select
                id="project-product-source"
                value={
                  components.some(
                    (item) => item.repository_id === productSource,
                  )
                    ? productSource
                    : components[0].repository_id
                }
                onChange={(event) => setProductSource(event.target.value)}
              >
                {repositories
                  .filter((repo) =>
                    components.some((item) => item.repository_id === repo.id),
                  )
                  .map((repo) => (
                    <option key={repo.id} value={repo.id}>
                      {projectLabels.get(repo.id)}
                    </option>
                  ))}
              </select>
              <small>
                This member repository owns the shared customer definition,
                product brand, typography and grid.
              </small>
            </div>
            {components.map((item) => (
              <fieldset key={item.id}>
                <legend>{item.name} depends on</legend>
                {components
                  .filter((other) => other.id !== item.id)
                  .map((other) => (
                    <label className="check-choice" key={other.id}>
                      <input
                        type="checkbox"
                        checked={item.depends_on.includes(other.id)}
                        onChange={(event) =>
                          setComponents((items) =>
                            items.map((entry) =>
                              entry.id === item.id
                                ? {
                                    ...entry,
                                    depends_on: event.target.checked
                                      ? [...entry.depends_on, other.id]
                                      : entry.depends_on.filter(
                                          (id) => id !== other.id,
                                        ),
                                  }
                                : entry,
                            ),
                          )
                        }
                      />
                      {other.name}
                    </label>
                  ))}
                {components.length === 1 && (
                  <p>No sibling components are configured.</p>
                )}
              </fieldset>
            ))}
            <ProjectMap components={components} repositories={repositories} />
          </>
        )}
        {step === 3 && (
          <>
            <dl className="wizard-summary">
              <div>
                <dt>Product</dt>
                <dd>
                  {name} · {label(topology)}
                </dd>
              </div>
              <div>
                <dt>Owners</dt>
                <dd>{lines(owners).join(", ")}</dd>
              </div>
              <div>
                <dt>Shared definition and design source</dt>
                <dd>
                  {
                    repositories.find(
                      (item) => item.id === definition().product_repository_id,
                    )?.name
                  }
                </dd>
              </div>
            </dl>
            <ProjectMap components={components} repositories={repositories} />
            <p>
              Saving configuration does not run components or complete
              dependencies. Component activity appears only after actual
              recorded runs.
            </p>
          </>
        )}
        {error && (
          <div className="alert" role="alert">
            {error}
          </div>
        )}
        <div className="wizard-actions">
          <button
            type="button"
            className="quiet"
            disabled={busy}
            onClick={onCancel}
          >
            Cancel
          </button>
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
          <button className="primary" disabled={busy}>
            {busy
              ? "Saving…"
              : step === 3
                ? "Save product project"
                : "Continue"}
          </button>
        </div>
      </form>
    </Wizard>
  );
}
