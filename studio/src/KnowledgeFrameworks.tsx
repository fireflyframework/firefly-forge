/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { safeExternalLink } from "./FeatureLinks";
import { useState } from "react";
import { Check, Copy, ExternalLink, RefreshCw } from "lucide-react";
import { formatKnowledgeTimestamp } from "./FrameworkKnowledgeData";
import { FrameworkOperationControls } from "./FrameworkOperationControls";
import { FrameworkGenerationReview } from "./FrameworkGenerationReview";
import type { Connection } from "./api";
import type { FrameworkBudget } from "./FrameworkKnowledgeData";
import type {
  FrameworkCorpus,
  FrameworkCounts,
  FrameworkDiscovery,
  FrameworkStatus,
} from "./FrameworkKnowledgeData";
import "./KnowledgeFrameworks.css";

const stateLabels = {
  missing: "No verified corpus",
  indexed: "Verified corpus available",
  checking: "Checking release metadata",
  update_available: "Update available",
  current: "Verified corpus available",
  refreshing: "Refresh in progress",
  needs_reconciliation: "Needs reconciliation",
  failed: "Refresh failed",
};
function Counts({ value }: { value: FrameworkCounts }) {
  return (
    <p className="framework-counts">
      {value.files.toLocaleString()} files · {value.documents.toLocaleString()}{" "}
      documents · {value.examples.toLocaleString()} examples ·{" "}
      {value.api.toLocaleString()} API files
      {value.excluded_files > 0 && (
        <> · {value.excluded_files.toLocaleString()} excluded</>
      )}
    </p>
  );
}
type RowProps = {
  value: FrameworkStatus;
  busy: boolean;
  pendingRequest?: string;
  pendingIsChange?: boolean;
  pendingIsActivation?: boolean;
  connection?: Connection;
  discovery?: FrameworkDiscovery;
  onCheck: () => void;
  onRefresh?: () => void;
  onRead?: () => void;
  onObserve?: () => void;
  onRenew?: (total: FrameworkBudget) => void;
  onCancel?: () => void;
  onResume?: () => void;
  onActivate?: (corpus: FrameworkCorpus, validationRunId: string) => void;
  onBrowse: (corpus: FrameworkCorpus) => void;
  onSearch: (corpus: FrameworkCorpus) => void;
};
export function FrameworkReleaseRow({
  value,
  busy,
  pendingRequest,
  pendingIsChange,
  pendingIsActivation,
  connection,
  discovery,
  onCheck,
  onRefresh,
  onRead,
  onObserve,
  onRenew,
  onCancel,
  onResume,
  onActivate,
  onBrowse,
  onSearch,
}: RowProps) {
  const indexed = value.current_corpus;
  const latest = value.latest_stable;
  const operation = value.active_operation;
  const ready = indexed?.state === "ready";
  const href = latest && safeExternalLink(latest.release_url);
  const latestCommitHref =
    latest &&
    safeExternalLink(
      `https://github.com/${latest.repository}/commit/${latest.commit}`,
    );
  const stateLabel =
    value.state === "current" && ready && latest
      ? indexed.framework_version === latest.framework_version &&
        indexed.commit === latest.commit
        ? "Indexed release matches latest check"
        : "Release reference differs from latest check"
      : stateLabels[value.state];
  const commitHref =
    indexed &&
    safeExternalLink(
      `https://github.com/${value.official_repository}/commit/${indexed.commit}`,
    );
  const [copyReceipt, setCopyReceipt] = useState<{
    commit: string;
    message: string;
  } | null>(null);
  const copyStatus =
    copyReceipt?.commit === indexed?.commit ? (copyReceipt?.message ?? "") : "";
  async function copyCommit() {
    if (!indexed) return;
    const commit = indexed.commit;
    try {
      await navigator.clipboard.writeText(commit);
      setCopyReceipt({ commit, message: "Copied" });
    } catch {
      setCopyReceipt({
        commit,
        message: "Copy unavailable. Select the full hash to copy it.",
      });
    }
  }
  return (
    <article
      className="framework-release-row"
      aria-labelledby={`framework-${value.framework}`}
    >
      <div className="framework-release-heading">
        <div>
          <h3 id={`framework-${value.framework}`}>{value.framework}</h3>
          <p>{stateLabel}</p>
        </div>
        {value.allowed_actions.includes("check_updates") && (
          <button
            className="secondary"
            disabled={busy || !!pendingRequest}
            onClick={onCheck}
          >
            <RefreshCw size={14} aria-hidden="true" /> Check updates
          </button>
        )}
      </div>
      <dl className="framework-version-comparison">
        <div>
          <dt>Indexed release</dt>
          <dd>{ready ? indexed.framework_version : "No verified corpus"}</dd>
          {ready && (
            <dd className="framework-version-detail">
              {indexed.release_tag}
              <br />
              Commit{" "}
              {commitHref ? (
                <a
                  href={commitHref}
                  title={indexed.commit}
                  target="_blank"
                  rel="noreferrer"
                >
                  <code>{indexed.commit.slice(0, 12)}</code>{" "}
                  <ExternalLink size={12} aria-hidden="true" />
                </a>
              ) : (
                <code>{indexed.commit.slice(0, 12)}</code>
              )}
            </dd>
          )}
        </div>
        <div>
          <dt>Latest stable release</dt>
          <dd>{latest?.framework_version ?? "Not checked"}</dd>
          <dd className="framework-version-detail">
            {latest && latestCommitHref && (
              <>
                <span>
                  Commit{" "}
                  <a
                    href={latestCommitHref}
                    title={latest.commit}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <code>{latest.commit.slice(0, 12)}</code>{" "}
                    <ExternalLink size={12} aria-hidden="true" />
                  </a>
                </span>
                <br />
              </>
            )}
            {value.checked_at ? (
              <>
                Checked{" "}
                <time dateTime={value.checked_at} title={value.checked_at}>
                  {formatKnowledgeTimestamp(value.checked_at)}
                </time>
              </>
            ) : (
              "Check official release metadata first."
            )}
            {href && (
              <>
                <br />
                <a href={href} target="_blank" rel="noreferrer">
                  Official release <ExternalLink size={12} aria-hidden="true" />
                </a>
              </>
            )}
          </dd>
        </div>
      </dl>
      {(value.discovery_error || value.discovery_retry_at) && (
        <div className="framework-discovery-status" role="status">
          {value.discovery_error && <p>{value.discovery_error}</p>}
          {value.discovery_retry_at && (
            <p>
              Next update check:{" "}
              <time
                dateTime={value.discovery_retry_at}
                title={value.discovery_retry_at}
              >
                {formatKnowledgeTimestamp(value.discovery_retry_at)}
              </time>
            </p>
          )}
        </div>
      )}
      {value.generation_target !== undefined && (
        <section
          className="framework-generation-target"
          aria-label={`${value.framework} generation default`}
        >
          <h4>Activated generation default</h4>
          {value.generation_target ? (
            <>
              <p>
                {value.generation_target.framework}{" "}
                {value.generation_target.corpus.framework_version} ·{" "}
                {value.generation_target.corpus.release_tag}
              </p>
              <p>
                For eligible new builds. Existing project dependencies and
                frozen runs keep their pins.
              </p>
              <details className="framework-provenance">
                <summary>Activation provenance</summary>
                <dl>
                  <dt>Exact corpus</dt>
                  <dd>
                    <code>{value.generation_target.corpus.corpus_ref}</code>
                  </dd>
                  <dt>Source commit</dt>
                  <dd>
                    <code>{value.generation_target.corpus.commit}</code>
                  </dd>
                  <dt>Native validation run</dt>
                  <dd>
                    <code>{value.generation_target.validation_run_id}</code>
                  </dd>
                  <dt>Validated source tree</dt>
                  <dd>
                    <code>
                      {value.generation_target.validation_source_tree}
                    </code>
                  </dd>
                  <dt>Validation digest</dt>
                  <dd>
                    <code>{value.generation_target.validation_digest}</code>
                  </dd>
                  <dt>Activated</dt>
                  <dd>
                    <time
                      dateTime={value.generation_target.activated_at}
                      title={value.generation_target.activated_at}
                    >
                      {formatKnowledgeTimestamp(
                        value.generation_target.activated_at,
                      )}
                    </time>
                  </dd>
                </dl>
              </details>
            </>
          ) : (
            <p>
              No generation default has been activated. A ready index alone does
              not select one.
            </p>
          )}
        </section>
      )}
      {connection &&
        onActivate &&
        value.allowed_actions.includes("activate_generation_target") &&
        !pendingRequest && (
          <FrameworkGenerationReview
            key={`${value.framework}:${value.revision}`}
            connection={connection}
            status={value}
            busy={busy}
            onActivate={onActivate}
          />
        )}
      {ready && (
        <>
          <Counts value={value.counts} />
          <div className="framework-actions">
            {value.allowed_actions.includes("browse") && (
              <button
                className="secondary"
                disabled={busy}
                onClick={() => onBrowse(indexed)}
              >
                Browse sources
              </button>
            )}
            {value.allowed_actions.includes("search") && (
              <button
                className="secondary"
                disabled={busy}
                onClick={() => onSearch(indexed)}
              >
                Search
              </button>
            )}
          </div>
        </>
      )}
      {discovery && onRefresh && !pendingRequest && (
        <section
          className="framework-refresh-review"
          aria-label={`${value.framework} refresh review`}
        >
          <h4>Review refresh target</h4>
          <p>
            {discovery.release.release_tag} at{" "}
            <code>{discovery.release.commit}</code>
          </p>
          <p>
            Index official references for this release. Existing project
            dependencies and frozen runs keep their pins.
          </p>
          <div className="framework-actions">
            <button
              className="primary"
              disabled={busy || !value.allowed_actions.includes("refresh")}
              onClick={onRefresh}
            >
              Start refresh
            </button>
          </div>
        </section>
      )}
      {operation && (
        <section
          className="framework-operation"
          aria-label={`${value.framework} refresh progress`}
        >
          <h4>
            Refresh target{" "}
            {operation.corpus?.framework_version ?? "awaiting inventory"}
          </h4>
          <p>
            {operation.state.replaceAll("_", " ")} · {operation.stage}
          </p>
          <Counts value={operation.counts} />
          <p>
            {operation.counts.ready_parts.toLocaleString()} of{" "}
            {operation.counts.parts.toLocaleString()} parts ready.{" "}
            {operation.uncertain_sources.toLocaleString()} uncertain sources.
          </p>
          <details>
            <summary>Operation and work budget</summary>
            <dl>
              <dt>Original operation</dt>
              <dd>
                <code>{operation.request_id}</code>
              </dd>
              <dt>Spent</dt>
              <dd>
                {operation.spent_budget.requests} requests ·{" "}
                {operation.spent_budget.bytes.toLocaleString()} bytes ·{" "}
                {operation.spent_budget.seconds} seconds
              </dd>
              <dt>Remaining</dt>
              <dd>
                {operation.remaining_budget.requests} requests ·{" "}
                {operation.remaining_budget.bytes.toLocaleString()} bytes ·{" "}
                {operation.remaining_budget.seconds} seconds
              </dd>
            </dl>
          </details>
          {operation.sanitized_error && (
            <p role="status">{operation.sanitized_error}</p>
          )}
          <div className="framework-actions">
            {onRead && !pendingIsActivation && (
              <button className="secondary" disabled={busy} onClick={onRead}>
                Read original operation
              </button>
            )}
            {onObserve && operation.allowed_actions.includes("observe") && (
              <button className="secondary" disabled={busy} onClick={onObserve}>
                Observe outcome
              </button>
            )}
          </div>
          {!pendingRequest && (
            <FrameworkOperationControls
              key={`${operation.request_id}:${operation.revision}`}
              operation={operation}
              busy={busy}
              onRenew={onRenew}
              onCancel={onCancel}
              onResume={onResume}
            />
          )}
        </section>
      )}
      {pendingRequest && (
        <section className="framework-operation" role="status">
          <h4>
            {pendingIsActivation
              ? "Unconfirmed generation activation"
              : pendingIsChange
                ? "Unconfirmed operation change"
                : "Unconfirmed refresh response"}
          </h4>
          <p>
            The response does not establish whether the write was admitted. Keep
            this request; a replacement is not submitted.
          </p>
          <p>
            <code>{pendingRequest}</code>
          </p>
          {onRead && (
            <button className="secondary" disabled={busy} onClick={onRead}>
              {pendingIsActivation
                ? "Read original activation"
                : "Read original operation"}
            </button>
          )}
        </section>
      )}
      <details className="framework-provenance">
        <summary>Release provenance</summary>
        <dl>
          <dt>Official repository</dt>
          <dd>{value.official_repository}</dd>
          {latest && (
            <>
              <dt>Latest checked commit</dt>
              <dd>
                <code>{latest.commit}</code>
              </dd>
            </>
          )}
          {indexed && (
            <>
              <dt>Source commit</dt>
              <dd>
                <code className="framework-full-commit">{indexed.commit}</code>{" "}
                <button
                  className="secondary"
                  type="button"
                  aria-label={`Copy ${value.framework} source commit`}
                  onClick={() => void copyCommit()}
                >
                  {copyStatus === "Copied" ? (
                    <Check size={14} aria-hidden="true" />
                  ) : (
                    <Copy size={14} aria-hidden="true" />
                  )}{" "}
                  {copyStatus === "Copied" ? "Copied" : "Copy"}
                </button>
                <span role="status">
                  {copyStatus !== "Copied" ? copyStatus : ""}
                </span>
              </dd>
              <dt>Corpus</dt>
              <dd>
                <code>{indexed.corpus_ref}</code>
              </dd>
              <dt>Manifest SHA-256</dt>
              <dd>
                <code>{indexed.manifest_sha256 ?? "Not finalized"}</code>
              </dd>
              <dt>License SHA-256</dt>
              <dd>
                <code>{indexed.license_sha256}</code>
              </dd>
              <dt>Workspace</dt>
              <dd>{indexed.workspace_id}</dd>
            </>
          )}
        </dl>
      </details>
    </article>
  );
}
