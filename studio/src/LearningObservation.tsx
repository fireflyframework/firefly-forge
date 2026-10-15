/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { FileCheck2, RefreshCw, X } from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import type {
  InvalidationContext,
  LearningProjection,
  LearningRecord,
} from "./LearningRecordData";
import {
  observationKey,
  readObservationMarker,
  readLearningObservation,
  retainObservation,
  type ObservationMarker,
  type LearningObservationReceipt,
} from "./LearningObservationData";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";
export function LearningObservation({
  connection,
  record,
  projection,
  session,
  onDenied,
  onConfirmed,
}: {
  connection: Connection;
  record: LearningRecord;
  projection: LearningProjection | null;
  session: IdentitySession;
  onDenied: () => void;
  onConfirmed: (receipt: LearningObservationReceipt) => void;
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
        pending: readObservationMarker(
          sessionStorage.getItem(observationKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { pending: null, blocked: true };
    }
  });
  const [pending, setPending] = useState<ObservationMarker | null>(
      retained.pending,
    ),
    [blocked, setBlocked] = useState(retained.blocked),
    [receipt, setReceipt] = useState<LearningObservationReceipt | null>(null),
    [editing, setEditing] = useState(false),
    [step, setStep] = useState(0),
    [source, setSource] = useState(""),
    [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const live = useRef(true),
    inflight = useRef(false),
    active = useRef<AbortController | null>(null),
    dialog = useRef<HTMLDialogElement>(null),
    opener = useRef<HTMLButtonElement>(null),
    result = useRef<HTMLDivElement>(null);
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
  const eligible =
    record.kind === "local_outcome" &&
    record.state === "verified" &&
    projection &&
    ["source_admitted", "unknown"].includes(projection.state) &&
    projection.submission_request_id &&
    projection.workspace_id &&
    projection.target_revision &&
    projection.payload_digest &&
    projection.content_sha256;
  const validSource =
    /^[A-Za-z0-9._-]{1,200}$/.test(source) &&
    (!projection?.source_id || source === projection.source_id);
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
        "The exact observation has not been observed. It may still be in flight. Keep its identity; do not submit it again.",
      );
    else if (
      cause instanceof Error &&
      cause.message.startsWith("Receipt provenance is incomplete")
    )
      setError(cause.message);
    else
      setError(
        "The original observation could not be confirmed. Its identity is retained; no request was repeated.",
      );
  }
  async function readReceipt(marker: ObservationMarker) {
    const controller = new AbortController();
    active.current = controller;
    const value = await request<unknown>(
      connection,
      `/learning/records/${marker.record}/projection/observations/${marker.requestId}`,
      { signal: controller.signal },
    );
    const confirmed = readLearningObservation(value, marker);
    if (!live.current || controller.signal.aborted) return;
    setReceipt(confirmed);
    setError("");
    onConfirmed(confirmed);
  }
  async function check() {
    if (!pending || inflight.current || !canReview) return;
    inflight.current = true;
    setBusy(true);
    setError("");
    try {
      const stored = readObservationMarker(
        sessionStorage.getItem(observationKey(context)),
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
      !eligible ||
      !validSource ||
      !acknowledged ||
      !projection
    )
      return;
    const marker: ObservationMarker = {
      ...context,
      version: 1,
      requestId: crypto.randomUUID().replaceAll("-", ""),
      projection: projection.id,
      submissionRequest: projection.submission_request_id!,
      workspace: projection.workspace_id!,
      source,
      targetRevision: projection.target_revision!,
      payloadDigest: projection.payload_digest!,
      contentHash: projection.content_sha256!,
    };
    inflight.current = true;
    setBusy(true);
    setError("");
    try {
      retainObservation(sessionStorage, marker);
    } catch {
      setBlocked(true);
      setError(
        "Pending request storage could not be verified. No observation was submitted. Restore browser storage before continuing.",
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
        `/learning/records/${record.id}/projection/observe`,
        {
          method: "POST",
          body: JSON.stringify({
            request_id: marker.requestId,
            source_id: marker.source,
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
        Current project review permission is required to verify an existing
        source or read its observation receipt.
      </p>
    );
  return (
    <section
      className="learning-record-observation"
      aria-label="Existing source verification"
    >
      <h4>
        <FileCheck2 size={18} /> Verify an existing source
      </h4>
      {pending ? (
        <div ref={result} tabIndex={-1} className="learning-observation-result">
          <h4>
            {receipt
              ? "Original source verification confirmed"
              : "Original source verification unresolved"}
          </h4>
          <p>
            {receipt
              ? "The exact observation event was read with your current review authority. No request was repeated."
              : "A saved projection state cannot confirm this request. Read its exact receipt before treating the observation as confirmed."}
          </p>
          <p className="learning-observation-source">
            Source: {pending.source}
          </p>
          <details>
            <summary>Retained request details</summary>
            <dl className="learning-record-summary">
              <div>
                <dt>Factory</dt>
                <dd>{pending.base}</dd>
              </div>
              <div>
                <dt>Original actor</dt>
                <dd>{pending.actor}</dd>
              </div>
              <div>
                <dt>Workspace</dt>
                <dd>{pending.workspace}</dd>
              </div>
              <div>
                <dt>Request</dt>
                <dd>{pending.requestId}</dd>
              </div>
              <div>
                <dt>Record</dt>
                <dd>{pending.record}</dd>
              </div>
              <div>
                <dt>Target revision</dt>
                <dd>{pending.targetRevision}</dd>
              </div>
            </dl>
          </details>
          {receipt && (
            <>
              <p>
                Historical observation: verified. Current record state:{" "}
                {receipt.current_record_state}. Current projection state:{" "}
                {receipt.current_projection_state}.
              </p>
              <p>
                {receipt.citations.length} matching{" "}
                {receipt.citations.length === 1
                  ? "citation was"
                  : "citations were"}{" "}
                retained. Current reuse still checks evidence eligibility and
                the frozen target.
              </p>
              <details>
                <summary>Original observation receipt</summary>
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
                  <div>
                    <dt>Original submission</dt>
                    <dd>{receipt.submission_request_id}</dd>
                  </div>
                  <div>
                    <dt>Target revision</dt>
                    <dd>{receipt.target_revision}</dd>
                  </div>
                  <div>
                    <dt>Payload digest</dt>
                    <dd>{receipt.payload_digest}</dd>
                  </div>
                  <div>
                    <dt>Content hash</dt>
                    <dd>{receipt.content_sha256}</dd>
                  </div>
                  {receipt.citations.map((citation) => (
                    <div key={citation.chunk_id}>
                      <dt>Citation</dt>
                      <dd>{citation.chunk_id}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            </>
          )}
          {!receipt && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void check()}
            >
              <RefreshCw size={16} />
              {busy
                ? "Checking original verification…"
                : "Check original verification"}
            </button>
          )}
        </div>
      ) : (
        <>
          <p>
            This checks an already indexed source against the saved evidence and
            records a local observation. It does not submit a source or start
            ingestion.
          </p>
          {blocked && (
            <p role="status">
              Pending request storage cannot be read safely. Source verification
              is unavailable until it can be verified.
            </p>
          )}
          {eligible ? (
            <button
              ref={opener}
              className="secondary"
              disabled={blocked}
              onClick={() => {
                setSource(projection?.source_id ?? "");
                setStep(0);
                setAcknowledged(false);
                setEditing(true);
              }}
            >
              <FileCheck2 size={16} />
              Verify existing source
            </button>
          ) : (
            <p className="subtle">
              An admitted source or unknown submission with complete saved
              target provenance and verified local evidence is required.
              Submission in progress, unsubmitted, abandoned and already
              verified projections are not resubmitted here.
            </p>
          )}
        </>
      )}
      {!editing && alert}
      {editing && (
        <dialog
          ref={dialog}
          className="access-member-dialog learning-observation-dialog learning-invalidation-dialog"
          aria-label="Verify existing source"
          onCancel={(event) => {
            event.preventDefault();
            if (!busy) closeReview();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Verify existing source</h2>
            <button
              className="quiet"
              aria-label="Close source verification review"
              disabled={busy}
              onClick={closeReview}
            >
              <X size={20} />
            </button>
          </div>
          <Wizard
            steps={["Source", "Review"]}
            active={step}
            onStep={(value) => {
              setStep(value);
              setAcknowledged(false);
            }}
            locked={busy || blocked}
            title={
              step === 0
                ? "Select the existing indexed source"
                : "Review source"
            }
            description="The factory reads the source and its citations from the recorded target. It will not create or ingest a source."
          >
            {alert}
            {step === 0 ? (
              <>
                <p>{record.envelope.question}</p>
                <div className="field">
                  <label htmlFor="learning-observation-source">
                    Existing source identifier
                  </label>
                  <input
                    id="learning-observation-source"
                    value={source}
                    maxLength={200}
                    disabled={busy || blocked}
                    onChange={(event) => setSource(event.target.value)}
                  />
                  <p className="subtle">
                    Use the actual identifier from the admitted workspace. A
                    recorded source cannot be replaced. Do not enter a token,
                    URL or private credential.
                  </p>
                </div>
                <dl className="learning-record-summary">
                  <div>
                    <dt>Purpose</dt>
                    <dd>Verified learning for this project</dd>
                  </div>
                  <div>
                    <dt>Recorded source status</dt>
                    <dd>{projection?.source_status ?? "Unknown"}</dd>
                  </div>
                </dl>
              </>
            ) : (
              <>
                <h3>{record.envelope.question}</h3>
                <p className="learning-observation-source">Source: {source}</p>
                <label className="learning-invalidation-ack">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    disabled={busy || blocked}
                    onChange={(event) => setAcknowledged(event.target.checked)}
                  />
                  <span>
                    I understand this reads the existing source and records a
                    local verification; it does not submit or ingest a source.
                  </span>
                </label>
                <details className="learning-observation-scope">
                  <summary>Source and request details</summary>
                  <dl className="learning-record-summary">
                    <div>
                      <dt>Factory</dt>
                      <dd>{context.base}</dd>
                    </div>
                    <div>
                      <dt>Actor</dt>
                      <dd>
                        {session.user.display_name} · {context.actor}
                      </dd>
                    </div>
                    <div>
                      <dt>Repository</dt>
                      <dd>{context.repository}</dd>
                    </div>
                    <div>
                      <dt>Origin run</dt>
                      <dd>{context.run}</dd>
                    </div>
                    <div>
                      <dt>Record</dt>
                      <dd>{context.record}</dd>
                    </div>
                    <div>
                      <dt>Projection</dt>
                      <dd>{projection?.id}</dd>
                    </div>
                    <div>
                      <dt>Workspace</dt>
                      <dd>{projection?.workspace_id}</dd>
                    </div>
                    <div>
                      <dt>Original submission</dt>
                      <dd>{projection?.submission_request_id}</dd>
                    </div>
                    <div>
                      <dt>Target revision</dt>
                      <dd>{projection?.target_revision}</dd>
                    </div>
                    <div>
                      <dt>Payload digest</dt>
                      <dd>{projection?.payload_digest}</dd>
                    </div>
                    <div>
                      <dt>Content hash</dt>
                      <dd>{projection?.content_sha256}</dd>
                    </div>
                  </dl>
                </details>
              </>
            )}
            <div className="wizard-actions">
              <button
                className="secondary"
                disabled={busy}
                onClick={closeReview}
              >
                Cancel
              </button>
              {step === 0 ? (
                <button
                  className="primary"
                  disabled={busy || blocked || !validSource}
                  onClick={() => setStep(1)}
                >
                  Review source
                </button>
              ) : (
                <button
                  className="primary"
                  disabled={busy || blocked || !acknowledged}
                  onClick={() => void submit()}
                >
                  Verify source
                </button>
              )}
            </div>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
