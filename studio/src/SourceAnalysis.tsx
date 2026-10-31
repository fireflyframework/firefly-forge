/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { Download, FileSearch, RefreshCw, X } from "lucide-react";
import { request, type Connection, type ProjectSnapshot } from "./api";
import { Wizard } from "./Wizard";
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
};
const failure = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "Source inspection could not be completed.";
export function SourceAnalysis({
  connection,
  project,
}: {
  connection: Connection;
  project: ProjectSnapshot;
}) {
  const [reports, setReports] = useState<Report[]>([]),
    [selected, setSelected] = useState(""),
    [report, setReport] = useState<Report | null>(null),
    [components, setComponents] = useState(
      project.definition.components.map((item) => item.id),
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0),
    [acknowledged, setAcknowledged] = useState(false);
  const [open, setOpen] = useState(false),
    [step, setStep] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
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
            setError("");
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(failure(cause));
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
        if (!controller.signal.aborted) setError(failure(cause));
      });
    return () => controller.abort();
  }, [connection, project.id, id, revision]);
  async function analyze() {
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
          body: JSON.stringify({ component_ids: components }),
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
      if (!controller.signal.aborted) setError(failure(cause));
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
  const declarations =
    current?.components.reduce((total, item) => total + item.facts.length, 0) ??
    0;
  return (
    <section className="source-analysis feature-record">
      <header>
        <div>
          <h2>
            <FileSearch size={24} />
            Source AS-IS
          </h2>
          <p>
            Review declared routes, tests and dependencies at their exact source
            commits. Inspection reads source without executing the application.
          </p>
        </div>
        <button
          className="secondary"
          onClick={() => {
            setStep(reports.length ? 1 : 0);
            setOpen(true);
          }}
        >
          Open source inspection
        </button>
      </header>
      <p className="feature-meta">
        {reports.length
          ? `${reports.length} recorded inspection${reports.length === 1 ? "" : "s"} · ${reports[0].status.replaceAll("_", " ")}`
          : "No source inspection recorded."}
      </p>
      {!open && error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      <dialog
        ref={dialog}
        className="feature-dialog source-analysis-dialog"
        aria-label="Source inspection review"
        onCancel={(event) => {
          if (busy) event.preventDefault();
          else setOpen(false);
        }}
      >
        <header className="source-dialog-heading">
          <h2>Source inspection</h2>
          <button
            className="quiet source-dialog-close"
            disabled={busy}
            onClick={() => setOpen(false)}
            aria-label="Close source inspection"
          >
            <X size={18} />
          </button>
        </header>
        {open && (
          <Wizard
            steps={["Scope", "Receipt", "Evidence", "Review"]}
            active={step}
            onStep={setStep}
            locked={busy}
            title={
              [
                "Inspect configured source",
                "Recorded inspection",
                "Source declarations",
                "Review the source evidence",
              ][step]
            }
            description={
              [
                "Choose the component roots to inspect. No application code is executed.",
                "Select an actual recorded report before reviewing its declarations.",
                "Each declaration retains its file, line, blob and source commit.",
                "Acknowledge this exact digest; source evidence does not establish runtime behavior or customer journeys.",
              ][step]
            }
          >
            <form onSubmit={(event) => event.preventDefault()}>
              {error && (
                <div className="alert" role="alert">
                  {error}
                </div>
              )}
              {step === 0 && (
                <fieldset>
                  <legend>Components to inspect</legend>
                  <div className="channel-choices">
                    {project.definition.components.map((item) => (
                      <label className="check-choice" key={item.id}>
                        <input
                          type="checkbox"
                          checked={components.includes(item.id)}
                          disabled={busy}
                          onChange={(event) =>
                            setComponents((values) =>
                              event.target.checked
                                ? [...values, item.id]
                                : values.filter((id) => id !== item.id),
                            )
                          }
                        />
                        {item.name} ·{" "}
                        {item.path === "." ? "Repository root" : item.path}
                      </label>
                    ))}
                  </div>
                </fieldset>
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
                          {item.id.slice(0, 8)} ·{" "}
                          {item.status.replaceAll("_", " ")} ·{" "}
                          {new Date(item.created_at).toLocaleString()}
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
                        {current.status.replaceAll("_", " ")} · source
                        inspection
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
                    <article
                      key={item.id}
                      className="source-evidence-component"
                    >
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
                        {item.facts.length === 1 ? "" : "s"} ·{" "}
                        {item.skipped_files} skipped file
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
                              <pre>{fact.source.excerpt}</pre>
                              <p>
                                Commit: <code>{fact.source.commit}</code> ·
                                blob: <code>{fact.source.blob}</code>
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
                    Source facts do not automatically become a functional
                    product definition. Review behavior and customer journeys
                    separately.
                  </p>
                  <div className="feature-actions">
                    {Object.entries(current.documents).map(
                      ([name, contents]) => (
                        <button
                          className="secondary"
                          key={name}
                          onClick={() => download(name, contents)}
                        >
                          <Download size={18} />
                          Download {name}
                        </button>
                      ),
                    )}
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
                        onChange={(event) =>
                          setAcknowledged(event.target.checked)
                        }
                      />
                      I reviewed the source evidence and open questions. This
                      does not claim observed runtime behavior.
                    </label>
                  )}
                  {current.reviewed && (
                    <p className="feature-status">
                      reviewed · source inspection
                    </p>
                  )}
                </>
              )}
              <div className="wizard-actions">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => (step ? setStep(step - 1) : setOpen(false))}
                >
                  {step ? "Back" : "Cancel"}
                </button>
                {step === 0 ? (
                  <button
                    className="primary"
                    disabled={busy || !components.length}
                    onClick={() => void analyze()}
                  >
                    {busy ? "Inspecting source…" : "Inspect configured source"}
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
                    {busy
                      ? "Recording review…"
                      : "Acknowledge this report digest"}
                  </button>
                ) : (
                  <button className="primary" onClick={() => setOpen(false)}>
                    Done
                  </button>
                )}
              </div>
            </form>
          </Wizard>
        )}
      </dialog>
    </section>
  );
}
