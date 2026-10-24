/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { GitBranch, ExternalLink } from "lucide-react";
import { request, type Connection } from "./api";
import {
  deliveryList,
  type GitDeliveries,
  type GitDelivery,
} from "./GitDeliveryData";
import { safeExternalLink } from "./FeatureLinks";
import "./GitDeliveries.css";

const labels: Record<GitDelivery["remote_status"], string> = {
  not_requested: "Saved locally",
  publishing: "Remote delivery in progress",
  draft: "Draft review updated",
  unknown: "Remote outcome needs review",
  ready: "Review marked ready",
};
export function GitDeliveryHistory({ value }: { value: GitDeliveries }) {
  return (
    <section
      className="git-delivery-history"
      aria-label="Saved development progress"
    >
      <div className="section-heading">
        <div>
          <h2>
            <GitBranch size={20} aria-hidden="true" /> Saved development
            progress
          </h2>
          <p className="subtle">
            These checkpoints preserve work in progress. Final checks, approval
            and run completion are recorded separately.
          </p>
        </div>
        <span className="badge">{value.items.length} saved</span>
      </div>
      {!value.items.length ? (
        <p>
          {value.policy.enabled
            ? "No checkpoints have been recorded yet. The server saves progress after completed implementation rounds or a configured safe pause."
            : "This run has no intermediate checkpoints. Its recorded workflow keeps final delivery separate."}
        </p>
      ) : (
        <ol className="git-delivery-timeline">
          {[...value.items].reverse().map((item) => {
            const url = safeExternalLink(item.pr_url);
            return (
              <li key={item.request_id}>
                <div className="git-delivery-entry-heading">
                  <h3>Checkpoint {item.sequence}</h3>
                  <span className={`git-delivery-state ${item.remote_status}`}>
                    {labels[item.remote_status]}
                  </span>
                </div>
                {item.verification_status === "partial_unverified" && (
                  <p role="note">
                    <strong>Partial work · unverified.</strong> The
                    implementation remains failed; this checkpoint saves its
                    partial output. Verification and the remaining requirements
                    are still pending.
                  </p>
                )}
                <p>
                  {item.changed_file_count} changed{" "}
                  {item.changed_file_count === 1 ? "file" : "files"} ·{" "}
                  {item.task_id} · attempt {item.attempt}
                </p>
                <dl className="git-delivery-evidence">
                  <div>
                    <dt>Commit</dt>
                    <dd>
                      <code>{item.revision}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Source tree</dt>
                    <dd>
                      <code>{item.source_tree}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Branch</dt>
                    <dd>{item.branch}</dd>
                  </div>
                  <div>
                    <dt>Saved</dt>
                    <dd>
                      <time dateTime={item.created_at}>
                        {new Date(item.created_at).toLocaleString()}
                      </time>
                    </dd>
                  </div>
                </dl>
                {item.remote_status === "publishing" && (
                  <p className="subtle">
                    The local commit is recorded. A completed remote delivery
                    has not been confirmed.
                  </p>
                )}
                {item.remote_status === "unknown" && (
                  <p className="subtle">
                    Inspect the retained delivery and remote repository before
                    taking another action. A replacement push is not sent from
                    this view.
                  </p>
                )}
                {url && (
                  <a
                    className="git-delivery-link"
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {item.remote_status === "ready"
                      ? "Open review"
                      : ["unknown", "publishing"].includes(item.remote_status)
                        ? "Inspect remote review"
                        : "Open draft review"}
                    <ExternalLink size={16} aria-hidden="true" />
                  </a>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

export function RunGitDeliveries({
  connection,
  runId,
}: {
  connection: Connection;
  runId: string;
}) {
  const [value, setValue] = useState<GitDeliveries | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let reading = false;
    async function load() {
      if (reading || controller.signal.aborted) return;
      reading = true;
      try {
        const result = deliveryList(
          await request<unknown>(connection, `/runs/${runId}/git-deliveries`, {
            signal: controller.signal,
          }),
          runId,
        );
        if (!controller.signal.aborted) {
          setValue(result);
          setError("");
        }
      } catch {
        if (!controller.signal.aborted) {
          setValue(null);
          setError(
            "Saved development progress could not be read and verified. Check your access and refresh the run.",
          );
        }
      } finally {
        reading = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [connection, runId]);
  if (error)
    return (
      <section aria-label="Saved development progress">
        <h2>Saved development progress</h2>
        <p className="alert" role="alert">
          {error}
        </p>
      </section>
    );
  if (!value)
    return (
      <section aria-label="Saved development progress">
        <h2>Saved development progress</h2>
        <p role="status">Reading saved checkpoints…</p>
      </section>
    );
  return <GitDeliveryHistory value={value} />;
}
