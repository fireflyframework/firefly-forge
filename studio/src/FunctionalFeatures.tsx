/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import { Plus, Trash2, Workflow } from "lucide-react";
import type { FunctionalFeature } from "./api";
const lines = (value: string) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
export function featureReadiness(
  features: FunctionalFeature[],
  journeys: string[],
  acceptance: string[],
) {
  return (
    features.length > 0 &&
    journeys.every((journey) =>
      features.some((feature) => feature.journey_refs.includes(journey)),
    ) &&
    features.every(
      (feature) =>
        feature.name.trim() &&
        feature.journey_refs.length &&
        feature.journey_refs.every((item) => journeys.includes(item)) &&
        feature.acceptance_refs.length &&
        feature.acceptance_refs.every(
          (item) =>
            /^acceptance-[1-9][0-9]*$/.test(item) &&
            acceptance[Number(item.replace("acceptance-", "")) - 1],
        ) &&
        feature.behaviors.length &&
        feature.behaviors.every(
          (behavior) =>
            behavior.given.trim() &&
            behavior.when.trim() &&
            behavior.then.trim() &&
            behavior.failure_paths.length,
        ),
    )
  );
}
export function FunctionalFeatures({
  value,
  journeys,
  acceptance,
  onChange,
}: {
  value: FunctionalFeature[];
  journeys: string[];
  acceptance: string[];
  onChange: (features: FunctionalFeature[]) => void;
}) {
  const [journey, setJourney] = useState(journeys[0] ?? ""),
    [selected, setSelected] = useState(value[0]?.id ?? ""),
    [behaviorId, setBehaviorId] = useState("");
  const activeJourney = journeys.includes(journey)
    ? journey
    : (journeys[0] ?? "");
  const options = value.filter(
      (item) =>
        item.journey_refs.includes(activeJourney) ||
        !item.journey_refs.length ||
        !item.journey_refs.some((reference) => journeys.includes(reference)),
    ),
    feature = options.find((item) => item.id === selected) ?? options[0];
  const behavior =
    feature?.behaviors.find((item) => item.id === behaviorId) ??
    feature?.behaviors[0];
  function update(change: Partial<FunctionalFeature>) {
    if (feature)
      onChange(
        value.map((item) =>
          item.id === feature.id ? { ...item, ...change } : item,
        ),
      );
  }
  function add() {
    let number = value.length + 1;
    while (value.some((item) => item.id === `feature-${number}`)) number++;
    const id = `feature-${number}`;
    onChange([
      ...value,
      {
        id,
        name: "",
        journey_refs: activeJourney ? [activeJourney] : [],
        acceptance_refs: [],
        component_refs: [],
        behaviors: [
          {
            id: "behavior-1",
            given: "",
            when: "",
            then: "",
            failure_paths: [],
          },
        ],
      },
    ]);
    setSelected(id);
    setBehaviorId("");
  }
  return (
    <section className="functional-features integration-section">
      <div className="feature-toolbar">
        <div>
          <h3>
            <Workflow size={24} />
            Journey-to-feature mapping
          </h3>
          <p>
            Describe expected behavior and failure paths, then link each feature
            to actual acceptance criteria.
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={!journeys.length || value.length >= 100}
          onClick={add}
        >
          <Plus size={18} />
          Add feature
        </button>
      </div>
      <div className="component-editor-layout">
        <nav aria-label="Functional journeys">
          <ol>
            {journeys.map((item, index) => (
              <li key={item}>
                <button
                  type="button"
                  aria-pressed={item === activeJourney}
                  onClick={() => {
                    setJourney(item);
                    setSelected("");
                    setBehaviorId("");
                  }}
                >
                  {index + 1}. {item}
                  <small>
                    {
                      value.filter((feature) =>
                        feature.journey_refs.includes(item),
                      ).length
                    }{" "}
                    mapped features
                  </small>
                </button>
              </li>
            ))}
          </ol>
        </nav>
        <section aria-label="Selected feature mapping">
          {feature ? (
            <>
              <div className="field">
                <label htmlFor="functional-feature-choice">
                  Feature in this journey
                </label>
                <select
                  id="functional-feature-choice"
                  value={feature.id}
                  onChange={(event) => {
                    setSelected(event.target.value);
                    setBehaviorId("");
                  }}
                >
                  {options.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name || item.id}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="functional-feature-name">Feature name</label>
                <input
                  id="functional-feature-name"
                  value={feature.name}
                  maxLength={200}
                  onChange={(event) => update({ name: event.target.value })}
                />
              </div>
              {(feature.journey_refs.some((item) => !journeys.includes(item)) ||
                feature.acceptance_refs.some(
                  (item) =>
                    !acceptance[Number(item.replace("acceptance-", "")) - 1],
                )) && (
                <div className="alert" role="alert">
                  <p>
                    This feature references edited or removed definition items.
                  </p>
                  <button
                    type="button"
                    className="secondary"
                    onClick={() =>
                      update({
                        journey_refs: feature.journey_refs.filter((item) =>
                          journeys.includes(item),
                        ),
                        acceptance_refs: feature.acceptance_refs.filter(
                          (item) =>
                            Boolean(
                              acceptance[
                                Number(item.replace("acceptance-", "")) - 1
                              ],
                            ),
                        ),
                      })
                    }
                  >
                    Remove outdated feature links
                  </button>
                </div>
              )}
              <fieldset>
                <legend>Linked journeys</legend>
                {journeys.map((item) => (
                  <label className="check-choice" key={item}>
                    <input
                      type="checkbox"
                      checked={feature.journey_refs.includes(item)}
                      onChange={(event) =>
                        update({
                          journey_refs: event.target.checked
                            ? [...feature.journey_refs, item]
                            : feature.journey_refs.filter(
                                (entry) => entry !== item,
                              ),
                        })
                      }
                    />
                    {item}
                  </label>
                ))}
              </fieldset>
              <fieldset>
                <legend>Feature acceptance criteria</legend>
                {acceptance.map((criterion, index) => {
                  const id = `acceptance-${index + 1}`;
                  return (
                    <label className="check-choice" key={id}>
                      <input
                        type="checkbox"
                        checked={feature.acceptance_refs.includes(id)}
                        onChange={(event) =>
                          update({
                            acceptance_refs: event.target.checked
                              ? [...feature.acceptance_refs, id]
                              : feature.acceptance_refs.filter(
                                  (item) => item !== id,
                                ),
                          })
                        }
                      />
                      {criterion}
                    </label>
                  );
                })}
              </fieldset>
              <div className="field">
                <label htmlFor="functional-behavior-choice">
                  Expected behavior
                </label>
                <select
                  id="functional-behavior-choice"
                  value={behavior?.id ?? ""}
                  onChange={(event) => setBehaviorId(event.target.value)}
                >
                  {feature.behaviors.map((item, index) => (
                    <option key={item.id} value={item.id}>
                      Behavior {index + 1}
                    </option>
                  ))}
                </select>
              </div>
              {behavior && (
                <>
                  {(
                    [
                      ["given", "Given"],
                      ["when", "When"],
                      ["then", "Then"],
                      ["failure_paths", "Failure and exception paths"],
                    ] as const
                  ).map(([key, label]) => (
                    <div className="field" key={key}>
                      <label htmlFor={`feature-behavior-${key}`}>{label}</label>
                      <textarea
                        id={`feature-behavior-${key}`}
                        rows={3}
                        maxLength={key === "failure_paths" ? 40000 : 2000}
                        value={
                          key === "failure_paths"
                            ? behavior.failure_paths.join("\n")
                            : behavior[key]
                        }
                        onChange={(event) =>
                          update({
                            behaviors: feature.behaviors.map((item) =>
                              item.id === behavior.id
                                ? {
                                    ...item,
                                    [key]:
                                      key === "failure_paths"
                                        ? lines(event.target.value)
                                        : event.target.value,
                                  }
                                : item,
                            ),
                          })
                        }
                      />
                      {key === "failure_paths" && (
                        <small>One concrete failure outcome per line.</small>
                      )}
                    </div>
                  ))}
                </>
              )}
              <div className="feature-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={feature.behaviors.length >= 20}
                  onClick={() => {
                    let number = feature.behaviors.length + 1;
                    while (
                      feature.behaviors.some(
                        (item) => item.id === `behavior-${number}`,
                      )
                    )
                      number++;
                    const id = `behavior-${number}`;
                    update({
                      behaviors: [
                        ...feature.behaviors,
                        {
                          id,
                          given: "",
                          when: "",
                          then: "",
                          failure_paths: [],
                        },
                      ],
                    });
                    setBehaviorId(id);
                  }}
                >
                  <Plus size={16} />
                  Add behavior
                </button>
                {feature.behaviors.length > 1 && (
                  <button
                    type="button"
                    className="quiet"
                    onClick={() => {
                      update({
                        behaviors: feature.behaviors.filter(
                          (item) => item.id !== behavior.id,
                        ),
                      });
                      setBehaviorId("");
                    }}
                  >
                    <Trash2 size={16} />
                    Remove behavior
                  </button>
                )}
              </div>
              <details>
                <summary>Logical component ownership</summary>
                <div className="field">
                  <label htmlFor="functional-components">Component IDs</label>
                  <textarea
                    id="functional-components"
                    rows={2}
                    value={feature.component_refs.join("\n")}
                    onChange={(event) =>
                      update({ component_refs: lines(event.target.value) })
                    }
                  />
                  <small>
                    Optional configured product component IDs, one per line.
                    Runtime validates the selected project's scope.
                  </small>
                </div>
              </details>
              <button
                type="button"
                className="quiet"
                onClick={() =>
                  onChange(value.filter((item) => item.id !== feature.id))
                }
              >
                <Trash2 size={16} />
                Remove feature
              </button>
            </>
          ) : (
            <p>
              No feature maps to this journey yet. Add a feature to define
              expected behavior.
            </p>
          )}
        </section>
      </div>
      <p className="feature-status">
        {featureReadiness(value, journeys, acceptance)
          ? "All journeys have complete feature mappings; explicit review remains required."
          : "Functional definition pending"}
      </p>
    </section>
  );
}
