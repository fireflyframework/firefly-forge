/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { Plus, Trash2, Workflow } from "lucide-react";
import type { ProductProcess } from "./api";
const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
export function ProcessEditor({
  value,
  journeys,
  acceptance,
  onChange,
}: {
  value: ProductProcess[];
  journeys: string[];
  acceptance: string[];
  onChange: (items: ProductProcess[]) => void;
}) {
  function update(index: number, change: Partial<ProductProcess>) {
    onChange(
      value.map((item, position) =>
        position === index ? { ...item, ...change } : item,
      ),
    );
  }
  function add() {
    let number = value.length + 1;
    while (value.some((item) => item.id === `process-${number}`)) number++;
    onChange([
      ...value,
      {
        id: `process-${number}`,
        name: "",
        actor: "",
        trigger: "",
        journey_refs: [],
        acceptance_refs: [],
        steps: [
          {
            id: "step-1",
            action: "",
            owner: "",
            inputs: [],
            outputs: [],
            failures: [],
          },
        ],
      },
    ]);
  }
  return (
    <section className="process-editor integration-section">
      <div className="feature-toolbar">
        <div>
          <h3>
            <Workflow size={24} />
            Functional processes
          </h3>
          <p>
            Connect each journey to actors, inputs, outcomes and failure paths.
            API-only process references require a complete workflow.
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          onClick={add}
          disabled={value.length >= 20}
        >
          <Plus size={18} />
          Add process
        </button>
      </div>
      {value.map((process, index) => (
        <details key={process.id} open className="feature-record">
          <summary>
            {index + 1}. {process.name || "Untitled process"}
          </summary>
          <div className="two-columns">
            {(
              [
                ["name", "Process name"],
                ["actor", "Actor"],
                ["trigger", "Trigger"],
              ] as const
            ).map(([key, label]) => (
              <div className="field" key={key}>
                <label htmlFor={`${process.id}-${key}`}>{label}</label>
                <input
                  id={`${process.id}-${key}`}
                  value={process[key]}
                  maxLength={key === "trigger" ? 2000 : 400}
                  onChange={(event) =>
                    update(index, { [key]: event.target.value })
                  }
                />
              </div>
            ))}
          </div>
          {(process.journey_refs.some((item) => !journeys.includes(item)) ||
            process.acceptance_refs.some(
              (item) =>
                !acceptance[Number(item.replace("acceptance-", "")) - 1],
            )) && (
            <div className="alert" role="alert">
              <p>This process references edited or removed definition items.</p>
              <button
                type="button"
                className="secondary"
                onClick={() =>
                  update(index, {
                    journey_refs: process.journey_refs.filter((item) =>
                      journeys.includes(item),
                    ),
                    acceptance_refs: process.acceptance_refs.filter((item) =>
                      Boolean(
                        acceptance[Number(item.replace("acceptance-", "")) - 1],
                      ),
                    ),
                  })
                }
              >
                Remove outdated process links
              </button>
            </div>
          )}
          <fieldset>
            <legend>Journey links</legend>
            {journeys.map((journey) => (
              <label className="check-choice" key={journey}>
                <input
                  type="checkbox"
                  checked={process.journey_refs.includes(journey)}
                  onChange={(event) =>
                    update(index, {
                      journey_refs: event.target.checked
                        ? [...process.journey_refs, journey]
                        : process.journey_refs.filter(
                            (item) => item !== journey,
                          ),
                    })
                  }
                />
                {journey}
              </label>
            ))}
          </fieldset>
          {process.steps.map((step, stepIndex) => (
            <article className="process-step" key={step.id}>
              <h4>Step {stepIndex + 1}</h4>
              <div className="two-columns">
                {(
                  ["action", "owner", "inputs", "outputs", "failures"] as const
                ).map((key) => (
                  <div className="field" key={key}>
                    <label htmlFor={`${process.id}-${step.id}-${key}`}>
                      {
                        {
                          action: "Action",
                          owner: "Step owner",
                          inputs: "Inputs",
                          outputs: "Outputs",
                          failures: "Failure paths",
                        }[key]
                      }
                    </label>
                    <textarea
                      id={`${process.id}-${step.id}-${key}`}
                      rows={key === "action" || key === "owner" ? 2 : 3}
                      value={
                        Array.isArray(step[key])
                          ? step[key].join("\n")
                          : step[key]
                      }
                      onChange={(event) =>
                        update(index, {
                          steps: process.steps.map((item, position) =>
                            position === stepIndex
                              ? {
                                  ...item,
                                  [key]:
                                    key === "action" || key === "owner"
                                      ? event.target.value
                                      : lines(event.target.value),
                                }
                              : item,
                          ),
                        })
                      }
                    />
                    {key !== "action" && key !== "owner" && (
                      <small>One item per line.</small>
                    )}
                  </div>
                ))}
              </div>
              {process.steps.length > 1 && (
                <button
                  type="button"
                  className="quiet"
                  onClick={() =>
                    update(index, {
                      steps: process.steps.filter(
                        (_, position) => position !== stepIndex,
                      ),
                    })
                  }
                >
                  <Trash2 size={16} />
                  Remove step {stepIndex + 1}
                </button>
              )}
            </article>
          ))}
          <button
            type="button"
            className="secondary"
            disabled={process.steps.length >= 12}
            onClick={() => {
              let number = process.steps.length + 1;
              while (process.steps.some((item) => item.id === `step-${number}`))
                number++;
              update(index, {
                steps: [
                  ...process.steps,
                  {
                    id: `step-${number}`,
                    action: "",
                    owner: "",
                    inputs: [],
                    outputs: [],
                    failures: [],
                  },
                ],
              });
            }}
          >
            <Plus size={16} />
            Add process step
          </button>
          <fieldset>
            <legend>Acceptance links</legend>
            {acceptance.map((criterion, criterionIndex) => {
              const id = `acceptance-${criterionIndex + 1}`;
              return (
                <label className="check-choice" key={id}>
                  <input
                    type="checkbox"
                    checked={process.acceptance_refs.includes(id)}
                    onChange={(event) =>
                      update(index, {
                        acceptance_refs: event.target.checked
                          ? [...process.acceptance_refs, id]
                          : process.acceptance_refs.filter(
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
          <button
            type="button"
            className="quiet"
            onClick={() =>
              onChange(value.filter((_, position) => position !== index))
            }
          >
            <Trash2 size={16} />
            Remove process
          </button>
        </details>
      ))}
    </section>
  );
}
