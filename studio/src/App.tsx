/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Box,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Code2,
  ExternalLink,
  FileDiff,
  FolderGit2,
  GitBranch,
  Hammer,
  Layers3,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Square,
  Terminal,
  Unplug,
  X,
} from "lucide-react";
import {
  normalizeBase,
  parseCommands,
  post,
  request,
  type Connection,
  type ForgeRun,
  type Repository,
} from "./api";

type Page = "overview" | "new" | "repositories" | "settings" | "run";
const working = (status: string) =>
  [
    "queued",
    "running",
    "cloning",
    "generating",
    "verifying",
    "publishing",
    "pending",
    "executing",
    "preparing",
    "developing",
    "validating",
  ].includes(status);
const errorText = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";
const human = (value: unknown) => String(value ?? "").replaceAll("_", " ");
const date = (value?: string) =>
  value && !Number.isNaN(Date.parse(value))
    ? new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Time not reported";
const records = (value: unknown): Array<[string, Record<string, unknown>]> => {
  if (Array.isArray(value))
    return value.map((entry, index) => [
      String(entry?.id ?? entry?.name ?? index),
      typeof entry === "object" && entry !== null ? entry : { value: entry },
    ]);
  if (typeof value === "object" && value !== null)
    return Object.entries(value).map(([name, entry]) => [
      name,
      typeof entry === "object" && entry !== null
        ? (entry as Record<string, unknown>)
        : { value: entry },
    ]);
  return [];
};
function Badge({ status }: { status: string }) {
  return (
    <span
      className={
        "badge " +
        (working(status)
          ? "working"
          : ["succeeded", "completed", "passed", "ready"].includes(status)
            ? "success"
            : ["failed", "error", "unavailable"].includes(status)
              ? "failed"
              : "")
      }
    >
      <span />
      {human(status)}
    </span>
  );
}
function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {isValidElement(children)
        ? cloneElement(
            children as ReactElement<{
              id?: string;
              "aria-describedby"?: string;
            }>,
            { id, "aria-describedby": hint ? id + "-hint" : undefined },
          )
        : children}
      {hint && <small id={id + "-hint"}>{hint}</small>}
    </div>
  );
}
function Empty({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <Box size={30} strokeWidth={1.4} />
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [connection, setConnection] = useState<Connection>({
    base: "/api/v1",
    token: "",
  });
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [runs, setRuns] = useState<ForgeRun[]>([]);
  const [providers, setProviders] = useState<unknown>(null);
  const [profiles, setProfiles] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<ForgeRun | null>(null);
  const [detailError, setDetailError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState(false);
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!connection.token) return;
      setLoading(true);
      setError("");
      try {
        const [repositoryResult, runResult, providerResult, profileResult] =
          await Promise.all([
            request<Repository[]>(connection, "/repositories", { signal }),
            request<ForgeRun[]>(connection, "/runs", { signal }),
            request<unknown>(connection, "/providers", { signal }),
            request<unknown>(connection, "/profiles", { signal }),
          ]);
        setRepositories(repositoryResult);
        setRuns(runResult);
        setProviders(providerResult);
        setProfiles(profileResult);
        setConnected(true);
      } catch (cause) {
        if (!signal?.aborted) {
          setError(errorText(cause));
          setConnected(false);
        }
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [connection],
  );
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    if (!connected || !runs.some((run) => working(run.status))) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [connected, runs, refresh]);
  useEffect(() => {
    if (page !== "run" || !selected?.id || !working(selected.status)) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      request<ForgeRun>(
        connection,
        "/runs/" + encodeURIComponent(selected.id),
        { signal: controller.signal },
      )
        .then(setSelected)
        .catch((cause) => {
          if (!controller.signal.aborted) setDetailError(errorText(cause));
        });
    }, 2500);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [page, selected?.id, selected?.status, connection]);
  async function openRun(run: ForgeRun) {
    setPage("run");
    setSelected(run);
    setDetailError("");
    setBusy(true);
    try {
      setSelected(
        await request<ForgeRun>(
          connection,
          "/runs/" + encodeURIComponent(run.id),
        ),
      );
    } catch (cause) {
      setDetailError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  async function runAction(action: "cancel" | "retry" | "publish") {
    if (!selected) return;
    setBusy(true);
    setDetailError("");
    try {
      const result = await post<ForgeRun>(
        connection,
        "/runs/" + encodeURIComponent(selected.id) + "/" + action,
      );
      setSelected(result);
      setNotice(
        action === "cancel"
          ? "Cancellation requested."
          : action === "publish"
            ? "Pull request publication completed."
            : "A new run was requested.",
      );
      void refresh();
    } catch (cause) {
      setDetailError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  function navigate(next: Page) {
    setPage(next);
    setError("");
  }
  const repositoryName = (id: string) =>
    repositories.find((repository) => repository.id === id)?.name ?? id;
  const nav = [
    { page: "overview" as const, icon: Layers3, label: "Factory" },
    { page: "new" as const, icon: Hammer, label: "New product" },
    { page: "repositories" as const, icon: FolderGit2, label: "Repositories" },
    { page: "settings" as const, icon: Settings2, label: "Connections" },
  ];
  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            navigate("overview");
          }}
        >
          <img src="/assets/forge-lockup.svg" alt="Firefly Forge" />
        </a>
        <div className="workspace">
          <span className="workspace-mark">
            <Hammer size={17} />
          </span>
          <div>
            Software factory<small>Local workspace</small>
          </div>
        </div>
        <nav aria-label="Main navigation">
          {nav.map((item) => (
            <button
              key={item.page}
              className={
                page === item.page ||
                (page === "run" && item.page === "overview")
                  ? "active"
                  : ""
              }
              onClick={() => navigate(item.page)}
              aria-current={page === item.page ? "page" : undefined}
            >
              <item.icon size={19} />
              <span>{item.label}</span>
              {item.page === "repositories" && (
                <small>{repositories.length}</small>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="build-note">
            <ShieldCheck size={20} />
            <p>
              Built on evidence.
              <small>Review every generated change before delivery.</small>
            </p>
          </div>
          <a
            href="https://github.com/fireflyframework"
            target="_blank"
            rel="noreferrer"
          >
            Firefly Software Foundation
            <ExternalLink size={13} />
          </a>
        </div>
      </aside>
      <div className="workspace-main">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace
            <ChevronRight size={14} />
            <strong>
              {page === "run"
                ? "Run details"
                : nav.find((item) => item.page === page)?.label}
            </strong>
          </div>
          <button
            className={"connection-indicator " + (connected ? "online" : "")}
            onClick={() => navigate("settings")}
          >
            <span />
            {connected ? "API connected" : "Connect your API"}
            <Settings2 size={14} />
          </button>
        </header>
        <main id="main" tabIndex={-1}>
          {error && (
            <div className="alert" role="alert">
              <Unplug size={18} />
              <span>{error}</span>
              <button onClick={() => void refresh()}>Try again</button>
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              <Check size={17} />
              {notice}
              <button
                aria-label="Dismiss notification"
                onClick={() => setNotice("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {page === "overview" && (
            <>
              <div className="page-heading">
                <div>
                  <h1>Your software factory.</h1>
                  <p>
                    Give an idea a brief. Follow the work. Review the evidence.
                  </p>
                </div>
                <button className="primary" onClick={() => navigate("new")}>
                  <Plus size={17} />
                  New product
                </button>
              </div>
              <section
                className="factory-panel"
                aria-labelledby="production-title"
              >
                <div className="factory-title">
                  <span>
                    <Hammer size={18} />
                    <h2 id="production-title">
                      From intent to a verified change
                    </h2>
                  </span>
                  <span className="subtle">One isolated workspace per run</span>
                </div>
                <div className="production-line">
                  {[
                    {
                      icon: FileDiff,
                      title: "Define",
                      text: "Brief & repository",
                    },
                    {
                      icon: GitBranch,
                      title: "Isolate",
                      text: "Clone & branch",
                    },
                    { icon: Code2, title: "Build", text: "Agent & stack" },
                    {
                      icon: ShieldCheck,
                      title: "Verify",
                      text: "Commands & evidence",
                    },
                    { icon: Box, title: "Deliver", text: "Diff & artifacts" },
                  ].map((step, index) => (
                    <div className="production-step" key={step.title}>
                      <div className="machine">
                        <step.icon size={27} strokeWidth={1.4} />
                        <span>{index + 1}</span>
                      </div>
                      <h3>{step.title}</h3>
                      <p>{step.text}</p>
                      {index < 4 && <div className="conveyor" />}
                    </div>
                  ))}
                </div>
                <div className="factory-foot">
                  <ShieldCheck size={15} />
                  <span>
                    Generated changes stay local until you choose to publish.
                  </span>
                  <a
                    href="https://fireflyframework.github.io/firefly-forge/"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Read the guide
                    <ExternalLink size={13} />
                  </a>
                </div>
              </section>
              <div className="metrics">
                <div>
                  <span>Repositories</span>
                  <strong>
                    {repositories.length.toString().padStart(2, "0")}
                  </strong>
                  <FolderGit2 size={20} />
                </div>
                <div>
                  <span>Active runs</span>
                  <strong>
                    {runs
                      .filter((run) => working(run.status))
                      .length.toString()
                      .padStart(2, "0")}
                  </strong>
                  <Activity size={20} />
                </div>
                <div>
                  <span>Completed runs</span>
                  <strong>
                    {runs
                      .filter((run) =>
                        ["completed", "succeeded"].includes(run.status),
                      )
                      .length.toString()
                      .padStart(2, "0")}
                  </strong>
                  <CheckCircle2 size={20} />
                </div>
              </div>
              <section className="runs-section">
                <div className="section-heading">
                  <h2>
                    Recent runs <span>{runs.length}</span>
                  </h2>
                  <button
                    className="quiet"
                    onClick={() => void refresh()}
                    disabled={!connection.token || loading}
                  >
                    <RefreshCw size={15} className={loading ? "spin" : ""} />
                    Refresh
                  </button>
                </div>
                {loading && !runs.length ? (
                  <Loading />
                ) : runs.length ? (
                  <div className="run-list">
                    {runs.map((run) => (
                      <button
                        className="run-row"
                        key={run.id}
                        onClick={() => void openRun(run)}
                      >
                        <span className="run-icon">
                          <GitBranch size={19} />
                        </span>
                        <span className="run-description">
                          <strong>
                            {run.brief?.split("\n")[0] ||
                              "Software generation run"}
                          </strong>
                          <small>
                            {repositoryName(run.repository_id)} ·{" "}
                            {run.agent || "Agent"} · {run.backend || "Stack"}
                          </small>
                        </span>
                        <Badge status={run.status} />
                        <time>{date(run.created_at)}</time>
                        <ChevronRight size={17} />
                      </button>
                    ))}
                  </div>
                ) : (
                  <Empty
                    title={
                      connected
                        ? "Ready for your first run"
                        : "Bring your workspace online"
                    }
                    body={
                      connected
                        ? "Start with a brief and a repository. The complete run history will appear here."
                        : "Connect to your Forge API to see repositories, provider readiness and real run history."
                    }
                    action={
                      <button
                        className="secondary"
                        onClick={() => navigate(connected ? "new" : "settings")}
                      >
                        {connected ? "Create a product" : "Connect your API"}
                        <ArrowRight size={15} />
                      </button>
                    }
                  />
                )}
              </section>
            </>
          )}
          {page === "new" && (
            <NewProduct
              repositories={repositories}
              profiles={profiles}
              connected={connected}
              connection={connection}
              onRepository={() => navigate("repositories")}
              onConnection={() => navigate("settings")}
              onCreated={(run) => {
                setNotice("Your run has been queued.");
                void refresh();
                void openRun(run);
              }}
            />
          )}
          {page === "repositories" && (
            <>
              <div className="page-heading">
                <div>
                  <h1>Your source, connected.</h1>
                  <p>
                    Register a Git repository to give every run a clear starting
                    point.
                  </p>
                </div>
                <button
                  className="primary"
                  onClick={() => setDialog(true)}
                  disabled={!connected}
                >
                  <Plus size={17} />
                  Add repository
                </button>
              </div>
              <div className="repository-list">
                {repositories.length ? (
                  repositories.map((repository) => (
                    <article className="repository" key={repository.id}>
                      <span className="repository-icon">
                        <FolderGit2 size={24} />
                      </span>
                      <div>
                        <h2>{repository.name}</h2>
                        <p>{repository.url}</p>
                        <div className="repo-meta">
                          <span>{human(repository.provider)}</span>
                          <span>
                            <GitBranch size={13} />
                            {repository.default_branch}
                          </span>
                        </div>
                      </div>
                      <button className="quiet" onClick={() => navigate("new")}>
                        Start a run
                        <ArrowRight size={15} />
                      </button>
                    </article>
                  ))
                ) : (
                  <Empty
                    title="No repositories registered"
                    body={
                      connected
                        ? "Add a Git URL your Forge server can access. Credentials are configured on the server."
                        : "Connect your API first, then register a repository."
                    }
                    action={
                      <button
                        className="secondary"
                        onClick={() =>
                          connected ? setDialog(true) : navigate("settings")
                        }
                      >
                        {connected ? "Add repository" : "Connect API"}
                        <Plus size={15} />
                      </button>
                    }
                  />
                )}
              </div>
              <div className="inline-note">
                <ShieldCheck size={17} />
                <p>
                  Repository credentials stay on the server. The factory creates
                  an isolated branch and workspace for each run.
                </p>
              </div>
            </>
          )}
          {page === "settings" && (
            <Connections
              current={connection}
              connected={connected}
              providers={providers}
              loading={loading}
              onSave={(next) => {
                setConnection(next);
                setConnected(false);
                setNotice("Connection updated. Checking your API.");
              }}
            />
          )}
          {page === "run" && selected && (
            <>
              <button
                className="back quiet"
                onClick={() => navigate("overview")}
              >
                <ArrowLeft size={16} />
                All runs
              </button>
              <div className="page-heading run-heading">
                <div>
                  <div className="run-id">Run {selected.id}</div>
                  <h1>
                    {selected.brief?.split("\n")[0] || "Software generation"}
                  </h1>
                  <p>
                    {repositoryName(selected.repository_id)} ·{" "}
                    {selected.backend} / {selected.frontend} · {selected.agent}
                  </p>
                </div>
                <Badge status={selected.status} />
              </div>
              <div className="run-controls">
                <span>
                  <GitBranch size={16} />
                  Isolated change
                </span>
                {working(selected.status) ? (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => void runAction("cancel")}
                  >
                    <Square size={14} />
                    Cancel run
                  </button>
                ) : ["failed", "cancelled", "interrupted"].includes(
                    selected.status,
                  ) ? (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => void runAction("retry")}
                  >
                    <RefreshCw size={15} />
                    Retry run
                  </button>
                ) : null}
                {selected.status === "succeeded" && !selected.pr_url && (
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => void runAction("publish")}
                  >
                    <GitBranch size={15} />
                    Publish pull request
                  </button>
                )}
                {selected.pr_url && /^https?:\/\//.test(selected.pr_url) && (
                  <a
                    className="primary"
                    href={selected.pr_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open pull request
                    <ExternalLink size={15} />
                  </a>
                )}
              </div>
              {detailError && (
                <div className="alert" role="alert">
                  {detailError}
                </div>
              )}
              {selected.error && (
                <div className="alert" role="alert">
                  {selected.error}
                </div>
              )}
              <RunDetail run={selected} busy={busy} connection={connection} />
            </>
          )}
        </main>
        <footer>
          Firefly Forge{" "}
          <span>Software factory · Local changes, visible evidence.</span>
          <span>0.1.0</span>
        </footer>
      </div>
      {dialog && (
        <RepositoryDialog
          connection={connection}
          onClose={() => setDialog(false)}
          onAdded={() => {
            setDialog(false);
            setNotice("Repository registered.");
            void refresh();
          }}
        />
      )}
    </div>
  );
}
function Loading() {
  return (
    <div className="loading" role="status">
      <Loader2 size={20} className="spin" />
      Loading your workspace…
    </div>
  );
}
function NewProduct({
  repositories,
  profiles,
  connected,
  connection,
  onRepository,
  onConnection,
  onCreated,
}: {
  repositories: Repository[];
  profiles: unknown;
  connected: boolean;
  connection: Connection;
  onRepository: () => void;
  onConnection: () => void;
  onCreated: (run: ForgeRun) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [backend, setBackend] = useState("pyfly");
  const [frontend, setFrontend] = useState("react");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setError("");
    setBusy(true);
    try {
      const payload = {
        repository_id: String(data.get("repository_id")),
        brief: String(data.get("brief")),
        backend: String(data.get("backend")),
        frontend: String(data.get("frontend")),
        agent: String(data.get("agent")),
        commands: parseCommands(String(data.get("commands"))),
        publish: false as const,
      };
      onCreated(await post<ForgeRun>(connection, "/runs", payload));
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>What are we building?</h1>
          <p>
            Describe the outcome. Choose the foundation. Let the factory do the
            work.
          </p>
        </div>
      </div>
      <div className="creation-layout">
        <form
          className="product-form panel"
          onSubmit={(event) => void submit(event)}
        >
          <div className="form-section">
            <h2>
              <FileDiff size={18} />
              The brief
            </h2>
            <Field
              label="What should this change accomplish?"
              hint="Include the users, essential behavior and what success looks like."
            >
              <textarea
                name="brief"
                required
                minLength={10}
                rows={5}
                placeholder="Build a customer onboarding portal with an application form, document upload and a review queue…"
              />
            </Field>
            <Field label="Source repository">
              <select
                name="repository_id"
                required
                disabled={!repositories.length}
              >
                <option value="">Choose a repository</option>
                {repositories.map((repository) => (
                  <option key={repository.id} value={repository.id}>
                    {repository.name}
                  </option>
                ))}
              </select>
            </Field>
            {!repositories.length && (
              <button
                type="button"
                className="text-button"
                onClick={connected ? onRepository : onConnection}
              >
                {connected
                  ? "Register a repository first"
                  : "Connect your API first"}
                <ArrowRight size={14} />
              </button>
            )}
          </div>
          <div className="form-section">
            <h2>
              <Layers3 size={18} />
              The foundation
            </h2>
            <div className="two-columns">
              <Field label="Backend">
                <select
                  name="backend"
                  value={backend}
                  onChange={(event) => setBackend(event.target.value)}
                >
                  <option value="pyfly">PyFly · Python</option>
                  <option value="larafly">LaraFly · Laravel</option>
                  <optgroup label="Additional profiles — review catalog limits">
                    <option value="java">Firefly · Java</option>
                    <option value="rust">Rust</option>
                    <option value="dotnet">.NET</option>
                  </optgroup>
                </select>
              </Field>
              <Field label="Frontend">
                <select
                  name="frontend"
                  value={frontend}
                  onChange={(event) => setFrontend(event.target.value)}
                >
                  <option value="react">React</option>
                  <option value="angular">Angular</option>
                  <option value="nextjs">Next.js</option>
                  <option value="vue">Vue</option>
                  <option value="none">API only</option>
                </select>
              </Field>
            </div>
            <ProfileInfo profiles={profiles} kind="backends" id={backend} />
            <ProfileInfo profiles={profiles} kind="frontends" id={frontend} />
            <Field
              label="Execution agent"
              hint="The chosen provider must be configured on the Forge server."
            >
              <select name="agent" defaultValue="codex">
                <option value="codex">Codex</option>
                <option value="claude">Claude Code</option>
                <option value="bridge">Firefly Agent Bridge</option>
                <option value="agentic">Firefly Agentic</option>
              </select>
            </Field>
          </div>
          <div className="form-section">
            <h2>
              <ShieldCheck size={18} />
              Verification
            </h2>
            <Field
              label="Commands to run"
              hint='JSON argument arrays, for example [["uv","run","pytest"],["npm","test"]]. No shell expressions. Each command has a 120-second limit.'
            >
              <textarea
                name="commands"
                rows={3}
                placeholder='[["uv", "run", "pytest"]]'
                spellCheck={false}
                className="code-input"
              />
            </Field>
            <div className="inline-note">
              <ShieldCheck size={17} />
              <p>
                This run produces local changes and evidence. Publication is
                disabled for this request.
              </p>
            </div>
          </div>
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className="form-footer">
            <span>Review the result before delivery.</span>
            <button
              className="primary"
              disabled={busy || !connected || !repositories.length}
            >
              {busy ? (
                <Loader2 size={17} className="spin" />
              ) : (
                <Play size={16} />
              )}
              Start building
            </button>
          </div>
        </form>
        <aside className="brief-guide">
          <div className="guide-icon">
            <Hammer size={30} strokeWidth={1.3} />
          </div>
          <h2>A clear brief makes better software.</h2>
          <p>Think of this as a handoff to your engineering team.</p>
          <ol>
            <li>
              <strong>Name the outcome.</strong>
              <span>Explain the task people need to complete.</span>
            </li>
            <li>
              <strong>Set the boundaries.</strong>
              <span>State what to include, preserve and avoid.</span>
            </li>
            <li>
              <strong>Make success testable.</strong>
              <span>Add commands that verify the behavior you need.</span>
            </li>
          </ol>
          {profiles !== null && (
            <div className="profile-note">
              <CheckCircle2 size={16} />
              <span>Stack profiles loaded from your server.</span>
            </div>
          )}
        </aside>
      </div>
    </>
  );
}
function ProfileInfo({
  profiles,
  kind,
  id,
}: {
  profiles: unknown;
  kind: string;
  id: string;
}) {
  if (!profiles || typeof profiles !== "object") return null;
  const catalog = (profiles as Record<string, unknown>)[kind];
  const profile = records(catalog)
    .map(([, value]) => value)
    .find((value) => value.id === id);
  if (!profile || id === "none") return null;
  return (
    <div className="profile-detail">
      <strong>
        {String(profile.name ?? id)} {String(profile.version ?? "")}
      </strong>
      <p>{String(profile.detail ?? "")}</p>
      <small>
        {String(
          profile.validation ?? "Validation status not reported by the server.",
        )}
      </small>
    </div>
  );
}

function Connections({
  current,
  connected,
  providers,
  loading,
  onSave,
}: {
  current: Connection;
  connected: boolean;
  providers: unknown;
  loading: boolean;
  onSave: (connection: Connection) => void;
}) {
  const [error, setError] = useState("");
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      onSave({
        base: normalizeBase(String(data.get("base"))),
        token: String(data.get("token")).trim(),
      });
      setError("");
    } catch (cause) {
      setError(errorText(cause));
    }
  }
  const providerRecords = records(
    providers && typeof providers === "object" && "providers" in providers
      ? (providers as { providers: unknown }).providers
      : providers,
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Connect the factory.</h1>
          <p>
            Your API is the control center. Agents and Git credentials live on
            its server.
          </p>
        </div>
        <Badge status={connected ? "ready" : "disconnected"} />
      </div>
      <div className="settings-layout">
        <form className="panel settings-form" onSubmit={save}>
          <h2>
            <Unplug size={19} />
            Forge API
          </h2>
          <Field
            label="API address"
            hint="Use /api/v1 for the same server, or a complete API address."
          >
            <input
              name="base"
              required
              defaultValue={current.base}
              placeholder="/api/v1"
              autoComplete="url"
            />
          </Field>
          <Field
            label="API token"
            hint="Kept in this page's memory. Reloading the page clears it."
          >
            <input
              type="password"
              name="token"
              required
              defaultValue={current.token}
              placeholder="Enter FORGE_API_TOKEN"
              autoComplete="off"
            />
          </Field>
          <div className="inline-note">
            <ShieldCheck size={17} />
            <p>
              Use an HTTPS address when connecting to a remote API. Your token
              is sent only to the address you enter.
            </p>
          </div>
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <button className="primary" disabled={loading}>
            {loading ? (
              <Loader2 size={16} className="spin" />
            ) : (
              <Check size={16} />
            )}
            Save connection
          </button>
        </form>
        <section className="panel provider-panel">
          <h2>
            <Terminal size={19} />
            Execution providers
          </h2>
          <p className="subtle">Readiness reported by your Forge server.</p>
          {loading ? (
            <Loading />
          ) : providerRecords.length ? (
            providerRecords.map(([name, provider]) => (
              <div className="provider" key={name}>
                <span className="provider-icon">
                  <Terminal size={17} />
                </span>
                <div>
                  <strong>{human(provider.name ?? name)}</strong>
                  <small>
                    {String(
                      provider.reason ??
                        provider.description ??
                        provider.detail ??
                        "Configured on the server",
                    )}
                  </small>
                </div>
                <Badge
                  status={
                    provider.ready === true || provider.available === true
                      ? "ready"
                      : String(provider.status ?? "unavailable")
                  }
                />
              </div>
            ))
          ) : (
            <Empty
              title="Provider readiness is unavailable"
              body="Save a valid API connection to inspect configured agents."
            />
          )}
        </section>
      </div>
    </>
  );
}
function RepositoryDialog({
  connection,
  onClose,
  onAdded,
}: {
  connection: Connection;
  onClose: () => void;
  onAdded: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [previousFocus] = useState(() =>
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialogRef.current?.querySelector<HTMLElement>("input")?.focus();
    return () => previousFocus?.focus();
  }, [previousFocus]);
  useEffect(() => {
    function escape(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);
  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await post(
        connection,
        "/repositories",
        Object.fromEntries(data.entries()),
      );
      onAdded();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-backdrop">
      <dialog
        ref={dialogRef}
        open
        className="modal"
        aria-labelledby="repository-title"
        onKeyDown={(event) => {
          if (event.key === "Tab") {
            const focusable = Array.from(
              event.currentTarget.querySelectorAll<HTMLElement>(
                "button:not([disabled]), input, select",
              ),
            );
            const first = focusable[0];
            const last = focusable.at(-1);
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <div className="modal-heading">
          <h2 id="repository-title">Add a repository</h2>
          <button className="quiet" aria-label="Close dialog" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
        <form onSubmit={(event) => void add(event)}>
          <Field label="Repository name">
            <input
              name="name"
              required
              autoFocus
              placeholder="Customer portal"
            />
          </Field>
          <Field
            label="Git URL"
            hint="An HTTPS or SSH clone URL accessible to your server."
          >
            <input
              name="url"
              required
              placeholder="https://github.com/your-org/customer-portal.git"
            />
          </Field>
          <div className="two-columns">
            <Field label="Provider">
              <select name="provider" defaultValue="github">
                <option value="github">GitHub</option>
                <option value="gitlab">GitLab</option>
                <option value="bitbucket">Bitbucket</option>
                <option value="azure">Azure DevOps</option>
                <option value="gitea">Gitea</option>
                <option value="git">Generic Git</option>
              </select>
            </Field>
            <Field label="Default branch">
              <input name="default_branch" defaultValue="main" required />
            </Field>
          </div>
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button className="primary" disabled={busy}>
              {busy && <Loader2 size={16} className="spin" />}Add repository
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
function RunDetail({
  run,
  busy,
  connection,
}: {
  run: ForgeRun;
  busy: boolean;
  connection: Connection;
}) {
  const [tab, setTab] = useState<
    "timeline" | "diff" | "verification" | "artifacts"
  >("timeline");
  const tabs = [
    { id: "timeline" as const, icon: Activity, label: "Timeline" },
    { id: "diff" as const, icon: FileDiff, label: "Changes" },
    { id: "verification" as const, icon: ShieldCheck, label: "Verification" },
    { id: "artifacts" as const, icon: Box, label: "Artifacts" },
  ];
  const verification = records(run.verification);
  const [downloadError, setDownloadError] = useState("");
  async function download() {
    try {
      const result = await fetch(
        normalizeBase(connection.base) +
          "/runs/" +
          encodeURIComponent(run.id) +
          "/artifacts/changes.zip",
        { headers: { Authorization: "Bearer " + connection.token } },
      );
      if (!result.ok)
        throw new Error("Artifact download failed (" + result.status + ").");
      const url = URL.createObjectURL(await result.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "forge-" + run.id + "-changes.zip";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setDownloadError(errorText(cause));
    }
  }
  return (
    <section className="panel run-detail">
      <div className="detail-tabs" role="tablist" aria-label="Run evidence">
        {tabs.map((item) => (
          <button
            id={"tab-" + item.id}
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            aria-controls="run-evidence"
            tabIndex={tab === item.id ? 0 : -1}
            className={tab === item.id ? "active" : ""}
            onClick={() => setTab(item.id)}
            onKeyDown={(event) => {
              if (
                ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              ) {
                event.preventDefault();
                const index = tabs.findIndex((entry) => entry.id === tab);
                const next =
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? tabs.length - 1
                      : (index +
                          (event.key === "ArrowRight" ? 1 : -1) +
                          tabs.length) %
                        tabs.length;
                setTab(tabs[next].id);
                document.getElementById("tab-" + tabs[next].id)?.focus();
              }
            }}
          >
            <item.icon size={16} />
            {item.label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id="run-evidence"
        aria-labelledby={"tab-" + tab}
        className="evidence-content"
      >
        {busy && <Loading />}
        {tab === "timeline" &&
          (run.events?.length ? (
            <ol className="timeline">
              {run.events.map((event, index) => (
                <li key={index}>
                  <span className="timeline-dot">
                    <Circle size={10} />
                  </span>
                  <div>
                    <h3>
                      {human(
                        event.message ??
                          event.type ??
                          event.event ??
                          "Run event",
                      )}
                    </h3>
                    <p>{String(event.detail ?? event.stage ?? "")}</p>
                    <time>
                      {date(
                        String(
                          event.timestamp ?? event.at ?? event.created_at ?? "",
                        ),
                      )}
                    </time>
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <Empty
              title="Waiting for the first event"
              body="The server records progress as the isolated run advances."
            />
          ))}
        {tab === "diff" && (
          <>
            <div className="changed-files">
              <h3>
                Changed files <span>{run.changed_files?.length ?? 0}</span>
              </h3>
              {run.changed_files?.map((file) => (
                <div key={file}>
                  <FileDiff size={14} />
                  {file}
                </div>
              ))}
            </div>
            {run.diff ? (
              <pre className="diff" aria-label="Git diff">
                {run.diff.split("\n").map((line, index) => (
                  <span
                    key={index}
                    className={
                      line.startsWith("+")
                        ? "addition"
                        : line.startsWith("-")
                          ? "deletion"
                          : line.startsWith("@@")
                            ? "hunk"
                            : ""
                    }
                  >
                    {line}
                    {"\n"}
                  </span>
                ))}
              </pre>
            ) : (
              <Empty
                title="No diff reported yet"
                body="Generated file changes appear here after the agent finishes."
              />
            )}
          </>
        )}
        {tab === "verification" &&
          (verification.length ? (
            verification.map(([name, result]) => (
              <article className="verification" key={name}>
                <div>
                  <Terminal size={18} />
                  <strong>
                    {Array.isArray(result.argv)
                      ? result.argv.join(" ")
                      : String(result.command ?? result.name ?? name)}
                  </strong>
                  <Badge
                    status={
                      result.exit_code === 0 || result.passed === true
                        ? "passed"
                        : String(
                            result.status ??
                              (result.exit_code !== undefined
                                ? "failed"
                                : "not run"),
                          )
                    }
                  />
                </div>
                <pre>
                  {String(
                    result.stdout ?? result.output ?? result.detail ?? "",
                  )}
                  {result.stderr ? "\n" + String(result.stderr) : ""}
                </pre>
              </article>
            ))
          ) : (
            <Empty
              title="No verification evidence yet"
              body="Add verification commands when starting a run. Agent completion alone does not prove that a change works."
            />
          ))}
        {tab === "artifacts" &&
          (run.artifacts?.length ? (
            <div className="artifact-list">
              <button className="secondary" onClick={() => void download()}>
                <Box size={16} />
                Download changes
              </button>
              {downloadError && (
                <div className="alert" role="alert">
                  {downloadError}
                </div>
              )}
              {run.artifacts.map((artifact, index) => (
                <pre key={index}>
                  {typeof artifact === "string"
                    ? artifact
                    : JSON.stringify(artifact, null, 2)}
                </pre>
              ))}
            </div>
          ) : (
            <Empty
              title="No artifacts reported"
              body="The server lists retained outputs here when the run produces them."
            />
          ))}
      </div>
    </section>
  );
}
