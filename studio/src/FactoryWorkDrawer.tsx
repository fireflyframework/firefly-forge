/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  Box,
  ChevronLeft,
  ChevronRight,
  Factory,
  FolderGit2,
  Search,
  X,
} from "lucide-react";
import { request, type Connection } from "./api";
import type { DashboardProject, FactorySelection } from "./FactoryDashboard";

export function FactoryWorkDrawer({
  connection,
  open,
  standalone,
  selection,
  onChoose,
  onClose,
}: {
  connection: Connection;
  open: boolean;
  standalone: boolean;
  selection: FactorySelection;
  onChoose: (value: FactorySelection) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<(string | null)[]>([]);
  const [result, setResult] = useState<{
    key: string;
    items: DashboardProject[];
    next: string | null;
  }>({ key: "", items: [], next: null });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const key = JSON.stringify([search, cursor]);
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open) dialog.current?.close();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ search });
    if (cursor) query.set("cursor", cursor);
    void request<{
      version: 1;
      items: DashboardProject[];
      next_cursor: string | null;
    }>(connection, "/factory-dashboard/projects?" + query, {
      signal: controller.signal,
    })
      .then((value) => {
        if (value.version !== 1 || !Array.isArray(value.items))
          throw new Error(
            "The server returned an incompatible product-project page.",
          );
        if (!controller.signal.aborted)
          setResult({ key, items: value.items, next: value.next_cursor });
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Product projects could not be read.",
          );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, key, open, search, cursor]);
  const items = result.key === key ? result.items : [];
  function choose(kind: FactorySelection["kind"], project = "") {
    onChoose({ kind, project, run: "", view: selection.view });
    onClose();
  }
  return (
    <dialog
      className="factory-work-drawer"
      ref={dialog}
      aria-labelledby="factory-explorer-title"
      onCancel={onClose}
      onClose={onClose}
    >
      <header>
        <h2 id="factory-explorer-title">Factory work</h2>
        <button
          className="quiet icon-button"
          aria-label="Close work explorer"
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </header>
      <nav aria-label="Factory scopes">
        <button
          className="factory-scope-row"
          aria-current={selection.kind === "factory" ? "page" : undefined}
          onClick={() => choose("factory")}
        >
          <Factory size={18} />
          <span>Factory overview</span>
        </button>
        {standalone && (
          <button
            className="factory-scope-row"
            aria-current={selection.kind === "standalone" ? "page" : undefined}
            onClick={() => choose("standalone")}
          >
            <FolderGit2 size={18} />
            <span>Standalone repositories</span>
          </button>
        )}
      </nav>
      <section>
        <h3>Product projects</h3>
        <p>Each product can include several components and repositories.</p>
        <form
          className="factory-project-search"
          onSubmit={(event) => {
            event.preventDefault();
            setCursor(null);
            setHistory([]);
            setSearch(query.trim());
          }}
        >
          <label className="field">
            Find a product project
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={200}
            />
          </label>
          <button
            className="secondary icon-button"
            aria-label="Search product projects"
          >
            <Search size={18} />
          </button>
        </form>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="factory-project-page" aria-busy={loading}>
          {items.map((project) => (
            <button
              key={project.id}
              className="factory-project-row"
              aria-current={
                selection.project === project.id && selection.kind !== "factory"
                  ? "page"
                  : undefined
              }
              onClick={() => choose("project", project.id)}
            >
              <Box size={19} />
              <span>
                <strong>{project.name}</strong>
                <small>
                  {project.component_count.toLocaleString()}{" "}
                  {project.component_count === 1 ? "component" : "components"} ·{" "}
                  {project.repository_count.toLocaleString()}{" "}
                  {project.repository_count === 1
                    ? "repository"
                    : "repositories"}
                </small>
              </span>
              <ChevronRight size={16} />
            </button>
          ))}
          {!loading && !error && !items.length && (
            <p>No accessible product projects match this search.</p>
          )}
        </div>
        <footer className="factory-project-pager">
          <button
            className="quiet"
            disabled={loading || !history.length}
            onClick={() => {
              setCursor(history.at(-1) ?? null);
              setHistory((values) => values.slice(0, -1));
            }}
          >
            <ChevronLeft size={16} />
            Previous
          </button>
          <button
            className="quiet"
            disabled={loading || result.key !== key || !result.next}
            onClick={() => {
              setHistory((values) => [...values, cursor]);
              setCursor(result.next);
            }}
          >
            Next
            <ChevronRight size={16} />
          </button>
        </footer>
      </section>
    </dialog>
  );
}
