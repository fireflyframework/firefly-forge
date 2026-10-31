/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Download, FileSearch, RefreshCw } from "lucide-react";
import {
  request,
  ApiError,
  type Connection,
  type ProjectSnapshot,
  type ProjectComponent,
  type Repository,
} from "./api";
import { Wizard } from "./Wizard";
import {
  SourceComponentSelector,
  type SourceComponentFilters,
} from "./SourceComponentSelector";
import "./SourceAnalysis.css";
import { CodeSnippet } from "./CodeSnippet";
import {
  reviewedSourcePayload,
  type ReviewedSourceContext,
} from "./SourceAnalysisContext";
type Fact = {
  kind: string;
  value: string;
  confidence: "observed";
  source: {
    path: string;
    line: number;
    excerpt: string;
    blob: string;
    commit: string;
  };
};
type Report = {
  id: string;
  project_id: string;
  status: string;
  reviewed: boolean;
  method: "source_inspection";
  digest: string;
  components: {
    id: string;
    name: string;
    repository_id: string;
    path: string;
    commit: string;
    facts: Fact[];
    skipped_files: number;
    execution: "source_only";
  }[];
  documents: Record<string, string>;
  open_questions: string[];
  created_at: string;
  functional_definition: null;
  project_snapshot?: ProjectSnapshot;
};
const failure = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "Source inspection could not be completed.";
const sourceBinding = (item: ProjectComponent) =>
  JSON.stringify({
    repository_id: item.repository_id,
    path: item.path,
    kind: item.kind,
    backend: item.backend,
    frontend: item.frontend,
    verification: item.verification,
    depends_on: item.depends_on,
  });
