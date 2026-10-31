/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  Children,
  Fragment,
  cloneElement,
  isValidElement,
  useEffect,
  useLayoutEffect,
  useId,
  useRef,
  useState,
  type FormHTMLAttributes,
  type ReactElement,
  type ReactNode,
} from "react";
import { ProgressHeader, ScrollRegion, StepIndicator } from "./FormPrimitives";
import "./Wizard.css";

export function Wizard({
  steps,
  active,
  onStep,
  title,
  description,
  children,
  guide,
  locked = false,
  className = "",
  progressMode = "validated",
  headingAction,
  variant = "steps",
  stepDescriptions,
}: {
  steps: string[];
  active: number;
  onStep: (step: number) => void;
  title: string;
  description: string;
  children: ReactNode;
  guide?: ReactNode;
  locked?: boolean;
  className?: string;
  progressMode?: "validated" | "visited";
  headingAction?: ReactNode;
  variant?: "steps" | "rail";
  stepDescriptions?: string[];
}) {
  const stepSelect = useId();
  const frame = useRef<HTMLElement>(null);
  const [bounded, setBounded] = useState(false);
  useLayoutEffect(() => {
    setBounded(!!frame.current?.closest("dialog"));
  }, []);
  const body = (node: ReactNode): ReactNode =>
    Children.map(node, (child) => {
      if (!isValidElement(child)) return child;
      if (child.type === Fragment)
        return cloneElement(
          child as ReactElement<{ children: ReactNode }>,
          {},
          body((child.props as { children: ReactNode }).children),
        );
      if (child.type !== "form") return child;
      const form = child as ReactElement<FormHTMLAttributes<HTMLFormElement>>;
      const nodes = Children.toArray(form.props.children);
      const actions = nodes.find(
        (item) =>
          isValidElement(item) &&
          String((item.props as { className?: string }).className ?? "")
            .split(" ")
            .includes("wizard-actions"),
      );
      if (!actions) return child;
      return cloneElement(
        form,
        { className: `${form.props.className ?? ""} wizard-form` },
        <>
          <ScrollRegion label={`${title} fields`} bounded={bounded}>
            {nodes.filter((item) => item !== actions)}
          </ScrollRegion>
          {isValidElement(actions)
            ? cloneElement(actions as ReactElement<{ className: string }>, {
                className: `${(actions.props as { className?: string }).className ?? ""} form-divider`,
              })
            : actions}
        </>,
      );
    });
  const directNodes = Children.toArray(children);
  const directActions = directNodes.find(
    (node) =>
      isValidElement(node) &&
      String((node.props as { className?: string }).className ?? "")
        .split(" ")
        .includes("wizard-actions"),
  );
  const content = directActions ? (
    <div className="wizard-form">
      <ScrollRegion label={`${title} fields`} bounded={bounded}>
        {body(directNodes.filter((node) => node !== directActions))}
      </ScrollRegion>
      {isValidElement(directActions)
        ? cloneElement(directActions as ReactElement<{ className: string }>, {
            className: `${(directActions.props as { className?: string }).className ?? ""} form-divider`,
          })
        : directActions}
    </div>
  ) : (
    body(children)
  );
  const heading = useRef<HTMLHeadingElement>(null);
  const previous = useRef({ active, title });
  useEffect(() => {
    if (
      active !== previous.current.active ||
      title !== previous.current.title
    ) {
      heading.current?.focus({ preventScroll: true });
      frame.current
        ?.querySelectorAll<HTMLElement>(".wizard-field-scroll, .wizard-guide")
        .forEach((element) => {
          element.scrollTop = 0;
        });
      if (!bounded) {
        const element = frame.current;
        const document = element?.closest<HTMLElement>(".product-document");
        const owner =
          document && /auto|scroll/.test(getComputedStyle(document).overflowY)
            ? document
            : element?.closest("main");
        if (owner && variant === "rail") owner.scrollTop = 0;
        else if (owner && element)
          owner.scrollTop +=
            element.getBoundingClientRect().top -
            owner.getBoundingClientRect().top -
            parseFloat(getComputedStyle(owner).paddingTop);
        const progress = element?.querySelector<HTMLElement>(".wizard-steps"),
          current = progress?.querySelector<HTMLElement>(
            '[aria-current="step"]',
          );
        if (progress && current) {
          const p = progress.getBoundingClientRect(),
            c = current.getBoundingClientRect();
          if (c.right > p.right) progress.scrollLeft += c.right - p.right + 8;
          else if (c.left < p.left) progress.scrollLeft -= p.left - c.left + 8;
        }
      }
    }
    previous.current = { active, title };
  }, [active, title, bounded, variant]);
  return (
    <section
      ref={frame}
      className={`wizard wizard-frame${variant === "rail" ? " wizard-rail" : ""} ${className}`}
      aria-label={title}
      data-layout={bounded ? "bounded" : "flow"}
    >
      <ol className="wizard-steps form-grid" aria-label="Setup progress">
        {steps.map((label, index) => (
          <li key={label}>
            <button
              type="button"
              className={
                index === active ? "current" : index < active ? "complete" : ""
              }
              aria-current={index === active ? "step" : undefined}
              disabled={locked || index > active}
              title={
                progressMode === "visited" && index < active
                  ? `${label} · visited`
                  : label
              }
              onClick={() => onStep(index)}
            >
              <StepIndicator
                ordinal={index + 1}
                state={
                  index === active
                    ? "current"
                    : index < active
                      ? progressMode === "validated"
                        ? "completed"
                        : "visited"
                      : "upcoming"
                }
              />
              <span className="wizard-step-label">
                {label}
                {variant === "rail" && stepDescriptions?.[index] && (
                  <small>{stepDescriptions[index]}</small>
                )}
              </span>
            </button>
          </li>
        ))}
      </ol>
      {variant === "rail" && (
        <div className="wizard-mobile-progress">
          <label htmlFor={stepSelect}>
            Step {active + 1} of {steps.length}
          </label>
          <select
            id={stepSelect}
            aria-label="Setup step"
            value={active}
            disabled={locked}
            onChange={(event) => onStep(Number(event.target.value))}
          >
            {steps.map((label, index) => (
              <option key={label} value={index} disabled={index > active}>
                {index + 1}. {label}
                {index < active
                  ? progressMode === "validated"
                    ? " · completed"
                    : " · visited"
                  : ""}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className={"wizard-body" + (guide ? " has-guide" : "")}>
        <div className="wizard-content">
          <ProgressHeader
            title={title}
            description={description}
            action={headingAction}
            headingRef={heading}
          />
          {content}
        </div>
        {guide && <aside className="wizard-guide">{guide}</aside>}
      </div>
    </section>
  );
}
