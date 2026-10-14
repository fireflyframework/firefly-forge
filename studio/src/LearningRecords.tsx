/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  Database,
  FileCheck2,
  FileQuestion,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import {
  readLearningRecords,
  readLearningProjection,
  type LearningRecord,
  type LearningProjection,
} from "./LearningRecordData";
import "./LearningRecords.css";
const problem = (cause: unknown) =>
  (cause instanceof Error
    ? cause.message
    : "Learning records could not be read."
  ).slice(0, 800);
const states = {
  candidate: "Candidate evidence",
  verified: "Verified evidence",
  invalidated: "Invalidated",
  superseded: "Superseded",
};
const kinds = {
  local_outcome: "Local outcome",
  observed_failure: "Observed failure",
  web_reference: "Source reference",
};
const projections = {
  pending: "Not submitted",
  executing: "Submission in progress",
  source_admitted: "Source admitted",
  verified: "Verified projection",
  unknown: "Outcome unknown",
};
const date = (value: string) => new Date(value).toLocaleString();
export function LearningRecords({
  connection,
  repository,
  session,
  repositoryLabel,
}: {
  connection: Connection;
  repository: string | undefined;
  session?: IdentitySession | null;
  repositoryLabel?: string;
}) {
  const [saved, setSaved] = useState<{
      repository: string;
      items: LearningRecord[];
    } | null>(null),
    [selected, setSelected] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [revision, setRevision] = useState(0),
    [query, setQuery] = useState(""),
    [state, setState] = useState("all");
  const alert = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLElement>(null);
  useEffect(() => {
    frame.current?.scrollIntoView({ block: "start" });
  }, []);
  useEffect(() => {
    if (!loading && saved?.repository === repository)
      frame.current?.scrollIntoView({ block: "start" });
  }, [loading, saved, repository]);
  useEffect(() => {
    if (!repository) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    request<unknown>(
      connection,
      `/repositories/${encodeURIComponent(repository)}/learning`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (controller.signal.aborted) return;
        setSaved({ repository, items: readLearningRecords(value, repository) });
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setSaved(null);
          setError(problem(cause));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, repository, revision, session]);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  const items =
    !loading && saved && saved.repository === repository ? saved.items : [];
  const filtered = items.filter(
    (item) =>
      (state === "all" || item.state === state) &&
      `${item.envelope.question} ${item.envelope.statement}`
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
  );
  const record = filtered.find((item) => item.id === selected);
  return (
    <section
      className="feature-record learning-records"
      aria-label="Learning records"
      ref={frame}
    >
      <header>
        <div>
          <h2>Learning records</h2>
          <p className="subtle">
            Saved evidence for {repositoryLabel ?? "the selected repository"}.
          </p>
        </div>
        <button
          className="secondary"
          disabled={!repository || loading}
          onClick={() => setRevision((value) => value + 1)}
        >
          <RefreshCw size={16} />
          Refresh records
        </button>
      </header>
      <p className="learning-records-boundary">
        <ShieldCheck size={18} />
        <span>
          Read-only project evidence. Access follows your current permissions.
        </span>
      </p>
      {error && (
        <div className="alert" role="alert" tabIndex={-1} ref={alert}>
          {error}
        </div>
      )}
      {!repository ? (
        <p>Choose a repository to read its learning records.</p>
      ) : loading ? (
        <p role="status">Loading learning records…</p>
      ) : !error && !items.length ? (
        <div className="learning-records-empty">
          <FileQuestion size={28} />
          <p>No learning records returned for this repository.</p>
          <p className="subtle">
            Verified runs and observed checks can leave scoped evidence here. A
            completed run alone does not establish reusable evidence.
          </p>
        </div>
      ) : (
        !error && (
          <>
            <details className="learning-records-filter-toggle">
              <summary>Filter saved records</summary>
              <div className="learning-records-filters">
                <div className="field">
                  <label htmlFor="learning-record-filter">
                    Filter saved records
                  </label>
                  <input
                    id="learning-record-filter"
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="learning-record-state">Saved state</label>
                  <select
                    id="learning-record-state"
                    value={state}
                    onChange={(event) => setState(event.target.value)}
                  >
                    <option value="all">All states</option>
                    {Object.entries(states).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            </details>
            <p className="learning-records-count" role="status">
              {filtered.length} of {items.length} saved records shown
            </p>
            {!filtered.length ? (
              <p>No saved records match this filter.</p>
            ) : (
              <div className="learning-records-grid">
                <aside aria-label="Saved evidence">
                  <ul className="learning-records-list">
                    {filtered.map((item) => (
                      <li key={item.id}>
                        <button
                          aria-label={item.envelope.question}
                          aria-pressed={selected === item.id}
                          onClick={() => setSelected(item.id)}
                        >
                          <span className="learning-record-question">
                            {item.envelope.question}
                          </span>
                          <span
                            className="learning-record-list-meta"
                            aria-hidden="true"
                          >
                            <span>{kinds[item.kind]}</span>
                            <span
                              className="learning-record-state"
                              data-state={item.state}
                            >
                              {states[item.state]}
                            </span>
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </aside>
                <div className="learning-records-detail">
                  {record ? (
                    <RecordDetail
                      key={record.id}
                      connection={connection}
                      record={record}
                    />
                  ) : (
                    <div className="learning-records-empty">
                      <FileCheck2 size={28} />
                      <h3>Select a saved record</h3>
                      <p>
                        Inspect its evidence, verification receipts and
                        projection status.
                      </p>
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )
      )}
    </section>
  );
}
function RecordDetail({
  connection,
  record,
}: {
  connection: Connection;
  record: LearningRecord;
}) {
  const [projection, setProjection] = useState<LearningProjection | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  useEffect(() => {
    if (record.kind !== "local_outcome") {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    request<unknown>(
      connection,
      `/learning/records/${encodeURIComponent(record.id)}/projection`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted)
          setProjection(readLearningProjection(value, record.id));
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(problem(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, record.id, record.kind]);
  const envelope = record.envelope,
    context = envelope.context;
  return (
    <article>
      <div className="learning-record-detail-heading">
        <span className="learning-record-state" data-state={record.state}>
          {states[record.state]}
        </span>
        <h3 tabIndex={-1} ref={heading}>
          {envelope.question}
        </h3>
      </div>
      <p className="learning-record-statement">{envelope.statement}</p>
      <dl className="learning-record-summary">
        <div>
          <dt>Evidence kind</dt>
          <dd>{kinds[record.kind]}</dd>
        </div>
        <div>
          <dt>Observed purpose</dt>
          <dd>{envelope.purpose.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Framework</dt>
          <dd>
            {context.framework} {context.version}
          </dd>
        </div>
        <div>
          <dt>Component</dt>
          <dd>{context.component_id ?? "Repository scope"}</dd>
        </div>
        {record.observed_at && (
          <div>
            <dt>Observed</dt>
            <dd>
              <time dateTime={record.observed_at}>
                {date(record.observed_at)}
              </time>
            </dd>
          </div>
        )}
        {record.verified_at && (
          <div>
            <dt>Verified</dt>
            <dd>
              <time dateTime={record.verified_at}>
                {date(record.verified_at)}
              </time>
            </dd>
          </div>
        )}
        <div>
          <dt>Recorded expiry</dt>
          <dd>
            <time dateTime={record.expires_at}>{date(record.expires_at)}</time>
          </dd>
        </div>
      </dl>
      <section className="learning-record-reuse">
        <h4>Reuse is checked during lookup</h4>
        <p>
          {record.state === "invalidated" || record.state === "superseded"
            ? "This saved record is excluded from reuse. Its provenance remains available for review."
            : record.kind !== "local_outcome" || record.state !== "verified"
              ? "This is candidate evidence. It cannot authorize reuse of a verified local outcome."
              : "Saved verification does not establish current eligibility. The lookup checks project and repository scope, framework and dependency versions, question, source tree, freshness and native receipts."}
        </p>
      </section>
      <section className="learning-record-projection">
        <h4>
          <Database size={18} />
          flycanon projection
        </h4>
        {record.kind !== "local_outcome" ? (
          <p>
            Candidate observations do not have a verified local-outcome
            projection.
          </p>
        ) : loading ? (
          <p role="status">Reading projection status…</p>
        ) : error ? (
          <div className="alert" role="alert">
            {error}
          </div>
        ) : (
          projection && (
            <>
              <span className="learning-record-state">
                {projections[projection.state]}
              </span>
              <p>
                {projection.state === "unknown"
                  ? "A previous submission has an unresolved outcome. This view does not retry it or create another source."
                  : projection.state === "pending"
                    ? "No projection has been submitted. Local verification and remote ingestion remain separate."
                    : projection.state === "verified"
                      ? "A source and citation were verified in the recorded workspace. Current reuse still requires eligible local evidence and the captured target."
                      : projection.state === "executing"
                        ? "A submission was recorded as running. Refreshing this view reads status without repeating the write."
                        : "The remote source was admitted. Indexing and a matching citation still need verification."}
              </p>
              {(projection.workspace_id || projection.source_id) && (
                <dl className="learning-record-summary">
                  <div>
                    <dt>Workspace</dt>
                    <dd>{projection.workspace_id ?? "Not recorded"}</dd>
                  </div>
                  <div>
                    <dt>Source</dt>
                    <dd>{projection.source_id ?? "Not recorded"}</dd>
                  </div>
                  <div>
                    <dt>Recorded source status</dt>
                    <dd>{projection.source_status ?? "Not recorded"}</dd>
                  </div>
                  <div>
                    <dt>Citations</dt>
                    <dd>{projection.citations.length}</dd>
                  </div>
                </dl>
              )}
              <p className="subtle">
                This explorer provides read-only review. Lifecycle writes
                require a separate authorized review and exact request
                reconciliation.
              </p>
            </>
          )
        )}
      </section>
      <details className="learning-record-provenance">
        <summary>Identifiers and verification receipts</summary>
        <dl className="learning-record-summary">
          <div>
            <dt>Record</dt>
            <dd>{record.id}</dd>
          </div>
          <div>
            <dt>Origin run</dt>
            <dd>{envelope.run_id}</dd>
          </div>
          <div>
            <dt>Project</dt>
            <dd>{context.project_id ?? "Unassigned; administrator scope"}</dd>
          </div>
          <div>
            <dt>Repository</dt>
            <dd>{context.repository_id}</dd>
          </div>
          <div>
            <dt>Source tree</dt>
            <dd>{context.source_tree}</dd>
          </div>
          <div>
            <dt>Base revision</dt>
            <dd>{envelope.source.base_revision}</dd>
          </div>
          <div>
            <dt>Inspected head</dt>
            <dd>{envelope.source.head}</dd>
          </div>
          <div>
            <dt>Dependency digest</dt>
            <dd>{context.dependency_digest}</dd>
          </div>
          <div>
            <dt>Question digest</dt>
            <dd>{context.query_digest}</dd>
          </div>
          {projection?.target_revision && (
            <div>
              <dt>Projection target revision</dt>
              <dd>{projection.target_revision}</dd>
            </div>
          )}
          {projection?.content_sha256 && (
            <div>
              <dt>Projected content digest</dt>
              <dd>{projection.content_sha256}</dd>
            </div>
          )}
        </dl>
        <h4>Native verification receipts</h4>
        {envelope.receipts.length ? (
          <ol className="learning-record-receipts">
            {envelope.receipts.map((receipt, index) => (
              <li key={index}>
                <strong>{receipt.kind.replaceAll("_", " ")}</strong>
                <span>Journal sequence {receipt.sequence}</span>
                <dl>
                  <div>
                    <dt>Receipt key</dt>
                    <dd>{receipt.key}</dd>
                  </div>
                  <div>
                    <dt>Digest</dt>
                    <dd>{receipt.digest}</dd>
                  </div>
                </dl>
              </li>
            ))}
          </ol>
        ) : (
          <p>No verification receipt references were returned.</p>
        )}
        <h4>Recorded citation references</h4>
        {envelope.citations.length ? (
          <ul>
            {envelope.citations.map((citation, index) => (
              <li key={index}>{citation}</li>
            ))}
          </ul>
        ) : (
          <p>No local citation references were returned.</p>
        )}
        {Boolean(projection?.citations.length) && (
          <>
            <h4>Projected citation references</h4>
            <ul>
              {projection?.citations.map((citation, index) => (
                <li key={index}>
                  {citation.source_id} / {citation.chunk_id}
                </li>
              ))}
            </ul>
          </>
        )}
      </details>
    </article>
  );
}
