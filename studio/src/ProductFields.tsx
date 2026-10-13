/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { Plus, Trash2, ArrowDown } from "lucide-react";
import type { ChangeEvent } from "react";
import type { FunctionalFeature } from "./api";
export function ProductList({
  label,
  id,
  value,
  onChange,
  hint,
  required = true,
  maximum = 50,
  hintAfter = false,
  compact = false,
}: {
  label: string;
  id: string;
  value: string;
  onChange: (value: string) => void;
  hint: string;
  required?: boolean;
  maximum?: number;
  hintAfter?: boolean;
  compact?: boolean;
}) {
  const entries = value.split("\n");
  const itemName =
    (
      {
        "Product owners": "owner",
        "User stories": "user story",
        "Acceptance criteria": "acceptance criterion",
        "Security requirements": "security requirement",
        "Nonfunctional requirements": "nonfunctional requirement",
      } as Record<string, string>
    )[label] ?? "entry";
  const FieldControl = itemName === "owner" ? "input" : "textarea";
  const addControl = (
    <button
      className="text-button"
      type="button"
      aria-label={compact ? `Add ${itemName}` : undefined}
      title={`Add ${itemName}`}
      disabled={entries.length >= maximum}
      onClick={() => onChange([...entries, ""].join("\n"))}
    >
      <Plus size={18} />
      Add {itemName}
    </button>
  );
  return (
    <fieldset
      className={`product-list-editor${compact ? " compact-list" : ""}`}
    >
      <legend>
        {label}
        {compact && addControl}
        {!required && <span className="field-optional">Optional</span>}
      </legend>
      {!hintAfter && <p>{hint}</p>}
      {entries.map((entry, index) => (
        <div className="product-list-row" key={index}>
          <span>{index + 1}</span>
          <label className="sr-only" htmlFor={`${id}-${index}`}>
            {index ? `${label} ${index + 1}` : label}
          </label>
          <FieldControl
            id={`${id}-${index}`}
            value={entry}
            required={required && index === 0}
            maxLength={4000}
            rows={itemName === "owner" ? undefined : 3}
            onChange={(
              event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
            ) =>
              onChange(
                entries
                  .map((item, i) =>
                    i === index
                      ? event.target.value.replace(/\r?\n/g, " ")
                      : item,
                  )
                  .join("\n"),
              )
            }
          />
          <button
            className="quiet"
            type="button"
            aria-label={`Remove ${itemName} ${index + 1}`}
            disabled={entries.length === 1 && !entry}
            onClick={() =>
              onChange(entries.filter((_, i) => i !== index).join("\n"))
            }
          >
            <Trash2 size={18} />
          </button>
        </div>
      ))}
      {hintAfter && <p>{hint}</p>}
      {!compact && addControl}
    </fieldset>
  );
}
export function ProductJourneys({
  value,
  onChange,
  customer,
  features,
}: {
  value: string;
  onChange: (value: string) => void;
  customer: string;
  features: FunctionalFeature[];
}) {
  const entries = value.split("\n");
  return (
    <section className="product-journey-editor">
      <header>
        <h3>Customer journey</h3>
        <p>
          Order the steps the customer experiences. Requirements will map each
          step to behavior, failure paths and acceptance.
        </p>
      </header>
      <ol>
        {entries.map((entry, index) => {
          const related = features.filter((feature) =>
            feature.journey_refs.includes(entry),
          );
          return (
            <li key={index}>
              <div className="journey-edit-node">
                <span className="journey-number">{index + 1}</span>
                <div>
                  <label htmlFor={`product-journey-${index}`}>
                    {index ? `Journey step ${index + 1}` : "Customer journey"}
                  </label>
                  <input
                    id={`product-journey-${index}`}
                    value={entry}
                    required={index === 0}
                    maxLength={4000}
                    onChange={(event) =>
                      onChange(
                        entries
                          .map((item, i) =>
                            i === index ? event.target.value : item,
                          )
                          .join("\n"),
                      )
                    }
                  />
                  <p className="journey-context">
                    For {customer || "the customer defined in step 1"} ·{" "}
                    {related.length
                      ? `${related.length} linked feature${related.length === 1 ? "" : "s"}`
                      : "Behavior mapping pending"}
                  </p>
                  {related.length > 0 && (
                    <details>
                      <summary>Linked behavior and context</summary>
                      {related.map((feature) => (
                        <section key={feature.id}>
                          <strong>{feature.name}</strong>
                          {feature.behaviors.map((behavior) => (
                            <p key={behavior.id}>
                              Given {behavior.given || "context pending"}; when{" "}
                              {behavior.when || "action pending"}; then{" "}
                              {behavior.then || "outcome pending"}.
                            </p>
                          ))}
                        </section>
                      ))}
                    </details>
                  )}
                </div>
                <button
                  className="quiet"
                  type="button"
                  aria-label={`Remove journey step ${index + 1}`}
                  disabled={entries.length === 1 && !entry}
                  onClick={() =>
                    onChange(entries.filter((_, i) => i !== index).join("\n"))
                  }
                >
                  <Trash2 size={18} />
                </button>
              </div>
              {index < entries.length - 1 && (
                <ArrowDown className="journey-flow-arrow" size={20} />
              )}
            </li>
          );
        })}
      </ol>
      <button
        type="button"
        className="secondary"
        disabled={entries.length >= 50}
        onClick={() => onChange([...entries, ""].join("\n"))}
      >
        <Plus size={18} />
        Add journey step
      </button>
    </section>
  );
}
