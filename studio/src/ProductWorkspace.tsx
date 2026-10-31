/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { MessageSquare } from "lucide-react";
const preference = "forge.product-assistant-collapsed";
const compactQuery = "(max-width: 1099px), (max-height: 680px)";
export function ProductWorkspace({
  document: documentContent,
  header,
  assistant,
  reveal,
  dismiss = 0,
}: {
  document: ReactNode;
  header?: ReactNode;
  assistant: (close: () => void) => ReactNode;
  reveal: number;
  dismiss?: number;
}) {
  const id = useId(),
    panel = useRef<HTMLDialogElement>(null),
    trigger = useRef<HTMLButtonElement>(null);
  const [compact, setCompact] = useState(
      () => matchMedia(compactQuery).matches,
    ),
    [mobileOpen, setMobileOpen] = useState(false),
    [desktopOpen, setDesktopOpen] = useState(() => {
      try {
        return localStorage.getItem(preference) !== "true";
      } catch {
        return true;
      }
    });
  const open = compact ? mobileOpen : desktopOpen;
  const previousReveal = useRef(reveal);
  useEffect(() => {
    const query = matchMedia(compactQuery);
    const change = () => setCompact(query.matches);
    query.addEventListener("change", change);
    return () => query.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem(preference, String(!desktopOpen));
    } catch {
      /* Layout preferences do not affect the saved product. */
    }
  }, [desktopOpen]);
  useEffect(() => {
    if (reveal !== previousReveal.current) {
      previousReveal.current = reveal;
      if (compact) setMobileOpen(true);
      else setDesktopOpen(true);
    }
  }, [reveal, compact]);
  useEffect(() => {
    if (dismiss && compact) setMobileOpen(false);
  }, [dismiss, compact]);
  useEffect(() => {
    const node = panel.current;
    if (!node) return;
    node.close();
    if (open) {
      if (compact) node.showModal();
      else node.show();
    }
  }, [compact, open]);
  function close() {
    if (compact) setMobileOpen(false);
    else setDesktopOpen(false);
    requestAnimationFrame(() => trigger.current?.focus());
  }
  return (
    <>
      <div
        className={`product-workbench${compact ? " compact" : ""}${!compact && !open ? " assistant-collapsed" : ""}`}
      >
        <header className="product-workbench-heading">
          {header}{" "}
          {(compact || !open) && (
            <button
              ref={trigger}
              className="secondary product-assistant-trigger"
              aria-label="Open design assistant"
              aria-expanded={open}
              aria-controls={id}
              onClick={() =>
                compact ? setMobileOpen(true) : setDesktopOpen(true)
              }
            >
              <MessageSquare size={20} />
              <span className="assistant-trigger-full">AI assistant</span>
              <span className="assistant-trigger-short">AI</span>
            </button>
          )}
        </header>
        <div className="product-document">{documentContent}</div>
        <dialog
          id={id}
          ref={panel}
          className={`product-assistant-panel${compact && open ? " drawer-open" : ""}`}
          role={compact ? "dialog" : "complementary"}
          aria-label="Product design assistant"
          aria-modal={compact && open ? true : undefined}
          onCancel={(event) => {
            event.preventDefault();
            close();
          }}
        >
          {assistant(close)}
        </dialog>
      </div>
    </>
  );
}
