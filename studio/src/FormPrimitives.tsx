/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  Children,
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type HTMLAttributes,
  type ReactElement,
  type ReactNode,
  type Ref,
  type RefObject,
} from "react";
import { ArrowDown, ArrowUp, Check } from "lucide-react";
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
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
}) {
  const hintId = `${id}-hint`;
  return (
    <div className="field form-stack">
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
function useScrollState(
  ref: RefObject<HTMLDivElement | null>,
  selector?: string,
) {
  const [state, setState] = useState({
    overflow: false,
    above: false,
    below: false,
  });
  useEffect(() => {
    const element = selector
      ? document.querySelector<HTMLElement>(selector)
      : ref.current;
    if (!element) return;
    let pending = 0;
    const measure = () => {
      pending = 0;
      const overflow =
        /auto|scroll/.test(getComputedStyle(element).overflowY) &&
        element.scrollHeight > element.clientHeight + 1;
      const next = {
        overflow,
        above: overflow && element.scrollTop > 1,
        below:
          overflow &&
          element.scrollTop + element.clientHeight < element.scrollHeight - 1,
      };
      setState((prior) =>
        prior.overflow === next.overflow &&
        prior.above === next.above &&
        prior.below === next.below
          ? prior
          : next,
      );
    };
    const schedule = () => {
      if (!pending) pending = requestAnimationFrame(measure);
    };
    const resize = new ResizeObserver(schedule);
    const observe = () => {
      resize.observe(element);
      Array.from(element.children).forEach((child) => resize.observe(child));
      schedule();
    };
    const mutations = new MutationObserver(observe);
    mutations.observe(element, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    element.addEventListener("scroll", schedule, { passive: true });
    observe();
    return () => {
      resize.disconnect();
      mutations.disconnect();
      element.removeEventListener("scroll", schedule);
      cancelAnimationFrame(pending);
    };
  }, [ref, selector]);
  return state;
}
function ScrollStatus({
  state,
  page = false,
  id,
  onStart,
}: {
  state: { overflow: boolean; above: boolean; below: boolean };
  page?: boolean;
  id?: string;
  onStart?: () => void;
}) {
  return (
    <p
      id={id}
      className={`form-scroll-status${page ? " page-scroll-status" : ""}`}
      hidden={!state.overflow}
    >
      {state.below ? (
        <ArrowDown size={14} aria-hidden="true" />
      ) : (
        <ArrowUp size={14} aria-hidden="true" />
      )}
      <span>
        {page ? "Page" : "Form"}:{" "}
        {state.below
          ? state.above
            ? "more content above and below"
            : "more content below"
          : "end of content; scroll up to review"}
      </span>
      {page && state.overflow && (
        <button
          type="button"
          className="text-button"
          disabled={!state.above}
          onClick={onStart}
        >
          Back to section start
        </button>
      )}
    </p>
  );
}
export function ScrollRegion({
  children,
  label = "Form fields",
}: {
  children: ReactNode;
  label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null),
    id = useId(),
    state = useScrollState(ref);
  return (
    <div className="form-scroll-region form-grid">
      <ScrollStatus state={state} id={id} />
      <div
        ref={ref}
        className="wizard-field-scroll"
        role="region"
        aria-label={label}
        aria-describedby={state.overflow ? id : undefined}
        tabIndex={state.overflow ? 0 : undefined}
      >
        {children}
      </div>
    </div>
  );
}
export function PageScrollStatus() {
  const ref = useRef<HTMLDivElement>(null),
    state = useScrollState(ref, ".workspace-main > main");
  return (
    <ScrollStatus
      state={state}
      page
      onStart={() => {
        const main = document.querySelector<HTMLElement>(
          ".workspace-main > main",
        );
        if (!main) return;
        const section = main.querySelector<HTMLElement>(
          ".wizard-frame, .consumed-terminal-reconciliation",
        );
        if (section)
          main.scrollTop +=
            section.getBoundingClientRect().top -
            main.getBoundingClientRect().top -
            parseFloat(getComputedStyle(main).paddingTop);
        else main.scrollTop = 0;
        (
          section?.querySelector<HTMLElement>("h2[tabindex], h4[tabindex]") ??
          main
        ).focus({ preventScroll: true });
      }}
    />
  );
}
