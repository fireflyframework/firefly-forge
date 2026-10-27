/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import type {
  FrameworkBudget,
  FrameworkOperation,
} from "./FrameworkKnowledgeData";

export function FrameworkOperationControls({
  operation,
  busy,
  onRenew,
  onCancel,
  onResume,
}: {
  operation: FrameworkOperation;
  busy: boolean;
  onRenew?: (total: FrameworkBudget) => void;
  onCancel?: () => void;
  onResume?: () => void;
}) {
  const [error, setError] = useState("");
  const totals = {
    requests: Math.ceil(
      operation.spent_budget.requests + operation.remaining_budget.requests,
    ),
    bytes: Math.ceil(
      operation.spent_budget.bytes + operation.remaining_budget.bytes,
    ),
    seconds: Math.ceil(
      operation.spent_budget.seconds + operation.remaining_budget.seconds,
    ),
  };
  return (
    <div className="framework-operation-controls">
      {onRenew && operation.allowed_actions.includes("renew_budget") && (
        <details>
          <summary>Review work allowance</summary>
          <p>
            Enter new total allowances for this same operation. Existing spent
            usage and receipts remain.
          </p>
          <p>
            Already spent: {operation.spent_budget.requests} requests,{" "}
            {operation.spent_budget.bytes.toLocaleString()} bytes,{" "}
            {operation.spent_budget.seconds} seconds.
          </p>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setError("");
              const form = new FormData(event.currentTarget);
              const total = {
                requests: Number(form.get("requests")),
                bytes: Number(form.get("bytes")),
                seconds: Number(form.get("seconds")),
              };
              if (
                Object.values(total).some(
                  (value) => !Number.isSafeInteger(value) || value <= 0,
                )
              ) {
                setError("Enter finite positive whole-number totals.");
                return;
              }
              onRenew(total);
            }}
          >
            <div className="framework-budget-fields">
              {(["requests", "bytes", "seconds"] as const).map((name) => (
                <div className="field" key={name}>
                  <label
                    htmlFor={`framework-budget-${operation.request_id}-${name}`}
                  >
                    Total {name}
                  </label>
                  <input
                    id={`framework-budget-${operation.request_id}-${name}`}
                    name={name}
                    type="number"
                    min={1}
                    step={1}
                    required
                    defaultValue={totals[name]}
                    disabled={busy}
                  />
                </div>
              ))}
            </div>
            <p className="subtle">
              This review is bound to the displayed operation revision. Read the
              operation again if another worker changes it.
            </p>
            <button disabled={busy}>Apply total allowance</button>
          </form>
          {error && <p role="alert">{error}</p>}
        </details>
      )}
      {onResume && operation.allowed_actions.includes("resume") && (
        <div className="framework-control-review">
          <p>
            Resume remaining work on this original import. Completed source
            receipts and spent usage remain.
          </p>
          {operation.uncertain_sources > 0 && (
            <p>Observe uncertain submissions before resuming.</p>
          )}
          <button
            className="secondary"
            disabled={busy || operation.uncertain_sources > 0}
            onClick={onResume}
          >
            Resume reviewed import
          </button>
        </div>
      )}
      {onCancel && operation.allowed_actions.includes("cancel") && (
        <div className="framework-control-review">
          <p>
            Cancel remaining work while preserving submitted sources, receipts
            and spent usage. {operation.uncertain_sources} uncertain sources
            remain recorded until observed.
          </p>
          <button className="secondary" disabled={busy} onClick={onCancel}>
            Cancel remaining work
          </button>
        </div>
      )}
    </div>
  );
}
