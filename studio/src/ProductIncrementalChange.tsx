/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { Eye, GitPullRequest, Save } from "lucide-react";
import type {
  Connection,
  CustomerProductDefinition,
  ProductSnapshot,
} from "./api";

import {
  useProductDraftField,
  type ProductDraftSession,
} from "./ProductDrafts";
import {
  currentProductChange,
  previewProductChange,
  saveProductChange,
  type ProductChangePreview,
} from "./ProductChange";
import "./ProductIncrementalChange.css";
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The contract change could not be confirmed.";
export function ProductIncrementalChange({
  session,
  connection,
  definition,
  busy,
  onBusy,
  onError,
  onSaved,
}: {
  session: ProductDraftSession;
  connection: Connection;
  definition: () => CustomerProductDefinition;
  busy: boolean;
  onBusy: (value: boolean) => void;
  onError: (value: string) => void;
  onSaved: (value: ProductSnapshot) => void;
}) {
  const [reason, setReason] = useProductDraftField(session, "changeReason", ""),
    [preview, setPreview] = useProductDraftField<ProductChangePreview | null>(
      session,
      "changePreview",
      null,
    );
  const current =
    preview && currentProductChange(session, preview) ? preview : null;
  const featureNames = (ids: string[]) =>
    ids
      .map(
        (id) =>
          current?.definition.features?.find((item) => item.id === id)?.name ??
          session.base?.definition.features?.find((item) => item.id === id)
            ?.name ??
          id,
      )
      .join(", ");
  const integrationNames = (ids: string[]) =>
    ids
      .map(
        (id) =>
          current?.definition.integrations?.find((item) => item.id === id)
            ?.name ??
          session.base?.definition.integrations?.find((item) => item.id === id)
            ?.name ??
          id,
      )
      .join(", ");
  async function review() {
    if (busy) return;
    onBusy(true);
    onError("");
    setPreview(null);
    try {
      setPreview(
        await previewProductChange(session, connection, definition(), reason),
      );
    } catch (error) {
      if (session.active) onError(message(error));
    } finally {
      if (session.active) onBusy(false);
    }
  }
  async function save() {
    if (busy || !current) return;
    onBusy(true);
    onError("");
    try {
      const result = await saveProductChange(session, connection, current);
      if (session.active && !session.dirty && !session.stale) {
        setPreview(null);
        onSaved(result);
      } else if (session.active)
        onError(
          "The original change was saved. Later draft edits remain for review.",
        );
    } catch (error) {
      if (session.active) onError(message(error));
    } finally {
      if (session.active) onBusy(false);
    }
  }
  return (
    <section
      className="product-incremental-change"
      aria-label="Incremental contract change"
    >
      <div className="product-change-heading">
        <GitPullRequest size={22} />
        <div>
          <h3>Change selected contracts</h3>
          <p>
            Preview feature and integration edits against saved revision{" "}
            {session.base?.revision}. Untouched contracts stay in place.
          </p>
        </div>
      </div>
      <div className="field">
        <label htmlFor="product-change-reason">Why is this changing?</label>
        <textarea
          id="product-change-reason"
          rows={2}
          maxLength={2000}
          value={reason}
          disabled={busy || session.saveHeld}
          onChange={(event) => {
            setReason(event.target.value);
            setPreview(null);
          }}
        />
      </div>
      <p className="subtle">
        This path can append journeys, criteria and stories. Use Save definition
        for context changes or reordered entries.
      </p>
      <button
        type="button"
        className="secondary"
        disabled={busy || !reason.trim() || session.stale || session.saveHeld}
        onClick={() => void review()}
      >
        <Eye size={16} />
        Preview contract change
      </button>
      {current && (
        <div
          className="product-change-preview"
          role="region"
          aria-label="Contract change impact"
        >
          <dl className="wizard-summary">
            <div>
              <dt>Changed features</dt>
              <dd>
                {featureNames(current.impact.changed_feature_ids) || "None"}
              </dd>
            </div>
            <div>
              <dt>Unchanged features</dt>
              <dd>
                {featureNames(current.impact.unchanged_feature_ids) || "None"}
              </dd>
            </div>
            <div>
              <dt>Changed integrations</dt>
              <dd>
                {integrationNames(current.impact.changed_integration_ids) ||
                  "None"}
              </dd>
            </div>
            <div>
              <dt>Affected journeys</dt>
              <dd>
                {current.impact.affected_journeys.join(", ") || "None recorded"}
              </dd>
            </div>
            <div>
              <dt>Regression criteria</dt>
              <dd>
                {current.impact.regression_acceptance_refs
                  .map(
                    (ref) =>
                      current.definition.acceptance[
                        Number(ref.replace("acceptance-", "")) - 1
                      ] ?? ref,
                  )
                  .join(", ") || "None recorded"}
              </dd>
            </div>
            <div>
              <dt>Reviews required</dt>
              <dd>{current.impact.reviews_required.join(", ")}</dd>
            </div>
          </dl>
          <p>
            Saving creates a contract revision and clears its reviews.
            Implementation has not started. It does not start a run or contact
            these systems.
          </p>
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => void save()}
          >
            <Save size={16} />
            Save contract change
          </button>
        </div>
      )}
    </section>
  );
}
