/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  Children,
  cloneElement,
  isValidElement,
  type HTMLAttributes,
  type ReactElement,
  type ReactNode,
  type Ref,
} from "react";
import { Check } from "lucide-react";
import "./FormFoundation.css";

export function StepIndicator({
  ordinal,
  state,
}: {
  ordinal: number;
  state: "current" | "completed" | "visited" | "upcoming";
}) {
  return (
    <span className="step-number" aria-label={`Step ${ordinal}, ${state}`}>
      <span>{ordinal}</span>
      {state === "completed" && (
        <Check className="step-completion" size={12} aria-hidden="true" />
      )}
    </span>
  );
}
export function ProgressHeader({
  title,
  description,
  action,
  headingRef,
}: {
  title: string;
  description: string;
  action?: ReactNode;
  headingRef?: Ref<HTMLHeadingElement>;
}) {
  return (
    <div className="wizard-heading form-stack">
      <h2 ref={headingRef} tabIndex={-1}>
        {title}
      </h2>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function FormField({
  id,
  label,
  hint,
  children,
  className = "",
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const hintId = `${id}-hint`;
  return (
    <div className={`field form-stack ${className}`.trim()}>
      <label htmlFor={id}>{label}</label>
      {Children.map(children, (child) => {
        if (
          !hint ||
          !isValidElement(child) ||
          typeof child.type !== "string" ||
          !["input", "select", "textarea"].includes(child.type)
        )
          return child;
        const control = child as ReactElement<HTMLAttributes<HTMLElement>>;
        return cloneElement(control, {
          "aria-describedby": [control.props["aria-describedby"], hintId]
            .filter(Boolean)
            .join(" "),
        });
      })}
      {hint && <small id={hintId}>{hint}</small>}
    </div>
  );
}
export function ScrollRegion({
  children,
  label = "Form fields",
  bounded = false,
}: {
  children: ReactNode;
  label?: string;
  bounded?: boolean;
}) {
  if (!bounded) return <div className="wizard-field-scroll">{children}</div>;
  return (
    <div className="form-scroll-region form-grid">
      <div
        className="wizard-field-scroll"
        role="region"
        aria-label={label}
        tabIndex={0}
      >
        {children}
      </div>
    </div>
  );
}
