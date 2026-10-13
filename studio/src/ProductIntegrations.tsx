/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import { Network, Plus, Trash2 } from "lucide-react";
import type { FunctionalFeature, ProductIntegration } from "./api";
import "./ProductIntegrations.css";
const lines = (text: string) =>
  text
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
export function integrationReadiness(
  items: ProductIntegration[],
  journeys: string[],
  features: FunctionalFeature[],
) {
  for (const item of items) {
    if (!item.name.trim())
      return "Name every system, or remove the unfinished entry.";
    if (
      item.kind === "owned" &&
      !/^[a-z][a-z0-9-]{0,63}$/.test(item.service_ref ?? "")
    )
      return `Choose a service reference for ${item.name}.`;
    if (item.protocol === "http" || item.endpoint) {
      try {
        const url = new URL(item.endpoint ?? "");
        if (
          url.protocol !== "https:" ||
          url.username ||
          url.password ||
          url.search ||
          url.hash
        )
          throw Error();
      } catch {
        return `${item.name}: enter an HTTPS address without credentials or query parameters.`;
      }
    }
    if (
      !item.feature_refs.length ||
      item.feature_refs.some((id) => !features.some((f) => f.id === id)) ||
      !item.journey_refs.length ||
      item.journey_refs.some((j) => !journeys.includes(j))
    )
      return `${item.name}: link the system to an existing feature and journey.`;
    if (
      !item.inputs.length ||
      !item.outputs.length ||
      [...item.inputs, ...item.outputs].some(
        (field) => !/^[A-Za-z][A-Za-z0-9_.-]{0,99}$/.test(field.name),
      ) ||
      !item.failure_paths.length
    )
      return `${item.name}: describe the incoming information, expected response, and what happens when it fails.`;
    if (
      item.authentication.method !== "none" &&
      ["api_key", "oauth2", "mtls"].includes(item.authentication.method) &&
      !item.authentication.secret_refs.length
    )
      return `${item.name}: add a named credential reference in Connection details.`;
    if (
      item.authentication.secret_refs.some(
        (ref) =>
          !/^secret:\/\/[a-z][a-z0-9-]{0,63}\/[A-Za-z][A-Za-z0-9_-]{0,99}$/.test(
            ref,
          ),
      )
    )
      return `${item.name}: use named credential references; do not paste secrets.`;
    if (item.retry.max_attempts > 1 && !item.retry.retry_on.length)
      return `${item.name}: choose when a retry is allowed.`;
    if (
      item.side_effects &&
      item.retry.max_attempts > 1 &&
      item.idempotency.mode !== "required"
    )
      return `${item.name}: retrying changes needs a required request identifier.`;
    if (
      item.idempotency.mode !== "not_applicable" &&
      !item.idempotency.key_field
    )
      return `${item.name}: name the request identifier in Connection details.`;
  }
  return "";
}
export function ProductIntegrations({
  value,
  journeys,
  features,
  onChange,
}: {
  value: ProductIntegration[];
  journeys: string[];
  features: FunctionalFeature[];
  onChange: (items: ProductIntegration[]) => void;
}) {
  const [selected, setSelected] = useState(value[0]?.id ?? "");
  const active = value.find((item) => item.id === selected) ?? value[0];
  function update(change: Partial<ProductIntegration>) {
    if (active)
      onChange(
        value.map((item) =>
          item.id === active.id ? { ...item, ...change } : item,
        ),
      );
  }
  function add() {
    let number = value.length + 1;
    while (value.some((item) => item.id === `system-${number}`)) number++;
    const id = `system-${number}`;
    onChange([
      ...value,
      {
        id,
        name: "",
        kind: "third_party",
        protocol: "http",
        endpoint: null,
        service_ref: null,
        authentication: { method: "none", secret_refs: [] },
        inputs: [],
        outputs: [],
        failure_paths: [],
        retry: { max_attempts: 1, backoff_seconds: 0, retry_on: [] },
        idempotency: {
          mode: "not_applicable",
          key_location: "header",
          key_field: null,
        },
        side_effects: false,
        feature_refs: [],
        journey_refs: [],
      },
    ]);
    setSelected(id);
  }
  const fields = (direction: "inputs" | "outputs", title: string) => (
    <section className="integration-contract-fields">
      <h4>{title}</h4>
      {active?.[direction].map((field, index) => (
        <article key={index}>
          <div className="field">
            <label htmlFor={`system-${direction}-${index}-name`}>
              Information name
            </label>
            <input
              id={`system-${direction}-${index}-name`}
              value={field.name}
              maxLength={100}
              onChange={(e) =>
                update({
                  [direction]: active[direction].map((f, i) =>
                    i === index ? { ...f, name: e.target.value } : f,
                  ),
                })
              }
            />
          </div>
          <div className="field">
            <label htmlFor={`system-${direction}-${index}-type`}>Type</label>
            <select
              id={`system-${direction}-${index}-type`}
              value={field.type}
              onChange={(e) =>
                update({
                  [direction]: active[direction].map((f, i) =>
                    i === index
                      ? { ...f, type: e.target.value as typeof f.type }
                      : f,
                  ),
                })
              }
            >
              {[
                "string",
                "integer",
                "number",
                "boolean",
                "object",
                "array",
              ].map((type) => (
                <option key={type} value={type}>
                  {
                    {
                      string: "Text",
                      integer: "Whole number",
                      number: "Number",
                      boolean: "Yes / no",
                      object: "Structured record",
                      array: "List",
                    }[type]
                  }
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="quiet"
            aria-label={`Remove ${direction} field ${index + 1}`}
            onClick={() =>
              update({
                [direction]: active[direction].filter((_, i) => i !== index),
              })
            }
          >
            <Trash2 size={18} />
          </button>
        </article>
      ))}
      <button
        type="button"
        className="text-button"
        disabled={(active?.[direction].length ?? 0) >= 50}
        onClick={() =>
          active &&
          update({
            [direction]: [
              ...active[direction],
              { name: "", type: "string", required: true, description: "" },
            ],
          })
        }
      >
        <Plus size={18} />
        Add {direction === "inputs" ? "incoming field" : "response field"}
      </button>
    </section>
  );
  return (
    <section className="product-integrations">
      <header className="feature-toolbar">
        <div>
          <h3>
            <Network size={24} />
            Systems this product uses
          </h3>
          <p>
            Describe the information exchanged and the journeys that depend on
            it. This saves a contract; it does not connect a live service.
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={value.length >= 50}
          onClick={add}
        >
          <Plus size={18} />
          Add a system
        </button>
      </header>
      {!active ? (
        <div className="integration-empty">
          <p>
            No systems added. Continue if this product operates independently.
          </p>
        </div>
      ) : (
        <div className="integration-inventory-layout">
          <nav aria-label="Product systems">
            {value.map((item) => (
              <button
                type="button"
                key={item.id}
                aria-pressed={item.id === active.id}
                onClick={() => setSelected(item.id)}
              >
                {item.name || "New system"}
                <small>
                  {item.kind === "owned"
                    ? "Your service"
                    : "Third-party service"}
                </small>
              </button>
            ))}
          </nav>
          <section aria-label="Selected system contract">
            <div className="two-columns">
              <div className="field">
                <label htmlFor="system-name">System name</label>
                <input
                  id="system-name"
                  value={active.name}
                  maxLength={200}
                  onChange={(e) => update({ name: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor="system-kind">Who runs it?</label>
                <select
                  id="system-kind"
                  value={active.kind}
                  onChange={(e) =>
                    update({
                      kind: e.target.value as ProductIntegration["kind"],
                    })
                  }
                >
                  <option value="owned">Our product team</option>
                  <option value="third_party">A third party</option>
                </select>
              </div>
            </div>
            <div className="two-columns">
              <div className="field">
                <label htmlFor="system-protocol">
                  How does it exchange information?
                </label>
                <select
                  id="system-protocol"
                  value={active.protocol}
                  onChange={(e) =>
                    update({
                      protocol: e.target
                        .value as ProductIntegration["protocol"],
                    })
                  }
                >
                  <option value="http">HTTP API</option>
                  <option value="event">Events</option>
                  <option value="queue">Message queue</option>
                  <option value="database">Database</option>
                </select>
              </div>
              {active.kind === "owned" && (
                <div className="field">
                  <label htmlFor="system-service">
                    Internal service reference
                  </label>
                  <input
                    id="system-service"
                    value={active.service_ref ?? ""}
                    onChange={(e) =>
                      update({ service_ref: e.target.value || null })
                    }
                  />
                  <small>
                    Use its component reference, such as inventory-api.
                  </small>
                </div>
              )}
            </div>
            {active.protocol === "http" && (
              <div className="field">
                <label htmlFor="system-endpoint">Service address</label>
                <input
                  id="system-endpoint"
                  type="url"
                  value={active.endpoint ?? ""}
                  placeholder="https://api.example.com"
                  onChange={(e) => update({ endpoint: e.target.value || null })}
                />
                <small>
                  Address only. Credentials are configured separately.
                </small>
              </div>
            )}
            <fieldset>
              <legend>Features that use this system</legend>
              {features.length ? (
                features.map((feature) => (
                  <label className="check-choice" key={feature.id}>
                    <input
                      type="checkbox"
                      checked={active.feature_refs.includes(feature.id)}
                      onChange={(e) =>
                        update({
                          feature_refs: e.target.checked
                            ? [...active.feature_refs, feature.id]
                            : active.feature_refs.filter(
                                (id) => id !== feature.id,
                              ),
                        })
                      }
                    />
                    {feature.name || "Unnamed feature"}
                  </label>
                ))
              ) : (
                <p>
                  Add a feature in the previous step before linking this system.
                </p>
              )}
            </fieldset>
            <fieldset>
              <legend>Customer journeys that rely on it</legend>
              {journeys.map((journey) => (
                <label className="check-choice" key={journey}>
                  <input
                    type="checkbox"
                    checked={active.journey_refs.includes(journey)}
                    onChange={(e) =>
                      update({
                        journey_refs: e.target.checked
                          ? [...active.journey_refs, journey]
                          : active.journey_refs.filter(
                              (ref) => ref !== journey,
                            ),
                      })
                    }
                  />
                  {journey}
                </label>
              ))}
            </fieldset>
            {fields("inputs", "What information goes in?")}
            {fields("outputs", "What should come back?")}
            <div className="field">
              <label htmlFor="system-failures">
                What happens when the connection fails?
              </label>
              <textarea
                id="system-failures"
                rows={3}
                value={active.failure_paths.join("\n")}
                onChange={(e) =>
                  update({ failure_paths: lines(e.target.value) })
                }
              />
              <small>
                Describe the fallback, user message, or recovery action.
              </small>
            </div>
            <details className="integration-connection-details">
              <summary>Connection details</summary>
              <p>
                Optional technical configuration. Store credential references
                here, never passwords or tokens.
              </p>
              <div className="field">
                <label htmlFor="system-auth">Authentication</label>
                <select
                  id="system-auth"
                  value={active.authentication.method}
                  onChange={(e) =>
                    update({
                      authentication: {
                        ...active.authentication,
                        method: e.target
                          .value as ProductIntegration["authentication"]["method"],
                        secret_refs:
                          e.target.value === "none"
                            ? []
                            : active.authentication.secret_refs,
                      },
                    })
                  }
                >
                  <option value="none">None</option>
                  <option value="oauth2">OAuth 2</option>
                  <option value="api_key">API key</option>
                  <option value="mtls">Mutual TLS</option>
                  <option value="workload_identity">Workload identity</option>
                </select>
              </div>
              {active.authentication.method !== "none" && (
                <div className="field">
                  <label htmlFor="system-secret-refs">
                    Named credential references
                  </label>
                  <textarea
                    id="system-secret-refs"
                    value={active.authentication.secret_refs.join("\n")}
                    placeholder="secret://service-profile/API_KEY"
                    onChange={(e) =>
                      update({
                        authentication: {
                          ...active.authentication,
                          secret_refs: lines(e.target.value),
                        },
                      })
                    }
                  />
                </div>
              )}
              <label className="check-choice">
                <input
                  type="checkbox"
                  checked={active.side_effects ?? false}
                  onChange={(e) => update({ side_effects: e.target.checked })}
                />
                Requests can change data in this system
              </label>
              <div className="two-columns">
                <div className="field">
                  <label htmlFor="system-retries">Maximum attempts</label>
                  <input
                    id="system-retries"
                    type="number"
                    min={1}
                    max={5}
                    value={active.retry.max_attempts}
                    onChange={(e) =>
                      update({
                        retry: {
                          ...active.retry,
                          max_attempts: Number(e.target.value),
                        },
                      })
                    }
                  />
                </div>
                <div className="field">
                  <label htmlFor="system-backoff">
                    Wait between attempts (seconds)
                  </label>
                  <input
                    id="system-backoff"
                    type="number"
                    min={0}
                    max={3600}
                    value={active.retry.backoff_seconds}
                    onChange={(e) =>
                      update({
                        retry: {
                          ...active.retry,
                          backoff_seconds: Number(e.target.value),
                        },
                      })
                    }
                  />
                </div>
              </div>
              {active.retry.max_attempts > 1 && (
                <fieldset>
                  <legend>Retry only after</legend>
                  {Object.entries({
                    timeout: "Timeout",
                    connection_failure: "Connection failure",
                    rate_limit: "Rate limit",
                    server_error: "Server error",
                  }).map(([id, label]) => (
                    <label className="check-choice" key={id}>
                      <input
                        type="checkbox"
                        checked={active.retry.retry_on.includes(id)}
                        onChange={(e) =>
                          update({
                            retry: {
                              ...active.retry,
                              retry_on: e.target.checked
                                ? [...active.retry.retry_on, id]
                                : active.retry.retry_on.filter(
                                    (item) => item !== id,
                                  ),
                            },
                          })
                        }
                      />
                      {label}
                    </label>
                  ))}
                </fieldset>
              )}
              <div className="field">
                <label htmlFor="system-idempotency">Request identifier</label>
                <select
                  id="system-idempotency"
                  value={active.idempotency.mode}
                  onChange={(e) =>
                    update({
                      idempotency: {
                        ...active.idempotency,
                        mode: e.target
                          .value as ProductIntegration["idempotency"]["mode"],
                        key_field:
                          e.target.value === "not_applicable"
                            ? null
                            : active.idempotency.key_field,
                      },
                    })
                  }
                >
                  <option value="not_applicable">Not applicable</option>
                  <option value="optional">Optional</option>
                  <option value="required">
                    Required to prevent duplicate changes
                  </option>
                </select>
              </div>
              {active.idempotency.mode !== "not_applicable" && (
                <div className="two-columns">
                  <div className="field">
                    <label htmlFor="system-key">Identifier field</label>
                    <input
                      id="system-key"
                      value={active.idempotency.key_field ?? ""}
                      onChange={(e) =>
                        update({
                          idempotency: {
                            ...active.idempotency,
                            key_field: e.target.value || null,
                          },
                        })
                      }
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="system-key-location">
                      Identifier location
                    </label>
                    <select
                      id="system-key-location"
                      value={active.idempotency.key_location}
                      onChange={(e) =>
                        update({
                          idempotency: {
                            ...active.idempotency,
                            key_location: e.target.value as "header" | "body",
                          },
                        })
                      }
                    >
                      <option value="header">Request header</option>
                      <option value="body">Request body</option>
                    </select>
                  </div>
                </div>
              )}
            </details>
            <button
              type="button"
              className="text-button"
              onClick={() =>
                onChange(value.filter((item) => item.id !== active.id))
              }
            >
              <Trash2 size={18} />
              Remove this system
            </button>
          </section>
        </div>
      )}
    </section>
  );
}
