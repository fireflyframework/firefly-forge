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
  BookOpen,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  ExternalLink,
  FileDiff,
  FolderGit2,
  GitBranch,
  Hammer,
  Layers3,
  Loader2,
  Monitor,
  Moon,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Square,
  Sun,
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
import { useAppearance, type Appearance } from "./appearance";
import { Wizard } from "./Wizard";
import { SourceControl } from "./SourceControl";
import { Knowledge } from "./Knowledge";
import { FactoryFloor } from "./FactoryFloor";
import { ContinueRun, ExecutionRecord } from "./ExecutionRecord";
import { DeliverySetup, RunDelivery } from "./Delivery";
import "./Integrations.css";
import {
  ProviderConfiguration,
  Workshop,
  type CodingProvider,
  type Policy,
} from "./Configuration";

type Page =
  | "overview"
  | "new"
  | "repositories"
  | "settings"
  | "run"
  | "workflow"
  | "delivery";
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
  const { appearance, setAppearance, theme } = useAppearance();
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
  const [setupStep, setSetupStep] = useState<number | null>(null);
  const [executionHealth, setExecutionHealth] = useState<{
    execution_ready?: boolean;
    detail?: string;
  } | null>(null);
  const navigation = useRef<HTMLElement>(null);
  useEffect(() => {
    const rail = navigation.current;
    if (!rail) return;
    const showCurrent = () =>
      rail
        .querySelector<HTMLElement>("button.active")
        ?.scrollIntoView({ block: "nearest", inline: "nearest" });
    showCurrent();
    const resize = new ResizeObserver(showCurrent);
    resize.observe(rail);
    return () => resize.disconnect();
  }, [page]);
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!connection.token) return;
      setLoading(true);
      setError("");
      try {
        const [
          repositoryResult,
          runResult,
          providerResult,
          profileResult,
          healthResult,
        ] = await Promise.all([
          request<Repository[]>(connection, "/repositories", { signal }),
          request<ForgeRun[]>(connection, "/runs", { signal }),
          request<unknown>(connection, "/providers", { signal }),
          request<unknown>(connection, "/profiles", { signal }),
          request<{ execution_ready?: boolean; detail?: string }>(
            connection,
            "/health",
            { signal },
          ),
        ]);
        setRepositories(repositoryResult);
        setRuns(runResult);
        setProviders(providerResult);
        setProfiles(profileResult);
        setExecutionHealth(healthResult);
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
    if (
      !connected ||
      !runs.some(
        (run) =>
          working(run.status) ||
          ["paused", "needs_reconciliation"].includes(run.status),
      )
    )
      return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [connected, runs, refresh]);
  useEffect(() => {
    if (
      page !== "run" ||
      !selected?.id ||
      !(
        working(selected.status) ||
        ["paused", "needs_reconciliation"].includes(selected.status)
      )
    )
      return;
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
    if (next === "settings") setSetupStep(null);
  }
  const repositoryName = (id: string) =>
    repositories.find((repository) => repository.id === id)?.name ?? id;
  const nav = [
    { page: "overview" as const, icon: Layers3, label: "Factory" },
    { page: "new" as const, icon: Hammer, label: "New product" },
    { page: "repositories" as const, icon: FolderGit2, label: "Repositories" },
    { page: "workflow" as const, icon: BookOpen, label: "Agents & skills" },
    { page: "delivery" as const, icon: Box, label: "Deployment" },
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
          <img src={`/assets/forge-lockup-${theme}.svg`} alt="Firefly Forge" />
        </a>
        <div className="workspace">
          <span className="workspace-mark">
            <Hammer size={17} />
          </span>
          <div>
            Software factory<small>Local workspace</small>
          </div>
        </div>
        <nav ref={navigation} aria-label="Main navigation">
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
          <a
            href="https://github.com/fireflyframework/firefly-forge"
            target="_blank"
            rel="noreferrer"
          >
            Source on GitHub
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
          <div className="topbar-actions">
            <label className="appearance-control">
              {appearance === "system" ? (
                <Monitor size={16} />
              ) : theme === "dark" ? (
                <Moon size={16} />
              ) : (
                <Sun size={16} />
              )}
              <span className="sr-only">Appearance</span>
              <select
                aria-label="Appearance"
                value={appearance}
                onChange={(event) =>
                  setAppearance(event.target.value as Appearance)
                }
              >
                <option value="system">System</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </label>
            <button
              className={"connection-indicator " + (connected ? "online" : "")}
              onClick={() => navigate("settings")}
            >
              <span />
              {connected ? "API connected" : "Connect your API"}
              <Settings2 size={14} />
            </button>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          {connected && executionHealth?.execution_ready === false && (
            <div className="alert" role="alert">
              {executionHealth.detail ||
                "Execution is unavailable. Inspect interrupted work before restarting the factory."}
            </div>
          )}
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
              <FactoryFloor
                connection={connection}
                connected={connected}
                repositories={repositories}
                runs={runs}
                theme={theme}
                onOpen={(run) => void openRun(run)}
                onCreate={() => navigate("new")}
              />
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
                <div className="integration-actions">
                  <button
                    className="primary"
                    onClick={() => {
                      navigate("settings");
                      setSetupStep(2);
                    }}
                  >
                    Connect applications
                    <ArrowRight size={17} />
                  </button>
                  <button
                    className="secondary"
                    onClick={() => setDialog(true)}
                    disabled={!connected}
                  >
                    <Plus size={17} />
                    Add repository
                  </button>
                </div>
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
                        ? "Grant repository access through your GitHub, GitLab or Bitbucket application. Local and generic Git sources can be added separately."
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
                  Repository access stays on the server. The factory creates an
                  isolated branch and workspace for each run.
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
              onUpdated={() => void refresh()}
              initialStep={setupStep}
              repositories={repositories}
              onDone={() => navigate("overview")}
              onSave={(next) => {
                setConnection(next);
                setConnected(false);
                setNotice("");
              }}
            />
          )}
          {page === "workflow" && (
            <Workshop
              connection={connection}
              connected={connected}
              repositories={repositories}
            />
          )}
          {page === "delivery" && (
            <DeliverySetup
              connection={connection}
              connected={connected}
              repositories={repositories}
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
                ) : ["failed", "cancelled"].includes(selected.status) ? (
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => void runAction("retry")}
                  >
                    <RefreshCw size={15} />
                    Retry run
                  </button>
                ) : null}
                {selected.status === "succeeded" &&
                  selected.operation !== "test" &&
                  !selected.pr_url && (
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
              <ContinueRun
                key={selected.id}
                run={selected}
                connection={connection}
                onUpdated={(run) => {
                  setSelected(run);
                  void refresh();
                }}
              />
              <RunDetail
                run={selected}
                busy={busy}
                connection={connection}
                onDelivery={() => navigate("delivery")}
              />
            </>
          )}
        </main>
        <footer>
          <span>© 2026 Firefly Software Foundation</span>
          <span className="footer-links">
            <a
              href="https://github.com/fireflyframework/firefly-forge/blob/main/LICENSE"
              target="_blank"
              rel="noreferrer"
            >
              Apache 2.0
            </a>
            <a
              href="https://fireflyframework.github.io/firefly-forge/"
              target="_blank"
              rel="noreferrer"
            >
              Documentation
            </a>
          </span>
          <span>v0.2.0</span>
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
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [brief, setBrief] = useState("");
  const [backend, setBackend] = useState("pyfly");
  const [frontend, setFrontend] = useState("react");
  const [agent, setAgent] = useState("codex");
  const [operation, setOperation] = useState("build");
  const [commands, setCommands] = useState("");
  const [repositoryId, setRepositoryId] = useState("");
  const [skillCatalog, setSkillCatalog] = useState<Policy[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [overridePolicies, setOverridePolicies] = useState(false);
  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    request<{ skills: Policy[] }>(connection, "/skills", {
      signal: controller.signal,
    })
      .then((result) =>
        setSkillCatalog(Array.isArray(result.skills) ? result.skills : []),
      )
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      });
    return () => controller.abort();
  }, [connection, connected]);
  useEffect(() => {
    if (!repositoryId) return;
    const controller = new AbortController();
    request<{ skills: string[] }>(
      connection,
      `/repositories/${repositoryId}/policies`,
      { signal: controller.signal },
    )
      .then((result) => {
        setSelectedSkills(Array.isArray(result.skills) ? result.skills : []);
        setOverridePolicies(false);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      });
    return () => controller.abort();
  }, [connection, repositoryId]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    try {
      const checks = parseCommands(commands);
      if (step < 3) {
        setStep(step + 1);
        return;
      }
      setBusy(true);
      onCreated(
        await post<ForgeRun>(connection, "/runs", {
          repository_id: repositoryId,
          brief:
            brief.trim() ||
            "Verify the current product using its executable checks.",
          operation,
          backend,
          frontend,
          agent: operation === "test" ? "codex" : agent,
          commands: checks,
          publish: false,
          ...(overridePolicies ? { skills: selectedSkills } : {}),
        }),
      );
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  const titles = [
    "Define the work",
    "Choose your foundation",
    "Set the checks",
    "Review your run",
  ];
  const descriptions = [
    "Choose the project and describe the outcome you need.",
    "Select the frameworks and coding provider for this project.",
    "Choose how the factory will verify the result and which project policies apply.",
    "Confirm the project, instructions and checks before starting. Publication is a separate action.",
  ];
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Start a run</h1>
          <p>
            Build, maintain or test a product with a recorded plan and
            verification evidence.
          </p>
        </div>
      </div>
      <Wizard
        steps={["Brief", "Foundation", "Checks", "Review"]}
        active={step}
        onStep={setStep}
        title={titles[step]}
        description={descriptions[step]}
        guide={
          <>
            <h3>
              {step === 0
                ? "Make the outcome clear"
                : step === 1
                  ? "Start with PyFly or LaraFly"
                  : step === 2
                    ? "Make success testable"
                    : "Your source stays reviewable"}
            </h3>
            <p>
              {step === 0
                ? "Tell the agent who will use this product, what they need to do, and what must stay unchanged."
                : step === 1
                  ? "These are the first supported foundations. The profile details below show what has been validated for each stack."
                  : step === 2
                    ? "Use the project's own checks. Add tests for the behavior you need, including rejected inputs and failures."
                    : "The factory records tasks, checks and a source checkpoint. You can inspect the result before publishing or deploying."}
            </p>
            <ul>
              <li>Each run has its own repository workspace.</li>
              <li>Project policies are frozen when the run is queued.</li>
              <li>Paused work retains its plan and execution history.</li>
            </ul>
          </>
        }
      >
        <form onSubmit={(event) => void submit(event)}>
          {step === 0 && (
            <div className="form-section">
              <Field label="Run purpose">
                <select
                  value={operation}
                  onChange={(event) => setOperation(event.target.value)}
                >
                  <option value="build">Build a product or feature</option>
                  <option value="maintain">Maintain an existing product</option>
                  <option value="test">Test an existing product</option>
                </select>
              </Field>
              <Field
                label="What should this change accomplish?"
                hint="Include the users, essential behavior and what success looks like."
              >
                <textarea
                  required={operation !== "test"}
                  minLength={operation === "test" ? 0 : 10}
                  rows={6}
                  value={brief}
                  onChange={(event) => setBrief(event.target.value)}
                  placeholder="Build a customer onboarding portal with an application form, document upload and a review queue…"
                />
              </Field>
              <Field label="Source repository">
                <select
                  value={repositoryId}
                  onChange={(event) => setRepositoryId(event.target.value)}
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
                    ? "Connect a repository first"
                    : "Connect your API first"}
                  <ArrowRight size={14} />
                </button>
              )}
            </div>
          )}
          {step === 1 && (
            <div className="form-section">
              <div className="two-columns">
                <Field label="Backend">
                  <select
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
              {operation !== "test" ? (
                <Field
                  label="Execution agent"
                  hint="The provider must be configured and signed in on the factory server."
                >
                  <select
                    value={agent}
                    onChange={(event) => setAgent(event.target.value)}
                  >
                    <option value="codex">Codex</option>
                    <option value="claude">Claude Code</option>
                    <option value="bridge">Firefly Agent Bridge</option>
                    <option value="agentic">Firefly Agentic</option>
                  </select>
                </Field>
              ) : (
                <p className="subtle">
                  Test runs execute checks directly. They do not use a model
                  subscription or change source.
                </p>
              )}
            </div>
          )}
          {step === 2 && (
            <div className="form-section">
              <Field
                label="Commands to run"
                hint='JSON argument arrays, for example [["uv","run","pytest"],["npm","test"]]. Each command has a 120-second limit.'
              >
                <textarea
                  rows={4}
                  value={commands}
                  onChange={(event) => setCommands(event.target.value)}
                  placeholder='[["uv", "run", "pytest"]]'
                  spellCheck={false}
                  className="code-input"
                />
              </Field>
              {!!skillCatalog.length && (
                <>
                  <h3>Project policies</h3>
                  <p className="subtle">
                    Inherit the project's versioned selection or choose policies
                    for this run. Core source and credential protections always
                    apply.
                  </p>
                  <label className="check-choice">
                    <input
                      type="checkbox"
                      checked={overridePolicies}
                      onChange={(event) =>
                        setOverridePolicies(event.target.checked)
                      }
                    />
                    Override project policies for this run
                  </label>
                  {overridePolicies && (
                    <div className="policy-grid">
                      {skillCatalog.map((skill) => (
                        <label className="policy-choice" key={skill.id}>
                          <input
                            type="checkbox"
                            checked={selectedSkills.includes(skill.id)}
                            onChange={(event) =>
                              setSelectedSkills((selected) =>
                                event.target.checked
                                  ? [...selected, skill.id]
                                  : selected.filter((id) => id !== skill.id),
                              )
                            }
                          />
                          <span>
                            <strong>{skill.name}</strong>
                            <small>{skill.description}</small>
                          </span>
                        </label>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          )}
          {step === 3 && (
            <dl className="wizard-summary">
              <div>
                <dt>Project</dt>
                <dd>
                  {repositories.find((item) => item.id === repositoryId)?.name}
                </dd>
              </div>
              <div>
                <dt>Purpose</dt>
                <dd>{human(operation)}</dd>
              </div>
              <div>
                <dt>Brief</dt>
                <dd>
                  {brief.trim() ||
                    "Verify the current product using its executable checks."}
                </dd>
              </div>
              <div>
                <dt>Foundation</dt>
                <dd>
                  {backend} / {frontend}
                </dd>
              </div>
              <div>
                <dt>Worker</dt>
                <dd>
                  {operation === "test" ? "Verification runner" : human(agent)}
                </dd>
              </div>
              <div>
                <dt>Checks</dt>
                <dd>
                  {commands.trim() ? (
                    <pre>{commands}</pre>
                  ) : (
                    "The factory will discover the project's executable checks."
                  )}
                </dd>
              </div>
              <div>
                <dt>Policies</dt>
                <dd>
                  {overridePolicies
                    ? selectedSkills.join(", ") ||
                      "No optional policies selected"
                    : "Project defaults"}
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
            {step ? (
              <button
                type="button"
                className="secondary"
                onClick={() => setStep(step - 1)}
                disabled={busy}
              >
                <ArrowLeft size={16} />
                Back
              </button>
            ) : (
              <span>Step 1 of 4</span>
            )}
            <button
              className="primary"
              disabled={busy || !connected || !repositories.length}
            >
              {busy ? (
                <Loader2 size={17} className="spin" />
              ) : step === 3 ? (
                <Play size={16} />
              ) : (
                <ArrowRight size={16} />
              )}
              {step < 2
                ? "Continue"
                : step === 2
                  ? "Review run"
                  : operation === "test"
                    ? "Run tests"
                    : operation === "maintain"
                      ? "Start maintenance"
                      : "Start building"}
            </button>
          </div>
        </form>
      </Wizard>
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
  onUpdated,
  onSave,
  repositories,
  onDone,
  initialStep,
}: {
  current: Connection;
  connected: boolean;
  providers: unknown;
  loading: boolean;
  onUpdated: () => void;
  onSave: (connection: Connection) => void;
  repositories: Repository[];
  onDone: () => void;
  initialStep: number | null;
}) {
  const { theme } = useAppearance();
  const [error, setError] = useState("");
  const [chosenStep, setStep] = useState<number | null>(initialStep);
  const [providerId, setProviderId] = useState("codex");
  const step = connected ? (chosenStep ?? 1) : 0;
  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      onSave({
        base: normalizeBase(String(data.get("base"))),
        token: String(data.get("token")).trim(),
      });
      setError("");
      setStep(null);
    } catch (cause) {
      setError(errorText(cause));
    }
  }
  const providerRecords = records(
    providers && typeof providers === "object" && "providers" in providers
      ? (providers as { providers: unknown }).providers
      : providers,
  );
  const chosenProvider =
    providerRecords.find(([, item]) => item.id === providerId) ??
    providerRecords[0];
  const apiForm = (
    <form className="settings-form" onSubmit={save}>
      <h2>Connect your factory</h2>
      <p className="subtle">
        Enter the address and operator token for your factory server.
      </p>
      <Field
        label="API address"
        hint="Use /api/v1 for this server, or a complete API address."
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
        hint="Kept in this tab's memory. Reloading clears it."
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
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      <button className="primary" disabled={loading}>
        {loading ? (
          <Loader2 size={16} className="spin" />
        ) : (
          <ArrowRight size={16} />
        )}
        Save connection
      </button>
      <p className="subtle">
        Use HTTPS when connecting to a remote server. Your token is sent to the
        address you enter.
      </p>
    </form>
  );
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Connect the factory</h1>
          <p>
            Set up the workspace, coding providers, repository access and
            project knowledge.
          </p>
        </div>
        <Badge status={connected ? "connected" : "disconnected"} />
      </div>
      {!connected ? (
        <section
          className="connection-welcome"
          aria-label="Connect your workspace"
        >
          <div className="connection-story">
            <img
              src={`/assets/forge-factory-${theme}.svg`}
              alt="Firefly software factory"
            />
            <h2>Your product starts here.</h2>
            <p>
              Connect your factory, choose a coding agent and grant repository
              access. Then follow each product from its brief through checks,
              review and deployment.
            </p>
            <div className="integration-section">
              <p>One project at a time. One recorded plan per run.</p>
            </div>
          </div>
          {apiForm}
        </section>
      ) : (
        <Wizard
          steps={["Workspace", "Coding AI", "Repositories", "Knowledge"]}
          active={step}
          onStep={setStep}
          title={
            [
              "Factory connection",
              "Choose your coding provider",
              "Grant repository access",
              "Add project knowledge",
            ][step]
          }
          description={
            [
              "Update the address or operator token for this tab.",
              "Use a local Codex or Claude Code subscription, a private bridge, or an API-backed agent.",
              "Create and authorize an application in GitHub, GitLab or Bitbucket, then choose granted repositories.",
              "Optionally connect flycanon for cited framework guidance, project source and verified run memory.",
            ][step]
          }
        >
          {step === 0 && apiForm}
          {step === 1 && (
            <div className="provider-panel">
              <div
                className="integration-provider-choice"
                aria-label="Coding provider"
              >
                {providerRecords.map(([name, item]) => (
                  <button
                    type="button"
                    key={name}
                    className="secondary"
                    aria-pressed={chosenProvider?.[0] === name}
                    onClick={() => setProviderId(String(item.id ?? name))}
                  >
                    {String(item.name ?? name)}
                  </button>
                ))}
              </div>
              {loading && !providerRecords.length ? (
                <Loading />
              ) : chosenProvider ? (
                chosenProvider[1].fields ? (
                  <ProviderConfiguration
                    key={chosenProvider[0]}
                    provider={chosenProvider[1] as unknown as CodingProvider}
                    connection={current}
                    onUpdated={onUpdated}
                  />
                ) : (
                  <div className="integration-card">
                    <h3>
                      {String(chosenProvider[1].name ?? chosenProvider[0])}
                    </h3>
                    <p>
                      {String(
                        chosenProvider[1].detail ?? "Readiness not reported",
                      )}
                    </p>
                    <Badge
                      status={
                        chosenProvider[1].ready ? "configured" : "unavailable"
                      }
                    />
                  </div>
                )
              ) : (
                <Empty
                  title="No provider details returned"
                  body="Check your factory connection and server configuration."
                />
              )}
            </div>
          )}
          {step === 2 && (
            <SourceControl connection={current} onConnected={onUpdated} />
          )}
          {step === 3 && (
            <Knowledge connection={current} repositories={repositories} />
          )}
          <div className="wizard-actions">
            {step > 0 ? (
              <button className="secondary" onClick={() => setStep(step - 1)}>
                <ArrowLeft size={16} />
                Back
              </button>
            ) : (
              <span>Factory connection is active</span>
            )}
            <button
              className="primary"
              onClick={() => (step === 3 ? onDone() : setStep(step + 1))}
            >
              {step === 3
                ? "Open factory"
                : step === 2
                  ? "Continue to knowledge"
                  : "Continue"}
              <ArrowRight size={16} />
            </button>
          </div>
        </Wizard>
      )}
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
  onDelivery,
}: {
  run: ForgeRun;
  busy: boolean;
  connection: Connection;
  onDelivery: () => void;
}) {
  const [tab, setTab] = useState<
    "timeline" | "diff" | "verification" | "artifacts" | "plan" | "deployment"
  >("timeline");
  const tabs = [
    { id: "timeline" as const, icon: Activity, label: "Timeline" },
    { id: "plan" as const, icon: BookOpen, label: "Plan" },
    { id: "diff" as const, icon: FileDiff, label: "Changes" },
    { id: "verification" as const, icon: ShieldCheck, label: "Verification" },
    { id: "artifacts" as const, icon: Box, label: "Artifacts" },
    { id: "deployment" as const, icon: Box, label: "Deployment" },
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
        {tab === "plan" && (
          <ExecutionRecord key={run.id} run={run} connection={connection} />
        )}
        {tab === "deployment" && (
          <RunDelivery
            key={run.id}
            run={run}
            connection={connection}
            onConfigure={onDelivery}
          />
        )}
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
