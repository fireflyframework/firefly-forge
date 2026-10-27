/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import type { Connection } from "./api";
import { safeExternalLink } from "./FeatureLinks";
import {
  frameworkCorpora,
  frameworkSources,
  type FrameworkCorpus,
  type FrameworkPage,
  type FrameworkName,
  type FrameworkSource,
} from "./FrameworkKnowledgeData";

export function FrameworkCorpusSelector({
  connection,
  selected,
  framework,
  inputId = "framework-corpus-version",
  onSelect,
}: {
  connection: Connection;
  selected: FrameworkCorpus | null;
  framework?: FrameworkName;
  inputId?: string;
  onSelect: (value: FrameworkCorpus) => void;
}) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<FrameworkPage<FrameworkCorpus> | null>(null);
  const [error, setError] = useState("");
  const cursor = cursors[cursors.length - 1];
  const selectedFramework = selected?.framework ?? framework;
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError("");
    if (!selectedFramework) return;
    frameworkCorpora(connection, selectedFramework, cursor, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setPage(value);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Corpus versions could not be read.",
          );
      });
    return () => controller.abort();
  }, [connection, selectedFramework, cursor]);
  const options = [
    ...(selected ? [selected] : []),
    ...(page?.items ?? []).filter(
      (value) => value.corpus_ref !== selected?.corpus_ref,
    ),
  ];
  return (
    <div className="framework-corpus-selector">
      <div className="field">
        <label htmlFor={inputId}>Framework release</label>
        <select
          id={inputId}
          value={selected?.corpus_ref ?? ""}
          onChange={(event) => {
            const value = options.find(
              (item) => item.corpus_ref === event.target.value,
            );
            if (value) onSelect(value);
          }}
        >
          {!selected && (
            <option value="" disabled>
              Choose a retained corpus
            </option>
          )}
          {options.map((value) => (
            <option key={value.corpus_ref} value={value.corpus_ref}>
              {value.framework} {value.framework_version} · {value.state} ·{" "}
              {value.commit.slice(0, 12)}
            </option>
          ))}
        </select>
      </div>
      <p className="subtle">
        {page ? (
          <>
            {page.total} retained corpora.{" "}
            {selected ? (
              <>
                Selected commit{" "}
                <code className="framework-selected-commit">
                  {selected.commit.slice(0, 12)}
                </code>
              </>
            ) : (
              "Choose an exact ready corpus."
            )}
          </>
        ) : (
          "Reading retained corpus versions…"
        )}
      </p>
      {selected && (
        <details className="framework-selected-provenance">
          <summary>Full selected commit</summary>
          <p>
            <code>{selected.commit}</code>
          </p>
        </details>
      )}
      <div className="framework-actions">
        <button
          type="button"
          className="secondary"
          disabled={!page || cursors.length === 1}
          onClick={() => setCursors((items) => items.slice(0, -1))}
        >
          Previous versions
        </button>
        <button
          type="button"
          className="secondary"
          disabled={!page?.next_cursor}
          onClick={() => {
            if (page?.next_cursor)
              setCursors((items) => [...items, page.next_cursor]);
          }}
        >
          Next versions
        </button>
      </div>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function FrameworkCorpusSources({
  connection,
  corpus,
}: {
  connection: Connection;
  corpus: FrameworkCorpus;
}) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState<FrameworkPage<FrameworkSource> | null>(null);
  const [error, setError] = useState("");
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError("");
    frameworkSources(connection, corpus, cursor, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setPage(value);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Framework sources could not be read.",
          );
      });
    return () => controller.abort();
  }, [connection, corpus, cursor]);
  return (
    <section className="feature-record framework-corpus-record">
      <h2>
        {corpus.framework} {corpus.framework_version} sources
      </h2>
      <p className="subtle">
        {corpus.state} · Commit <code>{corpus.commit}</code>. Selected, pending
        and excluded files retain distinct states.
      </p>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {!page && !error && <p role="status">Reading the source inventory…</p>}
      {page && (
        <>
          <p>
            {page.total.toLocaleString()} source records. This page shows{" "}
            {page.items.length}.
          </p>
          <div className="feature-list">
            {page.items.map((source) => (
              <article key={source.source_ref}>
                <header>
                  <div>
                    <h3>{source.path}</h3>
                    <p className="subtle">
                      {source.kind} · {source.bytes.toLocaleString()} bytes ·{" "}
                      {source.ready_parts} of {source.parts} parts ready
                    </p>
                  </div>
                  <span className="feature-status">
                    {source.state.replaceAll("_", " ")}
                  </span>
                </header>
                {source.exclusion_reason && (
                  <p>Excluded: {source.exclusion_reason}</p>
                )}
                <details>
                  <summary>Source provenance</summary>
                  <dl className="wizard-summary">
                    <div>
                      <dt>Git blob</dt>
                      <dd>
                        <code>{source.blob_oid}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Content SHA-256</dt>
                      <dd>
                        <code>{source.sha256 ?? "Not fetched"}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>License SHA-256</dt>
                      <dd>
                        <code>{source.license_sha256}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Source</dt>
                      <dd>
                        {safeExternalLink(source.source_uri) ? (
                          <a
                            href={safeExternalLink(source.source_uri)!}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {source.source_uri}
                          </a>
                        ) : (
                          source.source_uri
                        )}
                      </dd>
                    </div>
                  </dl>
                </details>
              </article>
            ))}
          </div>
          {!page.items.length && (
            <p>No source records were returned for this corpus.</p>
          )}
          <div className="framework-actions">
            <button
              className="secondary"
              disabled={cursors.length === 1}
              onClick={() => setCursors((items) => items.slice(0, -1))}
            >
              Previous sources
            </button>
            <button
              className="secondary"
              disabled={!page.next_cursor}
              onClick={() => {
                if (page.next_cursor)
                  setCursors((items) => [...items, page.next_cursor]);
              }}
            >
              Next sources
            </button>
          </div>
        </>
      )}
    </section>
  );
}
