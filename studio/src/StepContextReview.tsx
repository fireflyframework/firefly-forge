/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { MessageSquare, X } from "lucide-react";
import { RichMarkdown } from "./RichMarkdown";
import { appendStepContext } from "./ProductStepContext";
export function StepContextReview({
  message,
  context,
  onUse,
  onClose,
}: {
  message: string;
  context: string;
  onUse: (value: string) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const node = dialog.current;
    const timer = requestAnimationFrame(() => node?.showModal());
    return () => {
      cancelAnimationFrame(timer);
      node?.close();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="step-context-dialog"
      aria-label="Review step context"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header>
        <div>
          <h2>
            <MessageSquare size={24} />
            Review context before adding it
          </h2>
          <p>
            Your typed message is preserved. Choose how to include this editor
            content; nothing is sent yet.
          </p>
        </div>
        <button
          type="button"
          className="quiet"
          aria-label="Close step context review"
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </header>
      <div className="step-context-body">
        <section>
          <h3>Your current message</h3>
          <RichMarkdown value={message} />
        </section>
        <section>
          <h3>Current step context</h3>
          <RichMarkdown value={context} />
        </section>
        {error && (
          <p role="alert" className="alert">
            {error}
          </p>
        )}
      </div>
      <footer>
        <button type="button" className="secondary" onClick={onClose}>
          Keep my message
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => onUse(context)}
        >
          Replace message
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => {
            try {
              onUse(appendStepContext(message, context));
            } catch (cause) {
              setError(
                cause instanceof Error
                  ? cause.message
                  : "The context could not be appended.",
              );
            }
          }}
        >
          Append context
        </button>
      </footer>
    </dialog>
  );
}
