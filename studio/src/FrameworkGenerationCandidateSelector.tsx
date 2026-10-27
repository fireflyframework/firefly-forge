/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import type { Connection } from "./api";
import {
  formatKnowledgeTimestamp,
  type FrameworkCorpus,
} from "./FrameworkKnowledgeData";
import {
  frameworkGenerationCandidates,
  type FrameworkGenerationCandidate,
} from "./FrameworkGenerationCandidates";

export function FrameworkGenerationCandidateSelector({
  connection,
  corpus,
  selected,
  onSelect,
}: {
  connection: Connection;
  corpus: FrameworkCorpus;
  selected: FrameworkGenerationCandidate | null;
  onSelect: (value: FrameworkGenerationCandidate | null) => void;
}) {
  const [items, setItems] = useState<FrameworkGenerationCandidate[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    frameworkGenerationCandidates(connection, corpus, cursor, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setItems((previous) => [
          ...(cursor === null ? [] : previous),
          ...page.items.filter(
            (value) =>
              cursor === null ||
              !previous.some(
                (item) => item.validation_run_id === value.validation_run_id,
              ),
          ),
        ]);
        setNextCursor(page.next_cursor);
        setLoaded(true);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Eligible validation runs could not be read.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, corpus, cursor, reload]);
  const inputId = `generation-run-${corpus.framework}`;
  return (
    <div className="framework-generation-candidates" aria-busy={loading}>
      <div className="field">
        <label htmlFor={inputId}>Eligible validation run</label>
        <select
          id={inputId}
          value={selected?.validation_run_id ?? ""}
          required
          disabled={loading || !items.length}
          onChange={(event) =>
            onSelect(
              items.find(
                (item) => item.validation_run_id === event.target.value,
              ) ?? null,
            )
          }
        >
          <option value="" disabled>
            Choose a completed validation
          </option>
          {items.map((item) => (
            <option key={item.validation_run_id} value={item.validation_run_id}>
              {item.repository_name} / {item.component_id} ·{" "}
              {item.runtime.runtime} {item.runtime.version} ·{" "}
              {formatKnowledgeTimestamp(item.created_at)}
            </option>
          ))}
        </select>
      </div>
      {loading && <p role="status">Reading eligible validation runs…</p>}
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {loaded && !loading && !error && (
        <p className="subtle" role="status">
          {items.length
            ? `${items.length} eligible validation ${items.length === 1 ? "run" : "runs"} loaded.`
            : nextCursor
              ? "No eligible validation runs on this page. Continue loading to check the remaining runs."
              : "No eligible validation runs found. Complete canonical validation for this exact corpus, then reload the list."}
        </p>
      )}
      {selected && (
        <dl className="framework-candidate-details">
          <dt>Repository / component</dt>
          <dd>
            {selected.repository_name} / {selected.component_id}
          </dd>
          <dt>Validated runtime</dt>
          <dd>
            {selected.runtime.runtime} {selected.runtime.version}
          </dd>
          <dt>Run created</dt>
          <dd>
            <time dateTime={selected.created_at}>
              {formatKnowledgeTimestamp(selected.created_at)}
            </time>
          </dd>
        </dl>
      )}
      <div className="framework-actions">
        <button
          type="button"
          className="secondary"
          disabled={loading}
          onClick={() => {
            onSelect(null);
            setItems([]);
            setNextCursor(null);
            setLoaded(false);
            setCursor(null);
            setReload((value) => value + 1);
          }}
        >
          Reload validation runs
        </button>
        {nextCursor !== null && (
          <button
            type="button"
            className="secondary"
            disabled={loading}
            onClick={() => {
              setCursor(nextCursor);
              setReload((value) => value + 1);
            }}
          >
            {error ? "Retry loading more runs" : "Load more validation runs"}
          </button>
        )}
      </div>
    </div>
  );
}
