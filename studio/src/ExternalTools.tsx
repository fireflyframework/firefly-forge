/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Copy,
  Github,
  KeyRound,
  RefreshCw,
  ShieldCheck,
  Unplug,
  Waypoints,
} from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import { OutputView } from "./ExecutionPresentation";
import { searchError } from "./SearchSettings";
import { ExternalToolPrerequisites } from "./ExternalToolPrerequisites";
import {
  connectionPayload,
  githubRepository,
  grantPayload,
  readCatalog,
  readConnector,
  readGrants,
  readProjects,
  readRepositories,
  stateNames,
  toolFields,
  toolNames,
  type ToolConnector,
  type ToolGrant,
  type ToolProject,
  type ToolRepository,
  type ToolRole,
} from "./ExternalToolsSettings";
import "./SearchConfiguration.css";
import "./ExternalTools.css";

export function ExternalTools({
  connection,
  session,
  onAgents,
}: {
  connection: Connection;
  session: IdentitySession | null;
  onAgents: () => void;
}) {
  return (
    <>
      <div className="page-heading search-page-heading external-page-heading">
        <div>
          <h1>External tools</h1>
        </div>
        <Waypoints size={26} />
      </div>
      {session?.global_admin ? (
        <Administrator
          key={`${connection.base}:${session.user.id}`}
          connection={connection}
          onAgents={onAgents}
        />
      ) : (
        <section className="access-notice">
          <ShieldCheck size={22} />
          <div>
            <h2>Factory administrator access required</h2>
            <p>
              Project ownership does not grant access to platform keys, schema
              discovery or external tool permissions. Ask a factory
              administrator to configure this optional connection.
            </p>
          </div>
        </section>
      )}
    </>
  );
}
function Administrator({
  connection,
  onAgents,
}: {
  connection: Connection;
  onAgents: () => void;
}) {
  const [catalog, setCatalog] = useState<ToolConnector[]>([]),
    [projects, setProjects] = useState<ToolProject[]>([]),
    [repositories, setRepositories] = useState<ToolRepository[]>([]),
    [grants, setGrants] = useState<ToolGrant[]>([]);
  const [busy, setBusy] = useState(false),
    [operation, setOperation] = useState("read"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [uncertain, setUncertain] = useState(false),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0);
  const [prerequisiteId, setPrerequisiteId] = useState("");
  const [enabled, setEnabled] = useState(false),
    [key, setKey] = useState(""),
    [remove, setRemove] = useState(false),
    [selected, setSelected] = useState<string[]>([]),
    [reviewed, setReviewed] = useState(false),
    [projectId, setProjectId] = useState(""),
    [repositoryId, setRepositoryId] = useState(""),
    [roles, setRoles] = useState<ToolRole[]>(["implementer"]),
    [grantReadProject, setGrantReadProject] = useState(""),
    [revokeId, setRevokeId] = useState(""),
    [copied, setCopied] = useState(false);
  const active = useRef<AbortController | null>(null),
    view = useRef<HTMLDivElement>(null),
    alert = useRef<HTMLDivElement>(null),
    projectRef = useRef(""),
    discoveryRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    const main = view.current?.closest("main");
    if (main) main.scrollTop = 0;
    if (editing || prerequisiteId)
      view.current
        ?.querySelector<HTMLElement>(".wizard-heading h2")
        ?.focus({ preventScroll: true });
  }, [editing, prerequisiteId, step]);
  const current = catalog.find((item) => item.id === "github"),
    prerequisite = catalog.find((item) => item.id === prerequisiteId),
    project = projects.find((item) => item.id === projectId),
    repository = repositories.find((item) => item.id === repositoryId);
  const availableRepositories = repositories.filter(
    (item) =>
      project?.definition.components.some((c) => c.repository_id === item.id) &&
      githubRepository(item),
  );
  const load = useCallback(
    async (controller: AbortController) => {
      const [rawCatalog, rawProjects, rawRepositories] = await Promise.all([
        request<unknown>(connection, "/tools/mcp", {
          signal: controller.signal,
        }),
        request<unknown>(connection, "/projects", {
          signal: controller.signal,
        }),
        request<unknown>(connection, "/repositories", {
          signal: controller.signal,
        }),
      ]);
      const entries = readCatalog(rawCatalog),
        products = readProjects(rawProjects),
        repos = readRepositories(rawRepositories);
      let permissions: ToolGrant[] = [];
      if (
        projectRef.current &&
        products.some((item) => item.id === projectRef.current)
      )
        permissions = readGrants(
          await request<unknown>(
            connection,
            `/tools/mcp/projects/${projectRef.current}/grants`,
            { signal: controller.signal },
          ),
          projectRef.current,
        );
      if (controller.signal.aborted) return;
      const github = entries.find((item) => item.id === "github")!;
      if (discoveryRef.current !== (github.discovery?.digest ?? null)) {
        setReviewed(false);
        setCopied(false);
        setSelected(github.review?.tools ?? []);
      }
      discoveryRef.current = github.discovery?.digest ?? null;
      setCatalog(entries);
      setProjects(products);
      setRepositories(repos);
      setGrants(permissions);
      setGrantReadProject(projectRef.current);
    },
    [connection],
  );
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setOperation("read");
    void load(controller)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(searchError(cause, ""));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => active.current?.abort();
  }, [load]);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  async function reload() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setOperation("read");
    setError("");
    setKey("");
    setRevokeId("");
    try {
      await load(controller);
      if (!controller.signal.aborted) {
        setUncertain(false);
        setNotice(
          "Current state reloaded. Review provider usage before another remote request.",
        );
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(searchError(cause, ""));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function start() {
    if (!current || busy || uncertain) return;
    setEnabled(current.enabled);
    setKey("");
    setRemove(false);
    setSelected(current.review?.tools ?? []);
    setReviewed(false);
    setStep(0);
    setError("");
    setNotice("");
    setEditing(true);
    setPrerequisiteId("");
    setCopied(false);
  }
  async function changeProject(value: string) {
    if (busy || uncertain) return;
    active.current?.abort();
    projectRef.current = value;
    setProjectId(value);
    setRepositoryId("");
    setGrants([]);
    setGrantReadProject("");
    setRevokeId("");
    setError("");
    if (!value) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setOperation("read");
    try {
      const rows = readGrants(
        await request<unknown>(
          connection,
          `/tools/mcp/projects/${value}/grants`,
          { signal: controller.signal },
        ),
        value,
      );
      if (!controller.signal.aborted) {
        setGrants(rows);
        setGrantReadProject(value);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(searchError(cause, ""));
        setUncertain(true);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function mutate(
    path: string,
    method: string,
    body: unknown,
    validate: (value: unknown) => void,
    message: string,
    after?: () => void,
  ) {
    if (busy || uncertain) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setOperation(
      path.endsWith("/discover")
        ? "discover"
        : path.endsWith("/review")
          ? "review"
          : path.endsWith("/revoke")
            ? "revoke"
            : path.includes("/connectors/")
              ? "grant"
              : "save",
    );
    setError("");
    setNotice("");
    const privateKey = key;
    let receiptConfirmed = false;
    try {
      const value = await request<unknown>(connection, path, {
        method,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: controller.signal,
      });
      validate(value);
      receiptConfirmed = true;
      await load(controller);
      if (controller.signal.aborted) return;
      setNotice(message);
      after?.();
    } catch (cause) {
      if (controller.signal.aborted) return;
      const rejected =
        cause instanceof ApiError &&
        cause.status >= 400 &&
        cause.status < 500 &&
        cause.status !== 408;
      setUncertain(receiptConfirmed || !rejected);
      setError(
        (rejected && !receiptConfirmed
          ? ""
          : "The outcome is not confirmed. Reload current state before another change. ") +
          searchError(cause, privateKey),
      );
    } finally {
      if (!controller.signal.aborted) {
        setKey("");
        setBusy(false);
      }
    }
  }
  async function saveConnection(event: FormEvent) {
    event.preventDefault();
    if (!current) return;
    try {
      const payload = connectionPayload(current, enabled, key, remove);
      await mutate(
        "/tools/mcp/github",
        "PUT",
        payload,
        (value) => {
          if (readConnector(value).id !== "github")
            throw new Error("The connection response was not confirmed.");
        },
        "Connection saved.",
        () => {
          setRemove(false);
          setStep(1);
        },
      );
    } catch (cause) {
      setError(searchError(cause, key));
    }
  }
  async function discover() {
    if (!current?.enabled || !current.credential_configured) return;
    await mutate(
      "/tools/mcp/github/discover",
      "POST",
      undefined,
      (value) => {
        readConnector({
          ...current,
          discovery: value,
          review: null,
        });
      },
      "Discovery returned.",
      () => setStep(2),
    );
  }
  async function review(event: FormEvent) {
    event.preventDefault();
    if (!current?.discovery || !selected.length || !reviewed) return;
    await mutate(
      "/tools/mcp/github/review",
      "POST",
      { digest: current.discovery.digest, tools: selected },
      (value) => {
        if (!readConnector(value).review)
          throw new Error("The schema review response was not confirmed.");
      },
      "Schema review recorded.",
      () => setStep(3),
    );
  }
  async function grant(event: FormEvent) {
    event.preventDefault();
    if (!current || !project || !repository) return;
    try {
      const payload = grantPayload(
        current,
        project,
        repository,
        selected,
        roles,
        grants,
      );
      await mutate(
        `/tools/mcp/projects/${project.id}/connectors/github/grants`,
        "POST",
        payload,
        (value) => {
          const result = readGrants([value], project.id)[0];
          if (result.repository_id !== repository.id || !result.active)
            throw new Error("The grant response was not confirmed.");
        },
        "Grant request recorded. The list shows its current state.",
      );
    } catch (cause) {
      setError(searchError(cause, ""));
    }
  }
  async function revoke(row: ToolGrant) {
    await mutate(
      `/tools/mcp/projects/${row.project_id}/grants/${row.id}/revoke`,
      "POST",
      undefined,
      (value) => {
        const result = readGrants([value], row.project_id)[0];
        if (result.id !== row.id || result.active)
          throw new Error("Revocation was not confirmed.");
      },
      "Revocation recorded. An already sent request cannot be undone.",
      () => setRevokeId(""),
    );
  }
  function toggleTool(name: string) {
    setSelected((items) =>
      items.includes(name)
        ? items.filter((item) => item !== name)
        : [...items, name],
    );
    setReviewed(false);
  }
  const locked = busy || uncertain;
  const stepDescription = [
    "Save an authorized GitHub key and choose whether the connection is enabled.",
    "This separate remote request may use provider quota. It does not execute or grant a tool.",
    "Choose known read tools after inspecting their parameters. Remote descriptions cannot grant authority.",
    "A project grant and the run's external_mcp role permission are both required.",
  ][step];
  const shortContext = (
    <>
      <p className="external-short-context">{stepDescription}</p>
      {notice && (
        <p
          className="external-short-context external-wizard-notice"
          role="status"
        >
          {notice}
        </p>
      )}
    </>
  );
  return (
    <div ref={view} className="search-configuration external-tools">
      {busy && (
        <p role="status" className="external-hint">
          {operation === "read"
            ? "Reading current factory state…"
            : operation === "discover"
              ? "Discovering schemas through the explicit remote request…"
              : operation === "revoke"
                ? "Recording revocation…"
                : "Recording the requested change…"}
        </p>
      )}
      {error && (
        <div className="alert" role="alert" ref={alert} tabIndex={-1}>
          {error}
        </div>
      )}
      {notice && !editing && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <div className="external-toolbar">
        {editing || prerequisite ? (
          <button
            className="text-button external-back"
            aria-label="All external tools"
            disabled={locked}
            onClick={() => {
              setEditing(false);
              setPrerequisiteId("");
              setKey("");
              setError("");
            }}
          >
            <ArrowLeft size={16} />
            All tools
          </button>
        ) : (
          <p>
            Connect reviewed tools to one project repository. Keys stay on the
            factory server.
          </p>
        )}
        <button
          className="secondary"
          aria-label="Reload current state"
          disabled={busy}
          onClick={() => void reload()}
        >
          <RefreshCw size={16} />
          Reload
        </button>
      </div>
      {!catalog.length && (
        <section className="panel search-state">
          <h2>
            {busy ? "Reading external tools" : "External tools unavailable"}
          </h2>
          <p>Reading saved settings does not contact an external service.</p>
        </section>
      )}
      {!!catalog.length && !editing && !prerequisite && (
        <section
          className="administration-cards"
          aria-label="External tool connectors"
        >
          {catalog.map((item) => (
            <article key={item.id}>
              {item.id === "github" ? (
                <Github size={24} />
              ) : (
                <Waypoints size={24} />
              )}
              <h2>{item.name}</h2>
              <span className="badge">{stateNames[item.readiness]}</span>
              <p>
                {item.prerequisite ??
                  "Read selected GitHub repository information through the fixed, reviewed connection."}
              </p>
              {!item.prerequisite && (
                <>
                  <p>
                    {item.credential_configured
                      ? "A key is stored privately. No live tool execution is verified here."
                      : "No active key is configured."}
                  </p>
                  <button className="primary" disabled={locked} onClick={start}>
                    <ArrowRight size={16} />
                    {item.credential_configured
                      ? "Manage GitHub tools"
                      : "Set up GitHub tools"}
                  </button>
                </>
              )}
              {item.prerequisite && (
                <button
                  className="secondary"
                  disabled={locked}
                  onClick={() => {
                    setPrerequisiteId(item.id);
                    setNotice("");
                  }}
                >
                  <ArrowRight size={16} />
                  View {item.name} setup steps
                </button>
              )}
            </article>
          ))}
        </section>
      )}
      {prerequisite && (
        <ExternalToolPrerequisites
          key={prerequisite.id}
          connector={prerequisite}
          onAgents={onAgents}
        />
      )}
      {current && editing && (
        <>
          <Wizard
            className="external-tools-wizard"
            steps={["Connection", "Discover", "Review", "Project access"]}
            active={step}
            onStep={(next) => {
              setStep(next);
              setError("");
            }}
            locked={locked}
            progressMode="visited"
            title={
              [
                "Connect GitHub privately",
                "Discover available tool schemas",
                "Review the exact tool schemas",
                "Grant access to a project repository",
              ][step]
            }
            description={stepDescription}
            headingAction={
              notice ? (
                <p className="external-wizard-notice" role="status">
                  {notice}
                </p>
              ) : undefined
            }
            guide={
              <>
                <ShieldCheck size={28} />
                <h3>
                  {
                    [
                      "Platform credentials",
                      "One explicit discovery",
                      "A pinned review",
                      "Scoped execution",
                    ][step]
                  }
                </h3>
                <p>
                  {
                    [
                      "Saving contacts only your factory. Leaving a saved key blank preserves it. Rotation requires fresh discovery and review before new grants.",
                      "The endpoint is fixed by the approved connector. No arbitrary server, command or resource URL can be added.",
                      "The digest identifies the exact discovered schema revision. A changed schema requires another explicit discovery and review.",
                      "Grants are limited to this product's registered GitHub repository, selected tools and implementation roles. Frozen runs keep their grant and key revision only while the grant is active and the connection is enabled.",
                    ][step]
                  }
                </p>
                {step === 2 && current.discovery && (
                  <dl className="wizard-summary">
                    <div>
                      <dt>Selected read tools</dt>
                      <dd>
                        {selected.length} of{" "}
                        {Object.keys(current.discovery.tools).length}
                      </dd>
                    </div>
                    <div>
                      <dt>Selection</dt>
                      <dd>
                        {selected.length
                          ? selected.map((name) => toolNames[name]).join(", ")
                          : "Choose the tools this project needs."}
                      </dd>
                    </div>
                    <div>
                      <dt>Review</dt>
                      <dd>
                        {reviewed
                          ? "Acknowledged for this selection"
                          : "Inspect the parameters, then acknowledge this selection."}
                      </dd>
                    </div>
                  </dl>
                )}
              </>
            }
          >
            {step === 0 && (
              <form onSubmit={(event) => void saveConnection(event)}>
                {shortContext}
                <div className="field">
                  <label htmlFor="mcp-key">
                    <KeyRound size={16} />
                    GitHub provider key
                  </label>
                  <input
                    id="mcp-key"
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    maxLength={4096}
                    value={key}
                    disabled={locked || remove}
                    onChange={(event) => setKey(event.target.value)}
                    aria-describedby="mcp-key-hint"
                  />
                  <small id="mcp-key-hint">
                    {current.credential_configured
                      ? "Leave blank to keep the saved key. Its value is never returned to this page."
                      : "Enter a narrowly scoped authorized GitHub key. The factory stores it privately."}
                  </small>
                </div>
                <label className="check-choice">
                  <input
                    type="checkbox"
                    checked={enabled}
                    disabled={locked || remove}
                    onChange={(event) => setEnabled(event.target.checked)}
                  />
                  Enable this connection
                </label>
                {current.credential_configured && (
                  <details className="search-removal">
                    <summary>Remove active access</summary>
                    <p>
                      Removing the key disables the connection and revokes its
                      project grants. Historical keys remain private for
                      traceability.
                    </p>
                    <label className="check-choice">
                      <input
                        type="checkbox"
                        checked={remove}
                        disabled={locked}
                        onChange={(event) => {
                          setRemove(event.target.checked);
                          if (event.target.checked) {
                            setEnabled(false);
                            setKey("");
                          }
                        }}
                      />
                      Remove the active key and revoke grants
                    </label>
                  </details>
                )}
                <div className="wizard-actions">
                  <button
                    type="button"
                    className="secondary"
                    disabled={locked}
                    onClick={() => {
                      setEditing(false);
                      setKey("");
                    }}
                  >
                    Cancel
                  </button>
                  <button className="primary" disabled={locked}>
                    <Check size={16} />
                    {busy && operation === "save"
                      ? "Saving…"
                      : "Save connection and continue"}
                  </button>
                </div>
              </form>
            )}
            {step === 1 && (
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void discover();
                }}
              >
                {shortContext}
                <dl className="wizard-summary">
                  <div>
                    <dt>Connection</dt>
                    <dd>{current.enabled ? "Enabled" : "Disabled"}</dd>
                  </div>
                  <div>
                    <dt>Private key</dt>
                    <dd>
                      {current.credential_configured
                        ? "Configured"
                        : "Required"}
                    </dd>
                  </div>
                  <div>
                    <dt>Server</dt>
                    <dd>{current.endpoint}</dd>
                  </div>
                </dl>
                {(!current.enabled || !current.credential_configured) && (
                  <p className="external-hint">
                    Enable a configured connection before discovery. Return to
                    Connection to change it.
                  </p>
                )}
                <div className="wizard-actions">
                  <button
                    type="button"
                    className="secondary"
                    disabled={locked}
                    onClick={() => setStep(0)}
                  >
                    <ArrowLeft size={16} />
                    Back
                  </button>
                  <div>
                    {current.discovery && (
                      <button
                        type="button"
                        className="secondary"
                        disabled={locked}
                        onClick={() => {
                          setSelected(current.review?.tools ?? []);
                          setReviewed(false);
                          setStep(2);
                        }}
                      >
                        Review saved discovery
                      </button>
                    )}
                    <button
                      className="primary"
                      disabled={
                        locked ||
                        !current.enabled ||
                        !current.credential_configured
                      }
                    >
                      {busy && operation === "discover"
                        ? "Discovering…"
                        : "Discover schemas (remote request)"}
                    </button>
                  </div>
                </div>
              </form>
            )}
            {step === 2 &&
              (!current.discovery ? (
                <p>Discover the current tool schemas before reviewing them.</p>
              ) : (
                <form onSubmit={(event) => void review(event)}>
                  {shortContext}
                  <div className="external-digest">
                    <div className="section-heading">
                      <span>
                        Schema revision{" "}
                        <code>{current.discovery.digest.slice(0, 12)}…</code>
                      </span>
                      <button
                        className="text-button"
                        type="button"
                        onClick={() =>
                          void navigator.clipboard
                            .writeText(current.discovery!.digest)
                            .then(() => setCopied(true))
                            .catch(() =>
                              setError(
                                "Copy unavailable. Select the digest to copy.",
                              ),
                            )
                        }
                      >
                        <Copy size={14} />
                        {copied ? "Copied" : "Copy digest"}
                      </button>
                    </div>
                    <details>
                      <summary>Inspect exact discovery digest</summary>
                      <code>{current.discovery.digest}</code>
                    </details>
                  </div>
                  <ToolChoices
                    schemas={current.discovery.tools}
                    selected={selected}
                    locked={locked}
                    onToggle={toggleTool}
                  />
                  <label className="check-choice">
                    <input
                      type="checkbox"
                      checked={reviewed}
                      disabled={locked}
                      onChange={(event) => setReviewed(event.target.checked)}
                    />
                    I reviewed these tool names and parameters
                  </label>
                  <div className="wizard-actions">
                    <button
                      className="secondary"
                      type="button"
                      disabled={locked}
                      onClick={() => setStep(1)}
                    >
                      <ArrowLeft size={16} />
                      Back
                    </button>
                    <button
                      className="primary"
                      disabled={locked || !selected.length || !reviewed}
                    >
                      <Check size={16} />
                      {busy && operation === "review"
                        ? "Recording…"
                        : "Record schema review"}
                    </button>
                  </div>
                </form>
              ))}
            {step === 3 && (
              <form onSubmit={(event) => void grant(event)}>
                {shortContext}
                <div className="field">
                  <label htmlFor="mcp-project">Product project</label>
                  <select
                    id="mcp-project"
                    required
                    value={projectId}
                    disabled={locked}
                    onChange={(event) => void changeProject(event.target.value)}
                  >
                    <option value="">Choose a project</option>
                    {projects.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.definition.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="mcp-repository">Project repository</label>
                  <select
                    id="mcp-repository"
                    required
                    value={repositoryId}
                    disabled={locked || !project}
                    onChange={(event) => setRepositoryId(event.target.value)}
                  >
                    <option value="">
                      Choose a registered GitHub repository
                    </option>
                    {availableRepositories.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                  <small>
                    Only registered HTTPS GitHub repositories belonging to this
                    product are available.
                  </small>
                </div>
                {current.review ? (
                  <ToolChoices
                    schemas={Object.fromEntries(
                      current.review.tools.map((name) => [
                        name,
                        current.discovery!.tools[name],
                      ]),
                    )}
                    selected={selected}
                    locked={locked}
                    onToggle={toggleTool}
                  />
                ) : (
                  <p className="external-hint">
                    The connection needs an enabled, exact schema review before
                    granting tools.
                  </p>
                )}
                <fieldset className="external-role-fields">
                  <legend>Execution roles</legend>
                  {(["implementer", "docs"] as ToolRole[]).map((role) => (
                    <label className="check-choice" key={role}>
                      <input
                        type="checkbox"
                        checked={roles.includes(role)}
                        disabled={locked}
                        onChange={(event) =>
                          setRoles((items) =>
                            event.target.checked
                              ? [...items, role]
                              : items.filter((item) => item !== role),
                          )
                        }
                      />
                      {role === "implementer" ? "Implementer" : "Documentation"}
                    </label>
                  ))}
                </fieldset>
                <p className="external-hint">
                  Stage: Implementation. This does not grant the worker's
                  external_mcp capability; configure that role separately in
                  Agents & skills.
                </p>
                <p className="external-hint">
                  Native agentic and Bridge workers use these grants. Official
                  Codex and Claude CLI tools keep their own permissions.
                </p>
                <div className="wizard-actions">
                  <button
                    className="secondary"
                    type="button"
                    disabled={locked}
                    onClick={() => setStep(2)}
                  >
                    <ArrowLeft size={16} />
                    Back
                  </button>
                  <button
                    className="primary"
                    disabled={
                      locked ||
                      !current.enabled ||
                      !current.review ||
                      !project ||
                      !repository ||
                      !selected.length ||
                      !roles.length
                    }
                  >
                    <Check size={16} />
                    {busy && operation === "grant"
                      ? "Recording…"
                      : "Grant project access"}
                  </button>
                </div>
              </form>
            )}
          </Wizard>
          {step === 3 && project && (
            <section
              className="panel search-state"
              aria-label="Current project grants"
            >
              <h2>{project.definition.name}: current grants</h2>
              {grantReadProject !== project.id ? (
                <p>
                  {busy
                    ? "Reading current project grants…"
                    : "Current grants could not be read. Reload current state before making changes."}
                </p>
              ) : !grants.length ? (
                <p>No external tool grants are recorded for this project.</p>
              ) : (
                <>
                  <p>
                    Showing the latest {Math.min(50, grants.length)} of{" "}
                    {grants.length} retained grants.
                  </p>
                  <div className="external-grants">
                    {grants
                      .slice(-50)
                      .reverse()
                      .map((row) => (
                        <article key={row.id}>
                          <div className="section-heading">
                            <h3>
                              {row.scope.owner}/{row.scope.repo}
                            </h3>
                            <span className="badge">
                              {row.active ? "Active" : "Revoked"}
                            </span>
                          </div>
                          <p>
                            {Object.keys(row.tools)
                              .map((name) => toolNames[name])
                              .join(", ")}
                          </p>
                          <p>
                            Roles:{" "}
                            {row.roles
                              .map((role) =>
                                role === "docs"
                                  ? "Documentation"
                                  : "Implementer",
                              )
                              .join(", ")}
                            . Stage: Implementation.
                          </p>
                          <details>
                            <summary>Grant identity</summary>
                            <code>{row.id}</code>
                            <p>
                              Key revision:{" "}
                              <code>{row.credential_revision}</code>
                            </p>
                            <p>
                              {!row.active
                                ? "Revoked. This grant no longer authorizes calls, including queued runs."
                                : !current.enabled
                                  ? "Connection disabled. This grant cannot authorize calls."
                                  : row.credential_revision ===
                                      current.discovery?.credential_revision
                                    ? "Matches the current discovered key revision. Execution still requires the granted role and stage."
                                    : "Uses an earlier key revision. Only runs already frozen with this active grant may use it while the connection stays enabled."}
                            </p>
                          </details>
                          {row.active &&
                            (revokeId === row.id ? (
                              <div className="external-revoke">
                                <p>
                                  Stop future calls through this grant,
                                  including queued runs. An already sent request
                                  cannot be undone.
                                </p>
                                <div className="search-actions">
                                  <button
                                    className="secondary"
                                    disabled={locked}
                                    onClick={() => setRevokeId("")}
                                  >
                                    Keep grant
                                  </button>
                                  <button
                                    className="primary"
                                    disabled={locked}
                                    onClick={() => void revoke(row)}
                                  >
                                    <Unplug size={16} />
                                    Confirm revoke
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <button
                                className="secondary"
                                disabled={locked}
                                onClick={() => setRevokeId(row.id)}
                              >
                                <Unplug size={16} />
                                Revoke access
                              </button>
                            ))}
                        </article>
                      ))}
                  </div>
                </>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
function ToolChoices({
  schemas,
  selected,
  locked,
  onToggle,
}: {
  schemas: Record<string, Record<string, unknown>>;
  selected: string[];
  locked: boolean;
  onToggle: (name: string) => void;
}) {
  return (
    <fieldset className="external-tool-fields">
      <legend>Allowed read tools</legend>
      {Object.entries(schemas).map(([name, schema]) => (
        <div className="external-schema" key={name}>
          <label className="check-choice">
            <input
              type="checkbox"
              checked={selected.includes(name)}
              disabled={locked}
              onChange={() => onToggle(name)}
            />
            {toolNames[name]}
          </label>
          <code>{name}</code>
          <details>
            <summary>Inspect parameters and exact schema</summary>
            {schema.properties &&
            typeof schema.properties === "object" &&
            Object.keys(schema.properties).length > 64 ? (
              <p>
                Showing the first 64 parameters. The original reviewed schema
                below retains every field.
              </p>
            ) : null}
            <table>
              <thead>
                <tr>
                  <th>Parameter</th>
                  <th>Type</th>
                  <th>Required</th>
                </tr>
              </thead>
              <tbody>
                {toolFields(schema).map((field, index) => (
                  <tr key={index}>
                    <td>{field.name}</td>
                    <td>{field.type}</td>
                    <td>{field.required ? "Yes" : "No"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <details>
              <summary>Advanced: original reviewed schema</summary>
              <OutputView
                label="Reviewed schema"
                value={JSON.stringify(schema, null, 2)}
                path="schema.json"
              />
            </details>
          </details>
        </div>
      ))}
    </fieldset>
  );
}