export function SourceAnalysis({
  connection,
  project,
  repositories,
  onOpen,
  onClose,
  initialStep = 0,
  onRefresh,
  onContinueProduct,
}: {
  connection: Connection;
  project: ProjectSnapshot;
  repositories: Repository[];
  onOpen?: (step: number) => void;
  onClose?: () => void;
  initialStep?: number;
  onRefresh?: () => void;
  onContinueProduct?: (context: ReviewedSourceContext) => void;
}) {
  const [reports, setReports] = useState<Report[]>([]),
    [selected, setSelected] = useState(""),
    [report, setReport] = useState<Report | null>(null),
    [components, setComponents] = useState(
      project.definition.components.map((item) => item.id),
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [readError, setReadError] = useState(""),
    [revision, setRevision] = useState(0),
    [acknowledged, setAcknowledged] = useState(false);
  const [step, setStep] = useState(initialStep);
  const available = useMemo(
    () => new Map(project.definition.components.map((item) => [item.id, item])),
    [project.definition.components],
  );
  const [reviewedBindings, setReviewedBindings] = useState(
    () =>
      new Map(
        project.definition.components.map((item) => [
          item.id,
          sourceBinding(item),
        ]),
      ),
  );
  const [failedInspection, setFailedInspection] = useState(false);
  const [rejectedSnapshot, setRejectedSnapshot] = useState<{
    revision: number;
    digest: string;
  } | null>(null);
  const waitingForSnapshot =
    rejectedSnapshot?.revision === project.revision &&
    rejectedSnapshot.digest === project.digest;
  const selectedComponentIds = components.filter((id) => available.has(id));
  const selectedIds = new Set(selectedComponentIds);
  const changedComponents = project.definition.components.filter(
    (item) =>
      selectedIds.has(item.id) &&
      reviewedBindings.get(item.id) !== sourceBinding(item),
  );
  const scopeHeld = failedInspection || changedComponents.length > 0;
  useEffect(() => {
    setComponents((values) => {
      const remaining = values.filter((id) => available.has(id));
      return remaining.length === values.length ? values : remaining;
    });
  }, [available]);
  function selectComponents(ids: string[]) {
    const previous = new Set(components);
    setComponents(ids);
    setReviewedBindings((values) => {
      const next = new Map(values);
      ids
        .filter((id) => !previous.has(id))
        .forEach((id) => {
          const item = available.get(id);
          if (item) next.set(id, sourceBinding(item));
        });
      return next;
    });
  }
  const [componentFilters, setComponentFilters] =
    useState<SourceComponentFilters>({
      query: "",
      kind: "",
      repository: "",
      selectedOnly: false,
      page: 0,
    });
  useEffect(() => {
    setComponentFilters((values) => {
      const kind =
        !values.kind ||
        project.definition.components.some((item) => item.kind === values.kind)
          ? values.kind
          : "";
      const repository =
        !values.repository ||
        project.definition.components.some(
          (item) => item.repository_id === values.repository,
        )
          ? values.repository
          : "";
      return kind === values.kind && repository === values.repository
        ? values
        : { ...values, kind, repository, page: 0 };
    });
  }, [project.definition.components]);
  const pageHeading = useRef<HTMLHeadingElement>(null);
  const pageMode = Boolean(onClose);
  useEffect(() => {
    if (pageMode) {
      pageHeading.current?.focus({ preventScroll: true });
      const main = pageHeading.current?.closest("main");
      if (main) main.scrollTop = 0;
    }
  }, [pageMode]);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    const refresh = () =>
      request<Report[]>(connection, `/projects/${project.id}/analyses`, {
        signal: controller.signal,
      })
        .then((items) => {
          if (!controller.signal.aborted) {
            setReports(items);
            setReadError("");
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setReadError(failure(cause));
        });
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [connection, project.id, revision]);
  const id = reports.some((item) => item.id === selected)
    ? selected
    : reports[0]?.id;
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    request<Report>(connection, `/projects/${project.id}/analyses/${id}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) {
          setReport(value);
          setAcknowledged(false);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setReadError(failure(cause));
      });
    return () => controller.abort();
  }, [connection, project.id, id, revision]);
  async function analyze() {
    if (busy || !selectedComponentIds.length || scopeHeld) return;
    const controller = new AbortController();
    pending.current?.abort();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await request<Report>(
        connection,
        `/projects/${project.id}/analyses`,
        {
          method: "POST",
          body: JSON.stringify({
            component_ids: selectedComponentIds,
            expected_project_revision: project.revision,
            expected_project_digest: project.digest,
          }),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setReports((items) => [
        value,
        ...items.filter((item) => item.id !== value.id),
      ]);
      setSelected(value.id);
      setReport(value);
      setStep(1);
      setRevision((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(failure(cause));
        setFailedInspection(true);
        if (
          cause instanceof ApiError &&
          cause.status === 409 &&
          cause.message.includes(
            "Project changed; review component scope again",
          )
        )
          setRejectedSnapshot({
            revision: project.revision,
            digest: project.digest,
          });
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function review() {
    if (!report || !acknowledged) return;
    const controller = new AbortController();
    pending.current?.abort();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await request<Report>(
        connection,
        `/projects/${project.id}/analyses/${report.id}/review`,
        {
          method: "POST",
          body: JSON.stringify({ digest: report.digest, reviewed: true }),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setReport(value);
      setRevision((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) setError(failure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function download(name: string, contents: string) {
    const url = URL.createObjectURL(
      new Blob([contents], {
        type: name.endsWith(".json")
          ? "application/json"
          : "text/markdown;charset=utf-8",
      }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  const current = report && report.id === id ? report : null;
  const reviewedContext = useMemo(() => {
    if (
      !current?.reviewed ||
      current.project_snapshot?.digest !== project.digest
    )
      return null;
    try {
      return reviewedSourcePayload(
        {
          project_id: current.project_id,
          analysis_id: current.id,
          analysis_digest: current.digest,
          analysis_component_ids: current.components.map((item) => item.id),
        },
        project.id,
      );
    } catch {
      return null;
    }
  }, [current, project.id, project.digest]);
  const declarations =
    current?.components.reduce((total, item) => total + item.facts.length, 0) ??
    0;
  if (!onClose)
    return (
      <section className="source-analysis feature-record">
        <header>
          <div>
            <h2>
              <FileSearch size={24} />
              Source AS-IS
            </h2>
            <p>
              Review declared routes, tests and dependencies at their exact
              source commits. Inspection reads source without executing the
              application.
            </p>
          </div>
          <button
            className="secondary"
            disabled={!onOpen}
            onClick={() => onOpen?.(reports.length ? 1 : 0)}
          >
            Open source inspection
          </button>
        </header>
        <p className="feature-meta">
          {reports.length
            ? `${reports.length} recorded inspection${reports.length === 1 ? "" : "s"} · ${reports[0].status.replaceAll("_", " ")}`
            : "No source inspection recorded."}
        </p>
        {(error || readError) && (
          <div className="alert" role="alert">
            {error || readError}
          </div>
        )}
      </section>
    );
  return (
    <section className="source-inspection-page" aria-label="Source inspection">
      <button
        className="text-button source-inspection-back"
        onClick={onClose}
        disabled={busy}
      >
        <ArrowLeft size={16} aria-hidden="true" />
        Back to project
      </button>
      <header className="page-heading">
        <div>
          <h1 ref={pageHeading} tabIndex={-1}>
            Source inspection
          </h1>
          <p>
            {project.definition.name} · revision {project.revision}
          </p>
        </div>
        {onRefresh && (
          <button
            type="button"
            className="secondary source-inspection-refresh"
            disabled={busy}
            onClick={onRefresh}
            aria-label="Refresh project scope"
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>Refresh project scope</span>
          </button>
        )}
      </header>
      <p className="source-inspection-purpose">
        Inspect selected source without running the app or changing code.
      </p>
      <details className="source-inspection-explanation">
        <summary>What this inspection produces</summary>
        <p>
          Choose source roots for an AS-IS report of routes, dependencies and
          test declarations. Review the recorded evidence, then use the reviewed
          report as context for product definition. This reads files without
          executing the application or changing code.
        </p>
      </details>
      <Wizard
        variant="rail"
        layout="flow"
        progressMode="visited"
        stepDescriptions={[
          "Choose component roots",
          "Recorded source report",
          "Declarations and commits",
          "Confirm source review",
        ]}
        steps={["Scope", "Report", "Source findings", "Review"]}
        active={step}
        onStep={setStep}
        locked={busy}
        title={
          [
            "Choose source roots",
            "Recorded inspection",
            "Source findings",
            "Review the source evidence",
          ][step]
        }
        description={
          [
            "",
            "Select an actual recorded report before reviewing its declarations.",
            "Each declaration retains its file, line, blob and source commit.",
            "Review the findings and open questions before using this report to define the product.",
          ][step]
        }
      >
        <form onSubmit={(event) => event.preventDefault()}>
          {(error || readError) && (
            <div className="alert" role="alert">
              {error || readError}
            </div>
          )}
          {step === 0 && (
            <>
              {scopeHeld && (
                <div className="source-scope-review" role="alert">
                  <h3>Review current source scope</h3>
                  <p>
                    {failedInspection
                      ? "The inspection was not confirmed. Review the current scope and recorded reports before trying again."
                      : `${changedComponents.length} selected source contract${changedComponents.length === 1 ? "" : "s"} changed. Review the current roots, repositories, stack, checks and dependencies before continuing.`}
                  </p>
                  {waitingForSnapshot && (
                    <p>
                      Refresh the project snapshot before confirming its current
                      source scope.
                    </p>
                  )}
                  <div className="feature-actions">
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() => setStep(1)}
                    >
                      Read recorded reports
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy || waitingForSnapshot}
                      onClick={() => {
                        setReviewedBindings(
                          new Map(
                            project.definition.components.map((item) => [
                              item.id,
                              sourceBinding(item),
                            ]),
                          ),
                        );
                        setFailedInspection(false);
                        setRejectedSnapshot(null);
                        setError("");
                      }}
                    >
                      Confirm current source scope
                    </button>
                  </div>
                </div>
              )}
              <SourceComponentSelector
                components={project.definition.components}
                repositories={repositories}
                selected={selectedComponentIds}
                onSelectedChange={selectComponents}
                filters={componentFilters}
                onFiltersChange={setComponentFilters}
                disabled={busy}
                changedIds={changedComponents.map((item) => item.id)}
              />
            </>
          )}
          {step === 1 && (
            <>
              <div className="field">
                <label htmlFor="source-analysis-report">
                  Inspection report
                </label>
                <select
                  id="source-analysis-report"
                  value={id ?? ""}
                  disabled={busy || !reports.length}
                  onChange={(event) => {
                    setSelected(event.target.value);
                    setReport(null);
                  }}
                >
                  {reports.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.id.slice(0, 8)} · {item.status.replaceAll("_", " ")}{" "}
                      · {new Date(item.created_at).toLocaleString()}
                    </option>
                  ))}
                </select>
              </div>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setRevision((value) => value + 1)}
              >
                <RefreshCw size={18} />
                Refresh reports
              </button>
              {current ? (
                <>
                  <p className="feature-status">
                    {current.status.replaceAll("_", " ")} · source inspection
                  </p>
                  <p>
                    {current.components.length} inspected component
                    {current.components.length === 1 ? "" : "s"} ·{" "}
                    {declarations} observed declaration
                    {declarations === 1 ? "" : "s"}
                  </p>
                  <p className="feature-meta">
                    Report digest: <code>{current.digest}</code>
                  </p>
                </>
              ) : (
                <p role="status">
                  {reports.length
                    ? "Loading recorded evidence…"
                    : "No source inspection recorded."}
                </p>
              )}
            </>
          )}
          {step === 2 && current && (
            <div className="source-evidence-list">
              {current.components.map((item) => (
                <article key={item.id} className="source-evidence-component">
                  <h3>{item.name}</h3>
                  <p>
                    Source root:{" "}
                    <code>
                      {item.path === "." ? "Repository root" : item.path}
                    </code>
                  </p>
                  <p>
                    Commit: <code>{item.commit}</code>
                  </p>
                  <p>
                    {item.facts.length} observed declaration
                    {item.facts.length === 1 ? "" : "s"} · {item.skipped_files}{" "}
                    skipped file
                    {item.skipped_files === 1 ? "" : "s"} · source only
                  </p>
                  <details open>
                    <summary>Inspect source declarations</summary>
                    {item.facts.map((fact, index) => (
                      <article
                        className="source-fact"
                        key={`${fact.source.blob}/${fact.source.line}/${index}`}
                      >
                        <strong>{fact.kind.replaceAll("_", " ")}</strong>
                        <p>{fact.value}</p>
                        <p>
                          <code>
                            {fact.source.path}:{fact.source.line}
                          </code>
                        </p>
                        <details>
                          <summary>Exact source excerpt</summary>
                          <CodeSnippet
                            value={fact.source.excerpt}
                            language={(
                              fact.source.path.match(/\.([^.\\/]+)$/)?.[1] ||
                              "text"
                            ).toLowerCase()}
                          />
                          <p>
                            Commit: <code>{fact.source.commit}</code> · blob:{" "}
                            <code>{fact.source.blob}</code>
                          </p>
                        </details>
                      </article>
                    ))}
                  </details>
                </article>
              ))}
            </div>
          )}
          {step === 3 && current && (
            <>
              <h3>Open questions</h3>
              {current.open_questions.length ? (
                <ul>
                  {current.open_questions.map((question, index) => (
                    <li key={index}>{question}</li>
                  ))}
                </ul>
              ) : (
                <p>No open questions were recorded.</p>
              )}
              <p>
                Source facts do not automatically become a functional product
                definition. Review behavior and customer journeys separately.
              </p>
              <div className="feature-actions">
                {Object.entries(current.documents).map(([name, contents]) => (
                  <button
                    className="secondary"
                    key={name}
                    onClick={() => download(name, contents)}
                  >
                    <Download size={18} />
                    Download {name}
                  </button>
                ))}
              </div>
              <p className="feature-meta">
                Report digest: <code>{current.digest}</code>
              </p>
              {current.status === "ready_for_review" && (
                <label className="check-choice">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    disabled={busy}
                    onChange={(event) => setAcknowledged(event.target.checked)}
                  />
                  I reviewed the source findings and open questions. Runtime
                  behavior still needs testing.
                </label>
              )}
              {current.reviewed && (
                <p className="feature-status">Source findings reviewed</p>
              )}
              {current.reviewed && !reviewedContext && (
                <p className="feature-meta">
                  This reviewed report belongs to an earlier or unavailable
                  project snapshot. Inspect and review the current project scope
                  before continuing to product definition.
                </p>
              )}
            </>
          )}
          <div className="wizard-actions">
            <button
              className="secondary"
              disabled={busy}
              onClick={() => (step ? setStep(step - 1) : onClose())}
            >
              {step ? "Back" : "Back to project"}
            </button>
            {step === 0 ? (
              <button
                className="primary"
                disabled={busy || !selectedComponentIds.length || scopeHeld}
                onClick={() => void analyze()}
              >
                {busy
                  ? "Inspecting source…"
                  : `Inspect ${selectedComponentIds.length} component${selectedComponentIds.length === 1 ? "" : "s"}`}
              </button>
            ) : step < 3 ? (
              <button
                className="primary"
                disabled={
                  busy ||
                  !current ||
                  !["ready_for_review", "reviewed"].includes(current.status)
                }
                onClick={() => setStep(step + 1)}
              >
                Continue
              </button>
            ) : current?.status === "ready_for_review" ? (
              <button
                className="primary"
                disabled={busy || !acknowledged}
                onClick={() => void review()}
              >
                {busy ? "Recording review…" : "Confirm source review"}
              </button>
            ) : (
              <>
                <button
                  className={
                    reviewedContext && onContinueProduct
                      ? "secondary"
                      : "primary"
                  }
                  onClick={onClose}
                >
                  Done
                </button>
                {current?.reviewed && onContinueProduct && (
                  <button
                    type="button"
                    className="primary"
                    disabled={!reviewedContext}
                    onClick={() => {
                      if (reviewedContext) onContinueProduct(reviewedContext);
                    }}
                  >
                    Continue to product definition
                  </button>
                )}
              </>
            )}
          </div>
        </form>
      </Wizard>
    </section>
  );
}
