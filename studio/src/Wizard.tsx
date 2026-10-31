/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  Children,
  Fragment,
  cloneElement,
  isValidElement,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormHTMLAttributes,
  type ReactElement,
  type ReactNode,
} from "react";
import { ProgressHeader, ScrollRegion, StepIndicator } from "./FormPrimitives";

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
  pageFlow = false,
  parentLayout = false,
  headingAction,
  minimumFieldHeight = 160,
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
  pageFlow?: boolean;
  parentLayout?: boolean;
  headingAction?: ReactNode;
  minimumFieldHeight?: number;
}) {
  const frame = useRef<HTMLElement>(null);
  const [height, setHeight] = useState<number>();
  const [flow, setFlow] = useState(false);
  useEffect(() => {
    const element = frame.current;
    if (!element || pageFlow || parentLayout) return;
    const owner = element.closest("dialog") ?? element.closest("main");
    if (!owner) return;
    const measure = () => {
      const progress = element.querySelector<HTMLElement>(".wizard-steps");
      const current = progress?.querySelector<HTMLElement>(
        '[aria-current="step"]',
      );
      if (progress && current && progress.scrollWidth > progress.clientWidth) {
        const p = progress.getBoundingClientRect(),
          c = current.getBoundingClientRect();
        if (c.right > p.right) progress.scrollLeft += c.right - p.right + 12;
        else if (c.left < p.left) progress.scrollLeft -= p.left - c.left + 12;
      }
      const rect = element.getBoundingClientRect();
      const parent = owner.getBoundingClientRect();
      const modal = owner.tagName === "DIALOG";
      if (modal) {
        setHeight(undefined);
        setFlow(false);
        return;
      }
      const available = owner.clientHeight;
      const clearance = Number.parseFloat(
        getComputedStyle(owner).paddingBottom,
      );
      const offset = rect.top - parent.top + owner.scrollTop;
      const remaining = Math.max(0, available - offset - clearance);
      const content = element.querySelector<HTMLElement>(".wizard-content");
      const padding = content ? getComputedStyle(content) : null;
      const chrome = [
        ".wizard-steps",
        ".wizard-heading",
        ".wizard-actions",
        ".form-scroll-status",
      ]
        .map((selector) => {
          const part = element.querySelector<HTMLElement>(selector);
          if (!part) return 0;
          const style = getComputedStyle(part);
          return (
            part.getBoundingClientRect().height +
            parseFloat(style.marginTop) +
            parseFloat(style.marginBottom)
          );
        })
        .reduce((sum, value) => sum + value, 0);
      const contentPadding = padding
        ? parseFloat(padding.paddingTop) + parseFloat(padding.paddingBottom)
        : 0;
      setHeight(remaining);
      setFlow(remaining < chrome + contentPadding + minimumFieldHeight);
    };
    measure();
    let pending = 0;
    const schedule = () => {
      if (!pending)
        pending = requestAnimationFrame(() => {
          pending = 0;
          measure();
        });
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(owner);
    let ancestor = element.parentElement;
    while (ancestor && ancestor !== owner) {
      observer.observe(ancestor);
      ancestor = ancestor.parentElement;
    }
    const mutations = new MutationObserver(schedule);
    mutations.observe(owner, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    window.addEventListener("resize", schedule);
    return () => {
      observer.disconnect();
      mutations.disconnect();
      cancelAnimationFrame(pending);
      window.removeEventListener("resize", schedule);
    };
  }, [pageFlow, parentLayout, minimumFieldHeight]);
  useEffect(() => {
    const element = frame.current;
    const owner = element?.closest("main");
    if (
      height === undefined ||
      !element ||
      !owner ||
      element.closest("dialog") ||
      pageFlow ||
      parentLayout ||
      flow
    )
      return;
    const bottom =
      owner.getBoundingClientRect().bottom -
      Number.parseFloat(getComputedStyle(owner).paddingBottom);
    const overflow = element.getBoundingClientRect().bottom - bottom;
    if (overflow > 1) owner.scrollTop += overflow;
  }, [height, pageFlow, parentLayout, flow]);
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
          <ScrollRegion label={`${title} fields`}>
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
      <ScrollRegion label={`${title} fields`}>
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
  const previous = useRef(active);
  useEffect(() => {
    if (active !== previous.current) {
      heading.current?.focus({ preventScroll: true });
      frame.current
        ?.querySelectorAll<HTMLElement>(".wizard-field-scroll, .wizard-guide")
        .forEach((element) => {
          element.scrollTop = 0;
        });
      if (pageFlow || parentLayout) {
        const element = frame.current;
        const owner = element?.closest("main");
        if (owner) owner.scrollTop = 0;
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
    previous.current = active;
  }, [active, pageFlow, parentLayout]);
  return (
    <section
      ref={frame}
      className={`wizard wizard-frame ${className}`}
      aria-label={title}
      data-layout={
        parentLayout ? "parent" : pageFlow || flow ? "flow" : undefined
      }
      style={
        {
          "--wizard-frame-height": height ? `${height}px` : undefined,
        } as CSSProperties
      }
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
              <span>{label}</span>
            </button>
          </li>
        ))}
      </ol>
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
