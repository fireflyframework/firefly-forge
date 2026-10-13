/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { version as studioVersion } from "../package.json";
import { repositoryLabels } from "./RepositoryLabels";
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
  Unplug,
  X,
} from "lucide-react";
import {
  ApiError,
  normalizeBase,
  parseCommands,
  post,
  request,
  type Connection,
  type ForgeRun,
  type Repository,
  type ProductSnapshot,
  type ComponentRunContext,
  type ProjectSnapshot,
} from "./api";
import { useAppearance, type Appearance } from "./appearance";
import { Wizard } from "./Wizard";
import { SourceControl } from "./SourceControl";
import { KnowledgeExplorer, Knowledge } from "./Knowledge";
import { FactoryFloor } from "./FactoryFloor";
import { ContinueRun, ExecutionRecord } from "./ExecutionRecord";
import { EvidenceView, UnifiedDiff } from "./ExecutionPresentation";
import { DeliverySetup, RunDelivery } from "./Delivery";
import { Operations } from "./Operations";
import { Projects } from "./Projects";
import { MobileNavigation } from "./MobileNavigation";
import { FactoryWelcome } from "./FactoryWelcome";
import { ProductDefinition } from "./ProductDefinition";
import { IdentityMenu, SignIn, useIdentity } from "./Identity";
import { ProjectAccess } from "./ProjectAccess";
import { UserAdministration } from "./UserAdministration";
import { Administration } from "./Administration";
import { SearchConfiguration } from "./SearchConfiguration";
import { Collaboration } from "./Collaboration";
import "./Integrations.css";
import "./UsabilityRefinements.css";
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
  | "delivery"
  | "operations"
  | "product"
  | "projects"
  | "knowledge"
  | "collaboration"
  | "access"
  | "admin"
  | "users"
  | "search";
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
const deliveryPending = (run: ForgeRun) =>
  ["queued", "executing"].includes(run.publication_request?.status ?? "") ||
  run.cancel_request?.status === "requested";
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
  const [runProject, setRunProject] = useState("");
  const [runComponent, setRunComponent] = useState<ComponentRunContext | null>(
    null,
  );
  const [productProject, setProductProject] = useState<ProjectSnapshot | null>(
    null,
  );
  const [floorProject, setFloorProject] = useState("");
  const [accessUser, setAccessUser] = useState<string>();
  const [connection, setConnection] = useState<Connection>({
    base: "/api/v1",
    token: "",
  });
  const identity = useIdentity(connection.base);
  const restoredGoal = useRef(false);
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
  const [setupGoal, setSetupGoal] = useState<"overview" | "product">(
    "overview",
  );
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
  useEffect(() => {
    document
      .querySelector<HTMLElement>(".workspace-main > main")
      ?.scrollTo({ top: 0 });
  }, [page]);
  useEffect(() => {
    if (identity.loading) return;
    const authenticated = Boolean(identity.session);
    setConnection((current) =>
      current.authenticated === authenticated && !current.token
        ? current
        : { ...current, token: "", authenticated },
    );
    if (!authenticated) {
      setConnected(false);
      setRepositories([]);
      setRuns([]);
      setSelected(null);
      setProviders(null);
      setProfiles(null);
    } else if (!restoredGoal.current) {
      restoredGoal.current = true;
      const goal = sessionStorage.getItem("forge.pending-goal");
      sessionStorage.removeItem("forge.pending-goal");
      if (goal === "/product") {
        setSetupGoal("product");
        setPage("product");
      }
    }
  }, [identity.session, identity.loading]);
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!connection.authenticated && !connection.token) return;
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
          request<unknown>(connection, "/providers", { signal }).catch(
            (cause) => {
              if (cause instanceof ApiError && cause.status === 403)
                return null;
              throw cause;
            },
          ),
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
          deliveryPending(run) ||
          ["paused", "needs_reconciliation"].includes(run.status),
      )
    )
      return;
    const timer = window.setInterval(() => {
      void refresh();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [connected, runs, refresh]);
  const selectedDeliveryPending = selected ? deliveryPending(selected) : false;
  useEffect(() => {
    if (
      page !== "run" ||
      !selected?.id ||
      !(
        working(selected.status) ||
        selectedDeliveryPending ||
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
  }, [
    page,
    selected?.id,
    selected?.status,
    selectedDeliveryPending,
    connection,
  ]);
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
            ? result.pr_url
              ? "Pull request publication completed."
              : "Publication request recorded."
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
    if (next === "new") setRunComponent(null);
    if (next === "product") setProductProject(null);
    if (next === "settings") setSetupStep(null);
  }
  const repositoryName = (id: string) =>
    repositories.find((repository) => repository.id === id)?.name ?? id;
  const projectRuns = runs.filter((run) => run.repository_id === floorProject);
  const nav = [
    { page: "overview" as const, icon: Layers3, label: "Factory" },
    { page: "new" as const, icon: Hammer, label: "Start a run" },
    { page: "projects" as const, icon: Box, label: "Product projects" },
    { page: "product" as const, icon: Layers3, label: "Product definition" },
    { page: "repositories" as const, icon: FolderGit2, label: "Repositories" },
    { page: "delivery" as const, icon: Box, label: "Deployment" },
    { page: "operations" as const, icon: Activity, label: "Applications" },
    { page: "knowledge" as const, icon: BookOpen, label: "Knowledge" },
    { page: "admin" as const, icon: Settings2, label: "Administration" },
  ];
  const administrationLabels: Partial<Record<Page, string>> = {
    settings: "Connections",
    workflow: "Agents & skills",
    collaboration: "Repository workflow",
    access: "Project access",
    users: "People",
    search: "Internet search",
  };
  const navigationPage = administrationLabels[page]
    ? "admin"
    : page === "run"
      ? "overview"
      : page;
  const pageLabel =
    page === "run"
      ? "Run details"
      : (administrationLabels[page] ??
        nav.find((item) => item.page === page)?.label);
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
        <MobileNavigation
          items={nav.map((item) => ({
            id: item.page,
            label: item.label,
            icon: item.icon,
            ...(item.page === "repositories"
              ? { count: repositories.length }
              : {}),
          }))}
          current={navigationPage}
          onNavigate={(id) => navigate(id as Page)}
        />
        <div className="workspace">
          <span className="workspace-mark">
            <Hammer size={17} />
          </span>
          <div>
            Software factory<small>Project workspace</small>
          </div>
        </div>
        <nav ref={navigation} aria-label="Main navigation">
          {nav.map((item) => (
            <button
              key={item.page}
              className={navigationPage === item.page ? "active" : ""}
              onClick={() => navigate(item.page)}
              aria-current={navigationPage === item.page ? "page" : undefined}
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
            <strong>{pageLabel}</strong>
          </div>
          <span className="mobile-section-label">{pageLabel}</span>
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
              aria-label={connected ? "API connected" : "Connect your API"}
              onClick={() => navigate("settings")}
            >
              <span />
              <b className="connection-desktop-label">
                {connected ? "API connected" : "Connect your API"}
              </b>
              <b className="connection-mobile-label" aria-hidden="true">
                {connected ? "Connected" : "Connect API"}
              </b>
              <Settings2 size={14} />
            </button>
            {identity.session && (
              <IdentityMenu
                connection={connection}
                session={identity.session}
                onAccess={() => navigate("access")}
                onSignedOut={() => {
                  restoredGoal.current = false;
                  identity.refresh();
                  setConnected(false);
                  setNotice("");
                }}
              />
            )}
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
          {page === "overview" && !connected && (
            <FactoryWelcome
              theme={theme}
              onConnect={() => {
                setSetupGoal("overview");
                navigate("settings");
              }}
              onProduct={() => {
                setSetupGoal("product");
                navigate("settings");
              }}
            />
          )}
          {page === "overview" && connected && (
            <>
              <div className="page-heading">
                <div>
                  <h1>Your software factory.</h1>
                  <p>
                    Give an idea a brief. Follow the work. Review the evidence.
                  </p>
                </div>
                <button className="primary" onClick={() => navigate("product")}>
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
                onCreate={() => {
                  setRunProject(floorProject);
                  navigate("new");
                }}
                onProjectChange={setFloorProject}
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
                  <span>Factory active runs</span>
                  <strong>
                    {runs
                      .filter((run) => working(run.status))
                      .length.toString()
                      .padStart(2, "0")}
                  </strong>
                  <Activity size={20} />
                </div>
                <div>
                  <span>Factory completed runs</span>
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
                    Project runs <span>{projectRuns.length}</span>
                  </h2>
                  <button
                    className="quiet"
                    onClick={() => void refresh()}
                    disabled={!connected || loading}
                  >
                    <RefreshCw size={15} className={loading ? "spin" : ""} />
                    Refresh
                  </button>
                </div>
                {loading && !projectRuns.length ? (
                  <Loading />
                ) : projectRuns.length ? (
                  <div className="run-list">
                    {projectRuns.map((run) => (
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
          {page === "projects" && (
            <Projects
              connection={connection}
              connected={connected}
              repositories={repositories}
              onOpen={(run) => void openRun(run)}
              onProduct={(id, project) => {
                setRunProject(id);
                navigate("product");
                setProductProject(project);
              }}
              onStart={(context) => {
                setRunProject(context.component.repository_id);
                navigate("new");
                setRunComponent(context);
              }}
            />
          )}
          {page === "product" && (
            <ProductDefinition
              logicalProject={productProject}
              onConfigure={() => {
                navigate("settings");
                setSetupStep(1);
              }}
              connection={connection}
              connected={connected}
              repositories={repositories}
              initialRepository={runProject}
              onStart={(id) => {
                setRunProject(id);
                navigate("new");
              }}
            />
          )}
          {page === "knowledge" && (
            <KnowledgeExplorer
              onConfigure={() => {
                navigate("settings");
                setSetupStep(3);
              }}
              connection={connection}
              connected={connected}
              repositories={repositories}
            />
          )}
          {page === "collaboration" && (
            <Collaboration
              onConfigure={() => {
                navigate("settings");
                setSetupStep(2);
              }}
              connection={connection}
              connected={connected}
              repositories={repositories}
              onOpen={(run) => void openRun(run)}
            />
          )}
          {page === "new" && (
            <NewProduct
              key={
                runComponent
                  ? `${runComponent.project.id}/${runComponent.component.id}`
                  : "standalone"
              }
              initialComponent={runComponent}
              onStandalone={() => setRunComponent(null)}
              initialRepository={runProject}
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
                      <button
                        className="quiet"
                        onClick={() => {
                          setRunProject(repository.id);
                          navigate("new");
                        }}
                      >
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
              onDone={() => navigate(setupGoal)}
              doneLabel={
                setupGoal === "product"
                  ? "Open product definition"
                  : "Open production floor"
              }
              signin={
                <SignIn
                  connection={connection}
                  configuration={identity.configuration}
                  loading={identity.loading}
                  configurationError={identity.error}
                  pendingGoal={setupGoal}
                  onRefresh={identity.refresh}
                  onReady={identity.refresh}
                  onAddress={(base) => {
                    setConnection({ base, token: "", authenticated: false });
                    setConnected(false);
                  }}
                />
              }
            />
          )}
          {page === "admin" && (
            <Administration session={identity.session} onNavigate={navigate} />
          )}
          {page === "users" && (
            <UserAdministration
              connection={connection}
              session={identity.session}
              onConfigure={() => navigate("settings")}
              onAssign={(id) => {
                setAccessUser(id);
                navigate("access");
              }}
            />
          )}
          {page === "search" && (
            <SearchConfiguration
              connection={connection}
              session={identity.session}
            />
          )}
          {page === "access" && (
            <ProjectAccess
              connection={connection}
              session={identity.session}
              initialUser={accessUser}
              onConfigure={() => navigate("settings")}
              onUpdated={() => {
                setAccessUser(undefined);
                identity.refresh();
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
          {page === "operations" && (
            <Operations
              connection={connection}
              connected={connected}
              repositories={repositories}
              providers={providers}
              onOpen={(run) => void openRun(run)}
              onConfigure={() => navigate("delivery")}
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
                    {selected.operation === "test"
                      ? "Verify existing behavior"
                      : selected.brief?.split("\n")[0]?.length > 96
                        ? `${selected.brief
                            .split("\n")[0]
                            .slice(0, 96)
                            .replace(/\s+\S*$/, "")}…`
                        : selected.brief?.split("\n")[0] ||
                          "Software generation"}
                  </h1>
                  <p>
                    {repositoryName(selected.repository_id)} ·{" "}
                    {selected.backend} / {selected.frontend} ·{" "}
                    {selected.operation === "test"
                      ? "No coding provider · checks only"
                      : selected.agent}
                  </p>
                </div>
                <Badge status={selected.status} />
              </div>
              <details className="run-brief-details">
                <summary>Full run brief</summary>
                <p>{selected.brief}</p>
              </details>
              <div className="run-controls">
                <span>
                  <GitBranch size={16} />
                  Isolated change
                </span>
                {working(selected.status) ? (
                  <button
                    className="secondary"
                    disabled={
                      busy || selected.cancel_request?.status === "requested"
                    }
                    onClick={() => void runAction("cancel")}
                  >
                    <Square size={14} />
                    {selected.cancel_request?.status === "requested"
                      ? "Cancellation requested"
                      : "Cancel run"}
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
                      disabled={
                        busy ||
                        [
                          "queued",
                          "executing",
                          "needs_reconciliation",
                        ].includes(selected.publication_request?.status ?? "")
                      }
                      onClick={() => void runAction("publish")}
                    >
                      <GitBranch size={15} />
                      {selected.publication_request?.status === "queued"
                        ? "Publication queued"
                        : selected.publication_request?.status === "executing"
                          ? "Publishing pull request"
                          : selected.publication_request?.status ===
                              "needs_reconciliation"
                            ? "Publication needs review"
                            : "Publish pull request"}
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
          <span>v{studioVersion}</span>
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
  initialComponent,
  onStandalone,
  initialRepository,
  repositories,
  profiles,
  connected,
  connection,
  onRepository,
  onConnection,
  onCreated,
}: {
  initialComponent: ComponentRunContext | null;
  onStandalone: () => void;
  initialRepository: string;
  repositories: Repository[];
  profiles: unknown;
  connected: boolean;
  connection: Connection;
  onRepository: () => void;
  onConnection: () => void;
  onCreated: (run: ForgeRun) => void;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [brief, setBrief] = useState("");
  const [backend, setBackend] = useState(
    initialComponent?.component.backend ?? "pyfly",
  );
  const [frontend, setFrontend] = useState(
    initialComponent?.component.frontend ?? "react",
  );
  const [agent, setAgent] = useState("codex");
  const [operation, setOperation] = useState("build");
  const [commands, setCommands] = useState(
    initialComponent
      ? JSON.stringify(
          initialComponent.component.verification.map((item) => item.argv),
        )
      : "",
  );
  const [repositoryId, setRepositoryId] = useState(initialRepository);
  const productRepositoryId =
    initialComponent?.project.definition.product_repository_id || repositoryId;
  const [product, setProduct] = useState<ProductSnapshot | null>(null);
  const [productLoaded, setProductLoaded] = useState("");
  useEffect(() => {
    if (!productRepositoryId) return;
    const controller = new AbortController();
    request<ProductSnapshot | null>(
      connection,
      `/repositories/${productRepositoryId}/product`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (controller.signal.aborted) return;
        setProduct(
          value &&
            typeof value.revision === "number" &&
            value.repository_id === productRepositoryId &&
            value.definition
            ? value
            : null,
        );
        if (
          !initialComponent &&
          value?.repository_id === productRepositoryId &&
          value.definition.architecture
        ) {
          setBackend(value.definition.architecture.backend);
          setFrontend(value.definition.architecture.frontend);
        }
        setProductLoaded(productRepositoryId);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      });
    return () => controller.abort();
  }, [connection, productRepositoryId, initialComponent]);
  const [skillCatalog, setSkillCatalog] = useState<Policy[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [overridePolicies, setOverridePolicies] = useState(false);
  const [reviewAgent, setReviewAgent] = useState("");
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
  const frozenArchitecture =
    !initialComponent && productLoaded === productRepositoryId
      ? product?.definition.architecture
      : null;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    try {
      const checks = parseCommands(commands);
      if (step === 1 && frozenArchitecture && !frozenArchitecture.reviewed)
        throw new Error(
          "Review this product’s saved architecture in Product definition before starting a run.",
        );
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
          ...(initialComponent
            ? {
                project_id: initialComponent.project.id,
                component_id: initialComponent.component.id,
              }
            : { commands: checks }),
          publish: false,
          ...(!initialComponent &&
          productLoaded === productRepositoryId &&
          product
            ? { product_definition: product }
            : {}),
          ...(overridePolicies ? { skills: selectedSkills } : {}),
          ...(reviewAgent ? { assurance: { review_agent: reviewAgent } } : {}),
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
    operation === "test"
      ? "Choose the existing source and the checks you want to execute. No coding model changes the product."
      : "Choose the project and describe the outcome you need.",
    operation === "test"
      ? "Match the current repository’s frameworks. Test runs execute commands without a coding provider."
      : "Select the frameworks and coding provider for this project.",
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
        locked={busy}
        className="run-wizard"
        steps={["Brief", "Foundation", "Checks", "Review"]}
        active={step}
        onStep={setStep}
        title={titles[step]}
        description={descriptions[step]}
        guide={
          <>
            <h3>
              {step === 0
                ? operation === "test"
                  ? "Check existing behavior"
                  : "Make the outcome clear"
                : step === 1
                  ? "Start with PyFly or LaraFly"
                  : step === 2
                    ? "Make success testable"
                    : "Your source stays reviewable"}
            </h3>
            <p>
              {step === 0
                ? operation === "test"
                  ? "Choose the repository you want to verify. Add its real test commands in Checks; the factory records their outcomes without changing source."
                  : "Tell the agent who will use this product, what they need to do, and what must stay unchanged."
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
                  rows={3}
                  className="run-brief"
                  value={brief}
                  onChange={(event) => setBrief(event.target.value)}
                  placeholder="Build a customer onboarding portal with an application form, document upload and a review queue…"
                />
              </Field>
              {initialComponent && (
                <section className="integration-section">
                  <strong>
                    {initialComponent.project.definition.name} /{" "}
                    {initialComponent.component.name}
                  </strong>
                  <p>
                    Revision {initialComponent.project.revision} · root{" "}
                    <code>{initialComponent.component.path}</code>. This run
                    freezes the configured component stack, checks and shared
                    product definition/design system.
                  </p>
                  <button
                    type="button"
                    className="quiet"
                    onClick={onStandalone}
                  >
                    Use a standalone repository run
                  </button>
                </section>
              )}
              <Field label="Source repository">
                <select
                  value={repositoryId}
                  onChange={(event) => setRepositoryId(event.target.value)}
                  required
                  disabled={!repositories.length || Boolean(initialComponent)}
                >
                  <option value="">Choose a repository</option>
                  {repositories.map((repository) => (
                    <option key={repository.id} value={repository.id}>
                      {projectLabels.get(repository.id)}
                    </option>
                  ))}
                </select>
              </Field>
              {productLoaded === productRepositoryId && product && (
                <p className="feature-status">
                  Product definition revision {product.revision} will be frozen
                  into this run.
                </p>
              )}
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
              {frozenArchitecture && (
                <p className="integration-status">
                  Saved product revision {product?.revision} fixes the
                  foundation to {frozenArchitecture.backend} /{" "}
                  {frozenArchitecture.frontend}. To change this stack, edit the
                  architecture in Product definition.
                </p>
              )}
              <div className="two-columns">
                <Field label="Backend">
                  <select
                    value={backend}
                    disabled={Boolean(initialComponent || frozenArchitecture)}
                    onChange={(event) => setBackend(event.target.value)}
                  >
                    {backend === "none" && (
                      <option value="none">
                        No backend · standalone frontend
                      </option>
                    )}
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
                    disabled={Boolean(initialComponent || frozenArchitecture)}
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
                  disabled={Boolean(initialComponent)}
                  onChange={(event) => setCommands(event.target.value)}
                  placeholder='[["uv", "run", "pytest"]]'
                  spellCheck={false}
                  className="code-input"
                />
              </Field>
              <Field
                label="Independent review provider"
                hint="Optional. Review runs in a separate copied workspace and blocks delivery until approved. Provider credentials must be configured on the factory."
              >
                <select
                  value={reviewAgent}
                  onChange={(event) => setReviewAgent(event.target.value)}
                >
                  <option value="">No independent review requested</option>
                  <option value="codex">Codex</option>
                  <option value="claude">Claude Code</option>
                  <option value="bridge">Firefly Agent Bridge</option>
                  <option value="agentic">Firefly Agentic</option>
                </select>
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
                <dt>Product definition</dt>
                <dd>
                  {productLoaded === productRepositoryId
                    ? product
                      ? `Saved revision ${product.revision} · ${product.definition.customer}`
                      : "No saved definition; this run uses the brief."
                    : "Definition not loaded; the factory will resolve project defaults."}
                </dd>
              </div>
              <div>
                <dt>Independent review</dt>
                <dd>
                  {reviewAgent
                    ? `Requested from ${human(reviewAgent)} in a separate workspace`
                    : "Not requested"}
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
  signin,
  repositories,
  onDone,
  initialStep,
  doneLabel = "Open production floor",
}: {
  doneLabel?: string;
  current: Connection;
  connected: boolean;
  providers: unknown;
  loading: boolean;
  onUpdated: () => void;
  signin: ReactNode;
  repositories: Repository[];
  onDone: () => void;
  initialStep: number | null;
}) {
  const { theme } = useAppearance();
  const [chosenStep, setStep] = useState<number | null>(initialStep);
  const [providerId, setProviderId] = useState("codex");
  const step = connected ? (chosenStep ?? 1) : 0;
  const providerRecords = records(
    providers && typeof providers === "object" && "providers" in providers
      ? (providers as { providers: unknown }).providers
      : providers,
  );
  const chosenProvider =
    providerRecords.find(([, item]) => item.id === providerId) ??
    providerRecords[0];
  const apiForm = signin;
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
            <h2>After you connect</h2>
            <p>
              Connect your factory, choose a coding agent and grant repository
              access. Then follow each product from its brief through checks,
              review and deployment.
            </p>
            <div className="integration-section">
              <p>
                Each project keeps its source, configuration and recorded work.
                Independent repositories can run in parallel.
              </p>
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
              "Your server session determines identity and project access. Change servers or sign in explicitly.",
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
                ? doneLabel
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
    | "timeline"
    | "diff"
    | "verification"
    | "artifacts"
    | "plan"
    | "deployment"
    | "review"
  >("timeline");
  const tabs = [
    { id: "timeline" as const, icon: Activity, label: "Timeline" },
    { id: "plan" as const, icon: BookOpen, label: "Plan" },
    { id: "diff" as const, icon: FileDiff, label: "Changes" },
    { id: "verification" as const, icon: ShieldCheck, label: "Verification" },
    { id: "review" as const, icon: ShieldCheck, label: "Independent review" },
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
        {
          credentials: "include",
          headers: connection.token
            ? { Authorization: "Bearer " + connection.token }
            : {},
        },
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
        {tab === "review" && (
          <section>
            <h3>Independent review</h3>
            {!run.review || run.review.status === "not_requested" ? (
              <p>No independent review was requested for this run.</p>
            ) : (
              <>
                <span className="feature-status">
                  {run.review.status.replaceAll("_", " ")}
                </span>
                <p>{run.review.summary}</p>
                <p className="feature-meta">
                  Reviewed source tree <code>{run.review.source_tree}</code>
                </p>
                {run.review.findings.map((finding, index) => (
                  <article key={index} className="feature-record">
                    <strong>
                      {finding.severity} · {finding.summary}
                    </strong>
                    {finding.path && (
                      <p>
                        <code>
                          {finding.path}
                          {finding.line ? `:${finding.line}` : ""}
                        </code>
                      </p>
                    )}
                    <p>{finding.evidence}</p>
                  </article>
                ))}
              </>
            )}
          </section>
        )}
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
                    <EvidenceView
                      value={event}
                      omitFields={[
                        "message",
                        "type",
                        "event",
                        "timestamp",
                        "at",
                        "created_at",
                      ]}
                    />
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
              <UnifiedDiff value={run.diff} />
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
              <EvidenceView
                key={name}
                value={result}
                title={
                  Array.isArray(result.argv) || result.command
                    ? undefined
                    : name
                }
              />
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
                <EvidenceView key={index} value={artifact} />
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
