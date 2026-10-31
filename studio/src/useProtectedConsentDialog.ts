/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useLayoutEffect, useReducer, useState, type RefObject } from "react";

export function useProtectedConsentDialog(
  dialog: RefObject<HTMLDialogElement | null>,
  open: boolean,
  step: number,
) {
  const [layout, setLayout] = useState<"bounded" | "flow">("bounded");
  const [revision, remeasure] = useReducer((value: number) => value + 1, 0);
  useLayoutEffect(() => {
    const node = dialog.current;
    if (!open || !node) return;
    const reset = () => {
      const value = Number.parseFloat(
        getComputedStyle(document.documentElement).zoom,
      );
      const zoom = Number.isFinite(value) && value > 0 ? value : 1;
      node.style.setProperty("--delegation-zoom", String(zoom));
      node.dataset.compact = String(window.innerHeight / zoom <= 600);
      setLayout("bounded");
      remeasure();
    };
    reset();
    node.showModal();
    window.addEventListener("resize", reset);
    window.visualViewport?.addEventListener("resize", reset);
    document.fonts.addEventListener("loadingdone", reset);
    const observer = new MutationObserver(reset);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["style"],
    });
    return () => {
      window.removeEventListener("resize", reset);
      window.visualViewport?.removeEventListener("resize", reset);
      document.fonts.removeEventListener("loadingdone", reset);
      observer.disconnect();
      node.close();
    };
  }, [dialog, open]);
  useLayoutEffect(() => {
    if (open) {
      setLayout("bounded");
      remeasure();
    }
  }, [open, step]);
  useLayoutEffect(() => {
    if (!open || layout !== "bounded") return;
    const frame = dialog.current?.querySelector<HTMLElement>(".wizard-frame");
    if (!frame) return;
    const measure = () => {
      if (frame.dataset.layout !== "bounded") return;
      const fields = frame.querySelector<HTMLElement>(".wizard-field-scroll");
      // Latch only after the Wizard has committed its bounded content.
      if (fields && fields.clientHeight < 64) setLayout("flow");
    };
    measure();
    const observer = new MutationObserver(measure);
    observer.observe(frame, {
      attributes: true,
      attributeFilter: ["data-layout"],
    });
    return () => observer.disconnect();
  }, [dialog, layout, open, revision, step]);
  return layout;
}
