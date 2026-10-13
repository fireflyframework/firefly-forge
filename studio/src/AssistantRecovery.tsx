/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { RefreshCw, X } from "lucide-react";
import { ApiError, request, type Connection } from "./api";
export type RecoverableTurn = {
  id: string;
  status: string;
  ownership?: {
    owner_id: string | null;
    claim_epoch: number;
    lease_expires_at: string | null;
    last_heartbeat_at: string | null;
  };
  recovery?: {
    eligible: boolean;
    expected_epoch: number;
    legacy_confirmation_required: boolean;
  };
};
export function AssistantRecovery({
  connection,
  repository,
  conversation,
  turn,
  onClose,
  onRecorded,
}: {
  connection: Connection;
  repository: string;
  conversation: string;
  turn: RecoverableTurn;
  onClose: () => void;
  onRecorded: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    pending = useRef<AbortController | null>(null);
  const requestId = useRef(crypto.randomUUID().replaceAll("-", ""));
  const [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [unknown, setUnknown] = useState(false),
    [error, setError] = useState("");
  const legacy = Boolean(turn.recovery?.legacy_confirmation_required);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => {
      pending.current?.abort();
      element?.close();
    };
  }, []);
  async function record() {
    if (!turn.recovery || (legacy && !confirmed) || busy || unknown) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      await request<RecoverableTurn>(
        connection,
        `/repositories/${repository}/assistant/conversations/${conversation}/turns/${turn.id}/recover`,
        {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({
            request_id: requestId.current,
            expected_epoch: turn.recovery.expected_epoch,
            legacy_confirmed_stopped: legacy && confirmed,
          }),
        },
      );
      if (!controller.signal.aborted) {
        onRecorded();
        onClose();
      }
    } catch (cause) {
      if (controller.signal.aborted) return;
      const rejected =
        cause instanceof ApiError &&
        [403, 404, 409, 422].includes(cause.status);
      setUnknown(!rejected);
      setError(
        rejected
          ? cause.message
          : "The result is not confirmed. Check the recorded response before taking another action; this request will not be sent again automatically.",
      );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function check() {
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    try {
      const value = await request<{ turns: RecoverableTurn[] }>(
        connection,
        `/repositories/${repository}/assistant/conversations/${conversation}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      const current = value.turns?.find((item) => item.id === turn.id);
      if (
        current &&
        (current.status !== "processing" ||
          current.recovery?.expected_epoch !== turn.recovery?.expected_epoch)
      ) {
        onRecorded();
        onClose();
      } else
        setError(
          "The recorded response has not changed. Its outcome remains unconfirmed; no request has been repeated.",
        );
    } catch {
      if (!controller.signal.aborted)
        setError(
          "The recorded response could not be read. Keep this result unconfirmed until the server is available.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="assistant-recovery-dialog"
      aria-label="Review interrupted response"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <header>
        <div>
          <h2>Review interrupted response</h2>
          <p>
            This changes the recorded waiting state. It does not call the model
            again or apply a proposal.
          </p>
        </div>
        <button
          type="button"
          className="quiet"
          aria-label="Close interrupted response review"
          disabled={busy}
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </header>
      <div className="assistant-recovery-body">
        <p>
          {legacy
            ? "This earlier response has no recorded worker lease. Confirm that its original worker stopped before releasing the waiting state."
            : "The server reports that the worker’s ownership expired. The model’s final outcome may still be unknown."}
        </p>
        <p>
          The response will need reconciliation. Your typed message and selected
          files stay in the editor.
        </p>
        {legacy && (
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={busy || unknown}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            I confirmed that the original worker stopped.
          </label>
        )}
        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}
      </div>
      <footer>
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={onClose}
        >
          Keep recorded state
        </button>
        {unknown ? (
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => void check()}
          >
            <RefreshCw size={18} />
            Check recorded response
          </button>
        ) : (
          <button
            type="button"
            className="primary"
            disabled={busy || (legacy && !confirmed)}
            onClick={() => void record()}
          >
            {busy ? "Recording…" : "Record interrupted outcome"}
          </button>
        )}
      </footer>
    </dialog>
  );
}
