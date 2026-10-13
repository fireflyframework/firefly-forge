/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Upload, X } from "lucide-react";
import { post, request, type Connection } from "./api";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
import { TextFileInput } from "./TextFileInput";
type Admission = {
  id: string;
  repository_id: string;
  filename: string;
  title: string;
  content_sha256: string;
  content_bytes: number;
  workspace_id: string;
  status: "submitting" | "submitted" | "needs_reconciliation" | "rejected";
  source_id?: string;
  source_status?: string;
  created_at: string;
  updated_at: string;
  detail?: string;
  history: { status: string; at: string }[];
};
const problem = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The upload record could not be loaded.";
const base64 = (value: string) => {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32768)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  return btoa(binary);
};
export function KnowledgeUploads({
  connection,
  repository,
  enabled,
  onRefresh,
}: {
  connection: Connection;
  repository: string;
  enabled: boolean;
  onRefresh: () => void;
}) {
  const [items, setItems] = useState<Admission[]>([]);
  const [loaded, setLoaded] = useState("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!repository || !enabled) return;
    const controller = new AbortController();
    request<Admission[]>(
      connection,
      `/repositories/${repository}/knowledge/uploads`,
      { signal: controller.signal },
    )
      .then((records) => {
        if (controller.signal.aborted) return;
        setItems(records);
        setLoaded(repository);
        setError("");
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(problem(cause));
      });
    return () => controller.abort();
  }, [connection, repository, enabled, revision]);
  return (
    <section className="knowledge-uploads">
      <header>
        <div>
          <h3>Upload project knowledge</h3>
          <p>
            Submit bounded text files to this project's workspace. Admission and
            ingestion are separate recorded states.
          </p>
        </div>
        <button
          className="secondary"
          disabled={!enabled || !repository}
          onClick={() => setOpen(true)}
        >
          <Upload size={18} />
          Upload a file
        </button>
      </header>
      <p className="feature-meta">
        UTF-8 .txt, .md, .json, .yaml, .yml and .csv files; up to 1 MB. Review
        the file for secrets before submitting.
      </p>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {loaded === repository && items.length > 0 && (
        <details>
          <summary>{items.length} recorded uploads</summary>
          {items.map((item) => (
            <article className="feature-record" key={item.id}>
              <strong>{item.title || item.filename}</strong>
              <p>
                {item.filename} · {item.content_bytes} bytes · Admission{" "}
                {item.status.replaceAll("_", " ")}
              </p>
              <p>
                Remote source:{" "}
                {item.source_status || "No ingestion status recorded"}
              </p>
              {item.detail && <p>{item.detail}</p>}
              <code>{item.content_sha256}</code>
            </article>
          ))}
        </details>
      )}
      {open && (
        <UploadWizard
          key={repository}
          connection={connection}
          repository={repository}
          onClose={() => setOpen(false)}
          onUpdated={() => {
            setRevision((value) => value + 1);
            onRefresh();
          }}
        />
      )}
    </section>
  );
}
function UploadWizard({
  connection,
  repository,
  onClose,
  onUpdated,
}: {
  connection: Connection;
  repository: string;
  onClose: () => void;
  onUpdated: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [step, setStep] = useState(0);
  const [content, setContent] = useState("");
  const [filename, setFilename] = useState("");
  const [title, setTitle] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [admission, setAdmission] = useState<Admission | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sourceId, setSourceId] = useState("");
  const updated = useRef(onUpdated);
  updated.current = onUpdated;
  const count = new TextEncoder().encode(content).length;
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  useEffect(() => {
    if (!admission?.id) return;
    const controller = new AbortController();
    const id = admission.id;
    const update = () =>
      request<Admission>(
        connection,
        `/repositories/${repository}/knowledge/uploads/${id}`,
        { signal: controller.signal },
      )
        .then((item) => {
          if (controller.signal.aborted) return;
          setAdmission(item);
          setError("");
          updated.current();
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(problem(cause));
        });
    const timer = setInterval(() => void update(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [connection, repository, admission?.id]);
  async function reconcile() {
    if (!admission || !sourceId.trim()) return;
    setBusy(true);
    setError("");
    try {
      const item = await post<Admission>(
        connection,
        `/repositories/${repository}/knowledge/uploads/${admission.id}/reconcile`,
        { source_id: sourceId.trim() },
      );
      setAdmission(item);
      setSourceId("");
      onUpdated();
    } catch (cause) {
      setError(problem(cause));
    } finally {
      setBusy(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (admission) {
      if (admission.status === "needs_reconciliation" && sourceId.trim())
        await reconcile();
      return;
    }
    if (!count || count > 1000000) {
      setError("Choose a nonempty UTF-8 file no larger than 1 MB.");
      return;
    }
    if (step < 2) {
      setStep(step + 1);
      return;
    }
    if (admission) return;
    setBusy(true);
    try {
      const item = await post<Admission>(
        connection,
        `/repositories/${repository}/knowledge/uploads`,
        { filename, title, content_base64: base64(content) },
      );
      setAdmission(item);
      setContent("");
      setStep(3);
      onUpdated();
    } catch (cause) {
      setError(problem(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="feature-dialog"
      aria-label="Upload project knowledge"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <button
        className="quiet"
        type="button"
        disabled={busy}
        onClick={onClose}
        aria-label="Close knowledge upload"
      >
        <X size={18} />
        Close
      </button>
      <Wizard
        steps={["File", "Details", "Review", "Receipt"]}
        active={step}
        locked={Boolean(admission)}
        onStep={(value) => {
          if (!admission) setStep(value);
        }}
        title={
          [
            "Choose a project file",
            "Confirm the source details",
            "Review the text admission",
            "Inspect the recorded admission",
          ][step]
        }
        description={
          [
            "Only bounded UTF-8 text formats are accepted.",
            "Confirm the filename and title stored with the source.",
            "Submission writes a durable admission record. Ingestion is confirmed separately by the knowledge service.",
            "These statuses come from the saved admission and the scoped remote source.",
          ][step]
        }
      >
        <SetupIllustration kind="knowledge" step={step} />
        <form onSubmit={(event) => void submit(event)}>
          {step === 0 && (
            <TextFileInput
              label="Choose knowledge file"
              extensions={[".txt", ".md", ".json", ".yaml", ".yml", ".csv"]}
              maxBytes={1000000}
              onRead={(value, name) => {
                setContent(value);
                setFilename(name);
                setTitle(name.replace(/\.[^.]+$/, ""));
                setReviewed(false);
              }}
            />
          )}
          {step === 1 && (
            <>
              <div className="field">
                <label htmlFor="upload-filename">Source filename</label>
                <input
                  id="upload-filename"
                  required
                  value={filename}
                  onChange={(event) => setFilename(event.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="upload-title">Source title</label>
                <input
                  id="upload-title"
                  required
                  maxLength={200}
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                />
              </div>
              <p>{count} UTF-8 bytes will be submitted.</p>
            </>
          )}
          {step === 2 && (
            <>
              <dl className="wizard-summary">
                <div>
                  <dt>File</dt>
                  <dd>{filename}</dd>
                </div>
                <div>
                  <dt>Title</dt>
                  <dd>{title}</dd>
                </div>
                <div>
                  <dt>Size</dt>
                  <dd>{count} bytes</dd>
                </div>
              </dl>
              <details>
                <summary>Review text excerpt</summary>
                <pre>
                  {content.slice(0, 2000)}
                  {content.length > 2000 ? "\n…Excerpt only" : ""}
                </pre>
              </details>
              <label className="check-choice">
                <input
                  type="checkbox"
                  required
                  checked={reviewed}
                  onChange={(event) => setReviewed(event.target.checked)}
                />
                I reviewed this file and confirmed it contains no secrets.
              </label>
            </>
          )}
          {step === 3 && admission && (
            <>
              <span className="feature-status">
                Admission {admission.status.replaceAll("_", " ")}
              </span>
              <p>
                Remote source status:{" "}
                {admission.source_status || "Not yet reported"}
              </p>
              {admission.detail && <p>{admission.detail}</p>}
              {admission.status === "needs_reconciliation" && (
                <section className="integration-section">
                  <p>
                    The remote outcome is uncertain. Reconcile an existing
                    source in this workspace; this action verifies its filename
                    and digest without resubmitting content.
                  </p>
                  <div className="field">
                    <label htmlFor="upload-source-id">
                      Existing remote source ID
                    </label>
                    <input
                      id="upload-source-id"
                      value={sourceId}
                      onChange={(event) => setSourceId(event.target.value)}
                    />
                  </div>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy || !sourceId.trim()}
                    onClick={() => void reconcile()}
                  >
                    {busy ? "Reconciling…" : "Reconcile existing source"}
                  </button>
                </section>
              )}
              <dl className="wizard-summary">
                <div>
                  <dt>Workspace</dt>
                  <dd>{admission.workspace_id}</dd>
                </div>
                <div>
                  <dt>Source ID</dt>
                  <dd>{admission.source_id || "Not yet assigned"}</dd>
                </div>
                <div>
                  <dt>Content digest</dt>
                  <dd>
                    <code>{admission.content_sha256}</code>
                  </dd>
                </div>
              </dl>
              <details>
                <summary>Recorded status history</summary>
                {admission.history.map((item, index) => (
                  <p key={index}>
                    {item.status} · {new Date(item.at).toLocaleString()}
                  </p>
                ))}
              </details>
            </>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className="wizard-actions">
            {step > 0 && !admission && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => setStep(step - 1)}
              >
                Back
              </button>
            )}
            {step < 3 && (
              <button disabled={busy || (step === 0 && !content)}>
                {busy
                  ? "Submitting…"
                  : step === 2
                    ? "Submit knowledge file"
                    : "Continue"}
              </button>
            )}
            {step === 3 && (
              <button type="button" className="primary" onClick={onClose}>
                Done
              </button>
            )}
          </div>
        </form>
      </Wizard>
    </dialog>
  );
}
