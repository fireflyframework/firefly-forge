/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, type ReactNode } from "react";
import { Check } from "lucide-react";

export function Wizard({
  steps,
  active,
  onStep,
  title,
  description,
  children,
  guide,
}: {
  steps: string[];
  active: number;
  onStep: (step: number) => void;
  title: string;
  description: string;
  children: ReactNode;
  guide?: ReactNode;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const previous = useRef(active);
  useEffect(() => {
    if (active !== previous.current) heading.current?.focus();
    previous.current = active;
  }, [active]);
  return (
    <section className="wizard" aria-label={title}>
      <ol className="wizard-steps" aria-label="Setup progress">
        {steps.map((label, index) => (
          <li key={label}>
            <button
              type="button"
              className={
                index === active ? "current" : index < active ? "complete" : ""
              }
              aria-current={index === active ? "step" : undefined}
              disabled={index > active}
              onClick={() => onStep(index)}
            >
              <span className="step-number">
                {index < active ? <Check size={16} /> : index + 1}
              </span>
              <span>{label}</span>
            </button>
          </li>
        ))}
      </ol>
      <div className={"wizard-body" + (guide ? " has-guide" : "")}>
        <div className="wizard-content">
          <div className="wizard-heading">
            <h2 ref={heading} tabIndex={-1}>
              {title}
            </h2>
            <p>{description}</p>
          </div>
          {children}
        </div>
        {guide && <aside className="wizard-guide">{guide}</aside>}
      </div>
    </section>
  );
}
