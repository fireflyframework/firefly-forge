/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { Ban, FileCheck2, RefreshCw, X } from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import {
  invalidationKey,
  readInvalidationMarker,
  readLearningInvalidation,
  retainInvalidation,
  type InvalidationContext,
  type InvalidationMarker,
  type LearningInvalidationReceipt,
  type LearningRecord,
} from "./LearningRecordData";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";

export function LearningInvalidation({
  connection,
  record,
  session,
  onDenied,
  onConfirmed,
}: {
  connection: Connection;
  record: LearningRecord;
  session: IdentitySession;
  onDenied: () => void;
  onConfirmed: (state: LearningRecord["state"]) => void;
}) {
  const context: InvalidationContext = {
    base: new URL(normalizeBase(connection.base), window.location.href).href,
    actor: session.user.id,
    repository: record.envelope.context.repository_id,
    project: record.envelope.context.project_id,
    run: record.envelope.run_id,
    record: record.id,
  };
  const [retained] = useState(() => {
    try {
      return {
        pending: readInvalidationMarker(
          sessionStorage.getItem(invalidationKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { pending: null, blocked: true };
    }
  });
  const [pending, setPending] = useState<InvalidationMarker | null>(
      retained.pending,
    ),
    [blocked, setBlocked] = useState(retained.blocked),
    [receipt, setReceipt] = useState<LearningInvalidationReceipt | null>(null),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0),
    [reason, setReason] = useState(""),
    [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const live = useRef(true),
    inflight = useRef(false),
    dialog = useRef<HTMLDialogElement>(null),
    active = useRef<AbortController | null>(null),
    result = useRef<HTMLDivElement>(null),
    opener = useRef<HTMLButtonElement>(null);
  const canReview =
    session.global_admin ||
    Boolean(
      context.project &&
      session.memberships.some(
        (item) =>
          item.project_id === context.project &&
          item.permissions.includes("review"),
      ),
    );
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    const node = dialog.current;
    if (editing) node?.showModal();
    return () => node?.close();
  }, [editing]);
  useEffect(() => {
    if (pending || receipt) result.current?.focus();
  }, [pending, receipt]);
  function closeReview() {
    dialog.current?.close();
    setEditing(false);
    opener.current?.focus();
  }
  function failure(cause: unknown) {
    if (!live.current) return;
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      onDenied();
      return;
    }
    if (cause instanceof ApiError && cause.status === 404)
      setError(
        "The exact request has not been observed. It may still be in flight. Keep its identity; do not submit it again.",
      );
    else if (
      cause instanceof Error &&
      cause.message.startsWith("Receipt attribution is unknown")
    )
      setError(cause.message);
    else
      setError(
        "The original request could not be confirmed. Its identity is retained; no request was repeated.",
      );
  }
  async function readReceipt(marker: InvalidationMarker) {
    const controller = new AbortController();
    active.current = controller;
    const value = await request<unknown>(
      connection,
      `/learning/records/${marker.record}/invalidations/${marker.requestId}`,
      { signal: controller.signal },
    );
    const confirmed = readLearningInvalidation(value, marker);
    if (!live.current || controller.signal.aborted) return;
    setReceipt(confirmed);
    setError("");
    onConfirmed(confirmed.current_record_state);
  }
  async function check() {
    if (!pending || inflight.current || !canReview) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    try {
      const stored = readInvalidationMarker(
        sessionStorage.getItem(invalidationKey(context)),
        context,
      );
      if (!stored || JSON.stringify(stored) !== JSON.stringify(pending))
        throw new Error("Pending identity changed");
      await readReceipt(pending);
    } catch (cause) {
      failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function submit() {
    if (
      pending ||
      blocked ||
      inflight.current ||
      !canReview ||
      !reason.trim() ||
      reason.length > 2000 ||
      !acknowledged
    )
      return;
    const marker: InvalidationMarker = {
      ...context,
      version: 1,
      requestId: crypto.randomUUID().replaceAll("-", ""),
      reason,
    };
    inflight.current = true;
    setBusy(true);
    setError("");
    try {
      retainInvalidation(sessionStorage, marker);
    } catch {
      setBlocked(true);
      setError(
        "Pending request storage could not be verified. No invalidation was submitted. Restore browser storage before continuing.",
      );
      inflight.current = false;
      setBusy(false);
      return;
    }
    setPending(marker);
    setEditing(false);
    try {
      await request<unknown>(
        connection,
        `/learning/records/${record.id}/invalidate`,
        {
          method: "POST",
          body: JSON.stringify({
            reason: marker.reason,
            request_id: marker.requestId,
          }),
        },
      );
      if (live.current) await readReceipt(marker);
    } catch (cause) {
      failure(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  const alert = error && (
    <div className="alert" role="alert">
      {error}
    </div>
  );
  if (!canReview)
    return (
      <p className="subtle">
        Current project review permission is required to invalidate evidence or
        read its decision receipt.
      </p>
    );
  return (
    <section
      className="learning-record-invalidation"
      aria-label="Evidence invalidation"
    >
      <h4>
        <Ban size={18} /> Evidence lifecycle
      </h4>
      {pending ? (
        <div
          ref={result}
          tabIndex={-1}
          className="learning-invalidation-result"
        >
          <h4>
            {receipt
              ? "Original invalidation confirmed"
              : "Original request unresolved"}
          </h4>
          <p>
            {receipt
              ? "The exact event was read with your current review authority. No request was repeated."
              : "The response alone cannot confirm this decision. Check its exact receipt; refreshing record state never authorizes another submission."}
          </p>
          <dl className="learning-record-summary">
            <div>
              <dt>Original reason</dt>
              <dd>{pending.reason}</dd>
            </div>
            <div>
              <dt>Request</dt>
              <dd>{pending.requestId}</dd>
            </div>
          </dl>
          {receipt && (
            <>
              <p>
                <FileCheck2 size={16} /> Historical event: invalidated. Current
                record state: {receipt.current_record_state}.
              </p>
              <dl className="learning-record-summary">
                <div>
                  <dt>Original actor</dt>
                  <dd>{receipt.actor_id}</dd>
                </div>
                <div>
                  <dt>Recorded at</dt>
                  <dd>
                    <time dateTime={receipt.created_at}>
                      {new Date(receipt.created_at).toLocaleString()}
                    </time>
                  </dd>
                </div>
                <div>
                  <dt>Event</dt>
                  <dd>{receipt.event_id}</dd>
                </div>
                <div>
                  <dt>Original event digest</dt>
                  <dd>{receipt.digest}</dd>
                </div>
                <div>
                  <dt>Sequence</dt>
                  <dd>{receipt.sequence}</dd>
                </div>
              </dl>
            </>
          )}
          {!receipt && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void check()}
            >
              <RefreshCw size={16} />
              {busy ? "Checking original request…" : "Check original request"}
            </button>
          )}
        </div>
      ) : (
        <>
          <p>
            Invalidation excludes this evidence from reuse and preserves its
            recorded history. This view cannot undo the decision.
          </p>
          {blocked && (
            <p role="status">
              Pending request storage cannot be read safely. Invalidation is
              unavailable until it can be verified.
            </p>
          )}
          {!["invalidated", "superseded"].includes(record.state) && (
            <button
              ref={opener}
              className="secondary"
              disabled={blocked}
              onClick={() => {
                setStep(0);
                setAcknowledged(false);
                setEditing(true);
              }}
            >
              <Ban size={16} />
              Invalidate evidence
            </button>
          )}
        </>
      )}
      {!editing && alert}
      {editing && (
        <dialog
          ref={dialog}
          className="access-member-dialog learning-invalidation-dialog"
          aria-label="Invalidate saved evidence"
          onCancel={(event) => {
            event.preventDefault();
            if (!busy) closeReview();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Invalidate saved evidence</h2>
            <button
              className="quiet"
              aria-label="Close invalidation review"
              disabled={busy}
              onClick={closeReview}
            >
              <X size={20} />
            </button>
          </div>
          <Wizard
            steps={["Reason", "Review"]}
            active={step}
            onStep={(value) => {
              setStep(value);
              setAcknowledged(false);
            }}
            locked={busy || blocked}
            title={
              step === 0
                ? "Explain why this evidence no longer applies"
                : "Review the permanent decision"
            }
            description="Only this saved record will be invalidated. Its evidence and history remain available."
          >
            {alert}
            {step === 0 ? (
              <>
                <p>{record.envelope.question}</p>
                <div className="field">
                  <label htmlFor="learning-invalidation-reason">
                    Reason for invalidation
                  </label>
                  <textarea
                    id="learning-invalidation-reason"
                    value={reason}
                    maxLength={2000}
                    disabled={busy || blocked}
                    onChange={(event) => setReason(event.target.value)}
                  />
                  <p className="subtle">
                    Use a specific review reason. Do not enter passwords, tokens
                    or private credentials.
                  </p>
                </div>
              </>
            ) : (
              <>
                <h3>{record.envelope.question}</h3>
                <p className="learning-invalidation-reason">{reason}</p>
                <dl className="learning-record-summary">
                  <div>
                    <dt>Factory</dt>
                    <dd>{context.base}</dd>
                  </div>
                  <div>
                    <dt>Original actor</dt>
                    <dd>{context.actor}</dd>
                  </div>
                  <div>
                    <dt>Repository</dt>
                    <dd>{context.repository}</dd>
                  </div>
                  <div>
                    <dt>Record</dt>
                    <dd>{context.record}</dd>
                  </div>
                </dl>
                <label className="learning-invalidation-ack">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    disabled={busy || blocked}
                    onChange={(event) => setAcknowledged(event.target.checked)}
                  />
                  <span>
                    I understand this excludes the evidence from reuse and keeps
                    its history.
                  </span>
                </label>
              </>
            )}
            <div className="wizard-actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={closeReview}
              >
                Keep evidence
              </button>
              {step === 0 ? (
                <button
                  className="primary"
                  disabled={busy || blocked || !reason.trim()}
                  onClick={() => setStep(1)}
                >
                  Review invalidation
                </button>
              ) : (
                <button
                  className="primary"
                  disabled={busy || blocked || !acknowledged}
                  onClick={() => void submit()}
                >
                  Confirm invalidation
                </button>
              )}
            </div>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
