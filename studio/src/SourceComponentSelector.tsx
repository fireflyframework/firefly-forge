/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useId, useRef } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { ProjectComponent, Repository } from "./api";
import { FormField } from "./FormPrimitives";
import { repositoryLabels } from "./RepositoryLabels";
import "./SourceComponentSelector.css";

export type SourceComponentFilters = {
  query: string;
  kind: string;
  repository: string;
  selectedOnly: boolean;
  page: number;
};
const kinds: Record<ProjectComponent["kind"], string> = {
  api: "API",
  web: "Web",
  worker: "Worker",
  library: "Library",
  fullstack: "Full stack",
};
export function SourceComponentSelector({
  components,
  repositories,
  selected,
  onSelectedChange,
  filters,
  onFiltersChange,
  disabled = false,
  changedIds = [],
}: {
  components: ProjectComponent[];
  repositories: Repository[];
  selected: string[];
  onSelectedChange: (ids: string[]) => void;
  filters: SourceComponentFilters;
  onFiltersChange: (filters: SourceComponentFilters) => void;
  disabled?: boolean;
  changedIds?: string[];
}) {
  const id = useId(),
    selectedOnly = useRef<HTMLInputElement>(null);
  const labels = repositoryLabels(repositories);
  const label = (repository: string) =>
    labels.get(repository) ??
    `Unavailable repository · ${repository.slice(0, 8)}`;
  const selectedIds = new Set(selected);
  const changed = new Set(changedIds);
  const query = filters.query.trim().toLocaleLowerCase();
  const matching = components.filter(
    (item) =>
      (!query ||
        `${item.name} ${item.path}`.toLocaleLowerCase().includes(query)) &&
      (!filters.kind || item.kind === filters.kind) &&
      (!filters.repository || item.repository_id === filters.repository) &&
      (!filters.selectedOnly || selectedIds.has(item.id)),
  );
  const pageSize = 25,
    lastPage = Math.max(0, Math.ceil(matching.length / pageSize) - 1);
  const page = Math.min(filters.page, lastPage),
    start = page * pageSize;
  const visible = matching.slice(start, start + pageSize);
  const selectedCount = components.filter((item) =>
    selectedIds.has(item.id),
  ).length;
  const matchingSelected = matching.filter((item) =>
    selectedIds.has(item.id),
  ).length;
  const filterCount =
    Number(Boolean(filters.kind)) + Number(Boolean(filters.repository));
  const filtered = Boolean(query || filterCount || filters.selectedOnly);
  const updateFilters = (next: Partial<SourceComponentFilters>) =>
    onFiltersChange({ ...filters, ...next, page: 0 });
  return (
    <section
      className="source-component-selector"
      aria-label="Components to inspect"
    >
      <FormField id={`${id}-search`} label="Search components">
        <input
          id={`${id}-search`}
          type="search"
          placeholder="Name or path"
          value={filters.query}
          disabled={disabled}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.preventDefault();
          }}
          onChange={(event) => updateFilters({ query: event.target.value })}
        />
      </FormField>
      <div className="source-component-tools">
        <details className="source-component-filters">
          <summary>
            Filters{filterCount ? ` · ${filterCount} applied` : ""}
          </summary>
          <div className="source-component-filter-fields">
            <FormField id={`${id}-kind`} label="Component type">
              <select
                id={`${id}-kind`}
                value={filters.kind}
                disabled={disabled}
                onChange={(event) =>
                  updateFilters({ kind: event.target.value })
                }
              >
                <option value="">All types</option>
                {Object.entries(kinds)
                  .filter(([kind]) =>
                    components.some((item) => item.kind === kind),
                  )
                  .map(([kind, name]) => (
                    <option key={kind} value={kind}>
                      {name}
                    </option>
                  ))}
              </select>
            </FormField>
            <FormField id={`${id}-repository`} label="Repository">
              <select
                id={`${id}-repository`}
                value={filters.repository}
                disabled={disabled}
                onChange={(event) =>
                  updateFilters({ repository: event.target.value })
                }
              >
                <option value="">All repositories</option>
                {[...new Set(components.map((item) => item.repository_id))]
                  .sort((a, b) => label(a).localeCompare(label(b)))
                  .map((repository) => (
                    <option key={repository} value={repository}>
                      {label(repository)}
                    </option>
                  ))}
              </select>
            </FormField>
          </div>
        </details>
        <label className="check-choice source-component-selected-only">
          <input
            ref={selectedOnly}
            type="checkbox"
            checked={filters.selectedOnly}
            disabled={disabled}
            onChange={(event) =>
              updateFilters({ selectedOnly: event.target.checked })
            }
          />
          Selected only
        </label>
      </div>
      <p
        id={`${id}-count`}
        role="status"
        aria-label="Component selection"
        className="source-component-count"
      >
        {selectedCount.toLocaleString("en-US")} of{" "}
        {components.length.toLocaleString("en-US")} selected ·{" "}
        {matching.length.toLocaleString("en-US")}{" "}
        {matching.length === 1 ? "result" : "results"}
      </p>
      <div className="source-component-bulk">
        <button
          type="button"
          className="secondary"
          aria-describedby={`${id}-count`}
          disabled={
            disabled || !matching.length || matchingSelected === matching.length
          }
          onClick={() =>
            onSelectedChange([
              ...new Set([...selected, ...matching.map((item) => item.id)]),
            ])
          }
        >
          Select matching
        </button>
        <button
          type="button"
          className="secondary"
          aria-describedby={`${id}-count`}
          disabled={disabled || matchingSelected === 0}
          onClick={() => {
            const ids = new Set(matching.map((item) => item.id));
            onSelectedChange(selected.filter((value) => !ids.has(value)));
          }}
        >
          Clear matching
        </button>
      </div>
      {!matching.length ? (
        <div className="source-component-empty">
          <p>No components match these filters.</p>
          {filtered && (
            <button
              type="button"
              className="secondary"
              disabled={disabled}
              onClick={() =>
                onFiltersChange({
                  query: "",
                  kind: "",
                  repository: "",
                  selectedOnly: false,
                  page: 0,
                })
              }
            >
              Reset filters
            </button>
          )}
        </div>
      ) : (
        <>
          <ul className="source-component-list" aria-label="Component results">
            {visible.map((item, index) => (
              <li key={item.id}>
                <label className="source-component-choice">
                  <input
                    type="checkbox"
                    checked={selectedIds.has(item.id)}
                    disabled={disabled}
                    aria-label={`${item.name} · ${item.path === "." ? "Repository root" : item.path}`}
                    aria-describedby={`${id}-meta-${index}`}
                    onChange={(event) => {
                      onSelectedChange(
                        event.target.checked
                          ? [...selected, item.id]
                          : selected.filter((value) => value !== item.id),
                      );
                      if (filters.selectedOnly && !event.target.checked)
                        selectedOnly.current?.focus();
                    }}
                  />
                  <span>
                    <strong>{item.name}</strong>
                    <code>
                      {item.path === "." ? "Repository root" : item.path}
                    </code>
                    <small id={`${id}-meta-${index}`}>
                      {kinds[item.kind]} · {label(item.repository_id)}
                    </small>
                    {changed.has(item.id) && (
                      <small className="source-component-changed">
                        Source contract changed
                      </small>
                    )}
                  </span>
                </label>
                {changed.has(item.id) && (
                  <details className="source-component-contract" open>
                    <summary>Review current source contract</summary>
                    <dl>
                      <div>
                        <dt>Stack</dt>
                        <dd>
                          {item.backend} / {item.frontend}
                        </dd>
                      </div>
                      <div>
                        <dt>Checks</dt>
                        <dd>
                          {item.verification.length
                            ? item.verification.map((check, position) => (
                                <code key={position}>
                                  {JSON.stringify(check.argv)} · {check.timeout}
                                  s
                                </code>
                              ))
                            : "No checks declared"}
                        </dd>
                      </div>
                      <div>
                        <dt>Dependencies</dt>
                        <dd>
                          {item.depends_on.length
                            ? item.depends_on
                                .map(
                                  (value) =>
                                    components.find(
                                      (component) => component.id === value,
                                    )?.name ??
                                    `Unavailable component · ${value.slice(0, 8)}`,
                                )
                                .join(", ")
                            : "None declared"}
                        </dd>
                      </div>
                    </dl>
                  </details>
                )}
              </li>
            ))}
          </ul>
          {matching.length > pageSize && (
            <nav
              className="source-component-pages"
              aria-label="Component results pages"
            >
              <button
                type="button"
                className="secondary"
                disabled={disabled || page === 0}
                aria-label="Previous component results"
                onClick={() => onFiltersChange({ ...filters, page: page - 1 })}
              >
                <ChevronLeft size={16} aria-hidden="true" />
              </button>
              <span>
                Page {page + 1} of {lastPage + 1}
              </span>
              <button
                type="button"
                className="secondary"
                disabled={disabled || page === lastPage}
                aria-label="Next component results"
                onClick={() => onFiltersChange({ ...filters, page: page + 1 })}
              >
                <ChevronRight size={16} aria-hidden="true" />
              </button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}
