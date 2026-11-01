/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  ArrowRight,
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  GitBranch,
  Rows3,
} from "lucide-react";
import { safeExternalLink } from "./FeatureLinks";
import type {
  DashboardBucket,
  DashboardRun,
  DashboardSnapshot,
} from "./FactoryDashboard";

export const bucketLabels: Record<DashboardBucket, string> = {
  active: "Recorded running",
  queued: "Queued",
  needs_action: "Needs action",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  expired: "Expired",
  quarantined: "Quarantined",
  unknown: "Unknown state",
};

export function FactoryRunLinks({ run }: { run: DashboardRun }) {
  const issue = safeExternalLink(run.issue_context?.url),
    pull = safeExternalLink(run.pr_url);
  return (
    <dl className="factory-source-links">
      <div>
        <dt>
          <GitBranch size={15} />
          Branch
        </dt>
        <dd>{run.branch ? <code>{run.branch}</code> : "Not recorded"}</dd>
      </div>
      {run.issue_context && (
        <div>
          <dt>Source ticket</dt>
          <dd>
            {issue ? (
              <a href={issue} target="_blank" rel="noopener noreferrer">
                {run.issue_context.number !== null
                  ? `#${run.issue_context.number} `
                  : ""}
                {run.issue_context.title || "Open ticket"}
                <ArrowUpRight size={14} />
              </a>
            ) : (
              run.issue_context.title || "Link unavailable"
            )}
          </dd>
        </div>
      )}
      {pull && (
        <div>
          <dt>Pull request</dt>
          <dd>
            <a href={pull} target="_blank" rel="noopener noreferrer">
              Review recorded PR
              <ArrowUpRight size={14} />
            </a>
          </dd>
        </div>
      )}
    </dl>
  );
}

export function FactoryRunRecords({
  snapshot,
  board,
  bucket,
  loading,
  canPrevious,
  onBucket,
  onPrevious,
  onNext,
  onFocus,
  onOpen,
}: {
  snapshot: DashboardSnapshot;
  board: boolean;
  bucket: DashboardBucket | null;
  loading: boolean;
  canPrevious: boolean;
  onBucket: (value: DashboardBucket | null) => void;
  onPrevious: () => void;
  onNext: () => void;
  onFocus: (run: DashboardRun) => void;
  onOpen: (id: string) => void;
}) {
  const entries = Object.entries(bucketLabels) as [DashboardBucket, string][];
  const total = bucket ? snapshot.counts[bucket] : snapshot.counts.total;
  function record(run: DashboardRun) {
    return (
      <article className="factory-run-record" key={run.id}>
        <header>
          <div>
            <small>
              {run.association === "standalone"
                ? "Standalone repository"
                : run.association === "project_unavailable"
                  ? "Product reference unavailable"
                  : run.project_name}
            </small>
            <h4>{run.brief || "Recorded run"}</h4>
            {run.brief_truncated && (
              <span className="factory-preview-note">
                Brief preview · open the run for the full request
              </span>
            )}
          </div>
          <span className="factory-record-status">
            {run.status.replaceAll("_", " ")}
          </span>
        </header>
        <p className="factory-component-label">
          {run.component_name || run.component_id || "Component not recorded"} ·{" "}
          {run.repository_name || "Repository name unavailable"}
        </p>
        <FactoryRunLinks run={run} />
        <footer>
          <button className="quiet" onClick={() => onFocus(run)}>
            Focus run
            <Rows3 size={15} />
          </button>
          <button className="secondary" onClick={() => onOpen(run.id)}>
            Open run
            <ArrowRight size={15} />
          </button>
        </footer>
      </article>
    );
  }
  return (
    <section
      className="factory-records"
      aria-labelledby="factory-records-title"
    >
      <header className="factory-records-heading">
        <div>
          <h3 id="factory-records-title">Recorded runs</h3>
          <p>
            {snapshot.runs.items.length.toLocaleString()} shown on this page ·{" "}
            {total.toLocaleString()} in{" "}
            {bucket ? bucketLabels[bucket].toLowerCase() : "this scope"}
          </p>
        </div>
        {bucket && (
          <button className="quiet" onClick={() => onBucket(null)}>
            Show every state
          </button>
        )}
      </header>
      <div className={board ? "factory-status-board" : "factory-run-list"}>
        {board
          ? entries.map(([key, label]) => (
              <section
                className="factory-board-lane"
                key={key}
                aria-label={label}
              >
                <header>
                  <h4>{label}</h4>
                  <button
                    className="quiet"
                    aria-label={`Show ${label.toLowerCase()} runs`}
                    onClick={() => onBucket(key)}
                  >
                    {snapshot.counts[key].toLocaleString()}
                    <ChevronRight size={14} />
                  </button>
                </header>
                {snapshot.runs.items
                  .filter((run) => recordBucket(run.status) === key)
                  .map(record)}
                {snapshot.counts[key] > 0 &&
                  !snapshot.runs.items.some(
                    (run) => recordBucket(run.status) === key,
                  ) && (
                    <p>
                      No matching card is on this page. Open this state to page
                      through its records.
                    </p>
                  )}
              </section>
            ))
          : snapshot.runs.items.map(record)}
      </div>
      {!snapshot.runs.items.length && (
        <p className="factory-empty">
          No recorded runs are visible in this scope{bucket ? " and state" : ""}
          . This does not establish that other projects have no work.
        </p>
      )}
      <nav className="factory-records-pager" aria-label="Recorded run pages">
        <button
          className="quiet"
          disabled={loading || !canPrevious}
          onClick={onPrevious}
        >
          <ChevronLeft size={16} />
          Previous page
        </button>
        <button
          className="quiet"
          disabled={loading || !snapshot.runs.next_cursor}
          onClick={onNext}
        >
          Next page
          <ChevronRight size={16} />
        </button>
      </nav>
    </section>
  );
}

export function recordBucket(status: string): DashboardBucket {
  if (["queued", "pending"].includes(status)) return "queued";
  if (
    [
      "running",
      "cloning",
      "preparing",
      "generating",
      "developing",
      "executing",
      "verifying",
      "validating",
      "publishing",
      "deploying",
    ].includes(status)
  )
    return "active";
  if (
    [
      "paused",
      "interrupted",
      "needs_reconciliation",
      "awaiting_input",
    ].includes(status)
  )
    return "needs_action";
  if (["succeeded", "completed"].includes(status)) return "completed";
  if (["failed", "cancelled", "expired", "quarantined"].includes(status))
    return status as DashboardBucket;
  return "unknown";
}
