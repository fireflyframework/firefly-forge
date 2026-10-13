/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { FileClock, Sparkles, X } from "lucide-react";
import type {
  AIProposalProvenance,
  CustomerProductDefinition,
  ProductSavedOrigin,
} from "./api";
import { RichMarkdown } from "./RichMarkdown";
export type DraftProposal = {
  definition: CustomerProductDefinition;
  source: AIProposalProvenance;
  current: CustomerProductDefinition | null;
};
export function ProductOrigin({
  source,
  saved,
  dirty,
  revision,
  onSource,
}: {
  source: AIProposalProvenance | ProductSavedOrigin | null;
  saved: boolean;
  dirty: boolean;
  revision?: number;
  onSource: () => void;
}) {
  return (
    <div className="product-origin" role="status">
      <span>
        <Sparkles size={18} />
        {source
          ? saved && !dirty
            ? "AI-assisted definition"
            : "AI-assisted draft · review needed"
          : revision
            ? `Saved revision ${revision}`
            : "Manual draft · review needed"}
      </span>
      {source ? (
        <small>
          {source.provider} · {source.model || "model not recorded"} ·{" "}
          {source.generated_at
            ? new Date(source.generated_at).toLocaleString()
            : "time not recorded"}
        </small>
      ) : (
        <small>
          {dirty
            ? "Unsaved edits"
            : "Save creates a definition; starting a run is separate."}
        </small>
      )}
      {source?.conversation_id && (
        <button
          className="text-button"
          onClick={onSource}
          aria-label="View AI source"
          title="View AI source"
        >
          <FileClock size={16} />
          <span>View AI source</span>
        </button>
      )}
    </div>
  );
}
export function ProductProposal({
  value,
  onCancel,
  onApply,
}: {
  value: DraftProposal;
  onCancel: () => void;
  onApply: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="feature-dialog proposal-review-dialog"
      aria-label="Review AI proposal handoff"
      onCancel={onCancel}
    >
      <header>
        <div>
          <h2>Review the proposed draft</h2>
          <p>
            {value.source.provider} ·{" "}
            {value.source.model || "model not recorded"} ·{" "}
            {new Date(value.source.generated_at).toLocaleString()}
          </p>
        </div>
        <button
          className="quiet"
          aria-label="Close proposal handoff"
          onClick={onCancel}
        >
          <X size={20} />
        </button>
      </header>
      <div className="proposal-comparison">
        <p>
          Applying this proposal changes the editor. Review its behavior,
          architecture and design before saving.
        </p>
        {[
          "customer",
          "problem",
          "journey",
          "user_stories",
          "acceptance",
          "owners",
        ].map((key) => {
          const name = key as keyof CustomerProductDefinition;
          const before = value.current?.[name],
            after = value.definition[name];
          return (
            <section key={key}>
              <h3>{key.replaceAll("_", " ")}</h3>
              <div>
                <article>
                  <small>Current draft</small>
                  <RichMarkdown
                    value={
                      Array.isArray(before)
                        ? before.join("\n\n")
                        : typeof before === "string"
                          ? before
                          : "Not yet defined"
                    }
                  />
                </article>
                <article>
                  <small>Proposed draft</small>
                  <RichMarkdown
                    value={
                      Array.isArray(after)
                        ? after.join("\n\n")
                        : typeof after === "string"
                          ? after
                          : "Not yet defined"
                    }
                  />
                </article>
              </div>
            </section>
          );
        })}
        {value.current?.customer && (
          <label className="check-choice">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            Replace my current draft with this unreviewed proposal.
          </label>
        )}
        <details>
          <summary>AI source receipt</summary>
          <p>
            Conversation <code>{value.source.conversation_id}</code>
          </p>
          <p>
            Turn <code>{value.source.turn_id}</code>
          </p>
          <p>
            Proposal <code>{value.source.proposal_digest}</code>
          </p>
        </details>
      </div>
      <div className="wizard-actions">
        <button className="secondary" onClick={onCancel}>
          Keep current draft
        </button>
        <button
          className="primary"
          disabled={Boolean(value.current?.customer) && !confirmed}
          onClick={onApply}
        >
          Apply unreviewed draft
        </button>
      </div>
    </dialog>
  );
}
