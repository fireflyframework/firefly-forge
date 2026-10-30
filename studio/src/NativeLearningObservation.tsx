/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { FileCheck2, RefreshCw } from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import type {
  InvalidationContext,
  LearningProjection,
  LearningRecord,
} from "./LearningRecordData";
import {
  exactNativeObservation,
  makeNativeObservationMarker,
  nativeObservationBody,
  nativeObservationKey,
  readNativeLearningReceipt,
  readNativeObservationMarker,
  retainNativeObservation,
  type NativeObservationMarker,
  type NativeObservationReceipt,
} from "./NativeLearningData";
import "./NativeLearningObservation.css";

export function NativeLearningObservation({
  connection,
  record,
  projection,
  session,
  loading,
  error,
  onDenied,
  onConfirmed,
  onRefresh,
}: {
  connection: Connection;
  record: LearningRecord;
  projection: LearningProjection | null;
  session?: IdentitySession | null;
  loading: boolean;
  error: string;
  onDenied: () => void;
  onConfirmed: (receipt: NativeObservationReceipt) => void;
  onRefresh: () => void;
}) {
  const context: InvalidationContext = {
    base: new URL(normalizeBase(connection.base), window.location.href).href,
    actor: session?.user.id ?? "",
    repository: record.envelope.context.repository_id,
    project: record.envelope.context.project_id,
    run: record.envelope.run_id,
    record: record.id,
  };
  const [retained] = useState(() => {
    try {
      return {
        marker: readNativeObservationMarker(
          sessionStorage.getItem(nativeObservationKey(context)),
          context,
        ),
        blocked: false,
      };
    } catch {
      return { marker: null, blocked: true };
    }
  });
  const [pending, setPending] = useState<NativeObservationMarker | null>(
      retained.marker,
    ),
    [blocked, setBlocked] = useState(retained.blocked),
    [reviewing, setReviewing] = useState(false),
    [source, setSource] = useState(""),
    [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false),
    [failure, setFailure] = useState(""),
    [receipt, setReceipt] = useState<NativeObservationReceipt | null>(null),
    [historical, setHistorical] = useState(false);
  const live = useRef(true),
    inflight = useRef(false),
    active = useRef<AbortController | null>(null),
    result = useRef<HTMLDivElement>(null),
    alert = useRef<HTMLDivElement>(null),
    opener = useRef<HTMLButtonElement>(null);
  const evidence = projection?.native;
  const permissions =
    session?.memberships.find((item) => item.project_id === context.project)
      ?.permissions ?? [];
  const canRead = Boolean(
    session?.global_admin || permissions.includes("review"),
  );
  const canObserve =
    canRead && Boolean(session?.global_admin || permissions.includes("edit"));
  const eligible =
    evidence &&
    projection &&
    ["source_admitted", "unknown"].includes(projection.state) &&
    ((record.kind === "local_outcome" && record.state === "verified") ||
      (record.kind === "observed_failure" && record.state === "candidate"));
  const selectedSource = projection?.source_id ?? source;
  const validSource = /^[A-Za-z0-9_-]{1,64}$/.test(selectedSource);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (failure) alert.current?.focus();
  }, [failure]);
  useEffect(() => {
    if (receipt) result.current?.focus();
  }, [receipt]);
  function failed(cause: unknown) {
    if (!live.current) return;
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      onDenied();
      return;
    }
    setFailure(
      cause instanceof ApiError && cause.status === 404
        ? "The exact observation is not recorded yet. It may still be in flight. Keep this identity and read it again later."
        : "The source observation could not be confirmed. Keep its original identity; no request was repeated.",
    );
  }
  async function read(marker: NativeObservationMarker, saved = true) {
    if (marker.evidenceKind !== record.kind)
      throw new Error("The saved evidence kind changed");
    if (saved) await exactNativeObservation(sessionStorage, marker);
    const controller = new AbortController();
    active.current = controller;
    const value = await request<unknown>(
      connection,
      `/learning/records/${marker.record}/projection/observations/${marker.requestId}`,
      { signal: controller.signal },
    );
    const confirmed = readNativeLearningReceipt(value, marker, !saved);
    if (!live.current || controller.signal.aborted) return;
    setReceipt(confirmed);
    setHistorical(!saved);
    setFailure("");
    onConfirmed(confirmed);
  }
  async function check() {
    if (inflight.current || !canRead || !pending) return;
    inflight.current = true;
    setBusy(true);
    setFailure("");
    try {
      await read(pending);
    } catch (cause) {
      failed(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function readRecorded() {
    if (
      !projection?.native?.observationRequest ||
      !projection.source_id ||
      !canRead ||
      inflight.current
    )
      return;
    inflight.current = true;
    setBusy(true);
    setFailure("");
    try {
      const marker = await makeNativeObservationMarker(
        context,
        projection,
        projection.source_id,
        projection.native.observationRequest,
      );
      await read(marker, false);
    } catch (cause) {
      failed(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function observe() {
    if (
      inflight.current ||
      blocked ||
      pending ||
      !eligible ||
      !canObserve ||
      !validSource ||
      !acknowledged ||
      !projection
    )
      return;
    inflight.current = true;
    setBusy(true);
    setFailure("");
    let marker: NativeObservationMarker;
    try {
      marker = await makeNativeObservationMarker(
        context,
        projection,
        selectedSource,
        crypto.randomUUID().replaceAll("-", ""),
      );
      await retainNativeObservation(sessionStorage, marker);
    } catch {
      setBlocked(true);
      setFailure(
        "Pending request storage could not be verified. No observation was submitted. Restore browser storage before continuing.",
      );
      inflight.current = false;
      setBusy(false);
      return;
    }
    if (!live.current) {
      inflight.current = false;
      return;
    }
    setPending(marker);
    setReviewing(false);
    try {
      await exactNativeObservation(sessionStorage, marker);
      await request<unknown>(
        connection,
        `/learning/records/${record.id}/projection/observe`,
        {
          method: "POST",
          body: JSON.stringify(nativeObservationBody(marker)),
        },
      );
      if (live.current) await read(marker);
    } catch (cause) {
      failed(cause);
    } finally {
      inflight.current = false;
      if (live.current) setBusy(false);
    }
  }
  const versions = receipt?.source_versions ?? evidence?.sourceVersions ?? [];
  const citations = receipt?.citations ?? evidence?.citations ?? [];
  return (
    <section
      className="learning-record-projection native-learning"
      aria-label="Koinon source evidence"
    >
      <h4>
        <FileCheck2 size={18} />
        Koinon source evidence
      </h4>
      <p>
        {record.kind === "observed_failure"
          ? "Observed failure. Its cause is not classified, and independent checks are still required."
          : record.kind === "local_outcome"
            ? "Local checks were observed. Lookup still checks current eligibility."
            : "This source reference does not establish a local outcome."}
      </p>
      <p className="subtle">
        A source observation does not approve memory. Review external proposals
        privately in Koinon.
      </p>
      {loading ? (
        <p role="status">Reading native source status…</p>
      ) : error ? (
        <div role="alert" className="alert">
          {error}
        </div>
      ) : (
        <>
          {!receipt && (
            <>
              <span className="learning-record-state">
                {projection?.state === "verified"
                  ? "Recorded source readback"
                  : projection?.state === "unknown"
                    ? "Source outcome unknown"
                    : projection?.state === "executing"
                      ? "Source submission in progress"
                      : projection?.state === "source_admitted"
                        ? "Source admitted"
                        : projection?.state === "abandoned"
                          ? "Submission abandoned"
                          : "Not submitted"}
              </span>
              <p>
                {projection?.state === "unknown"
                  ? "Keep the admitted upload identity. Observe an existing source after checking its scoped content; do not upload it again."
                  : projection?.state === "executing"
                    ? "A source submission was recorded. Refresh status while its outcome is unresolved."
                    : !evidence
                      ? "No native source scope is admitted here. Submit evidence through an authorized API, CLI, SDK or MCP workflow before observation."
                      : projection?.state === "verified"
                        ? "Read the recorded receipt to inspect its historical provenance."
                        : "Observe the existing source to read its versions and citations."}
              </p>
            </>
          )}
          {evidence && (
            <details className="native-learning-saved-scope">
              <summary>Saved scope and source</summary>
              <dl className="learning-record-summary native-learning-scope">
                <div>
                  <dt>Knowledge tenant</dt>
                  <dd>{evidence.tenant}</dd>
                </div>
                <div>
                  <dt>Knowledge workspace</dt>
                  <dd>{evidence.scope.workspace_id}</dd>
                </div>
                <div>
                  <dt>Knowledge project</dt>
                  <dd>{evidence.scope.project_id}</dd>
                </div>
                <div>
                  <dt>Evidence collection</dt>
                  <dd>{evidence.scope.collection_id}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>{projection?.source_id ?? "Not recorded"}</dd>
                </div>
                <div>
                  <dt>Upstream authority</dt>
                  <dd>Unverified material</dd>
                </div>
              </dl>
            </details>
          )}
          {blocked && !failure && (
            <div role="alert" className="alert">
              The saved observation identity cannot be read. Keep browser data
              for review; do not start another request.
            </div>
          )}
          {(pending || receipt) && (
            <div className="native-learning-result" ref={result} tabIndex={-1}>
              <h4>
                {receipt
                  ? "Source observation recorded"
                  : busy
                    ? "Reading source observation"
                    : "Observation outcome unknown"}
              </h4>
              <p>
                {receipt
                  ? historical
                    ? "This historical receipt belongs to the recorded event. It does not establish a new request from this browser."
                    : "The exact event matches the retained request and source. No observation was repeated."
                  : "A projection's saved state cannot confirm this request. Read its exact receipt before treating the observation as recorded."}
              </p>
              {receipt && (
                <p>
                  Local record: <strong>{receipt.current_record_state}</strong>.
                  Current projection:{" "}
                  {receipt.current_projection_state.replaceAll("_", " ")}.
                  Recorded source versions: {receipt.source_versions.length}.
                </p>
              )}
              {pending && (
                <div className="native-learning-actions">
                  <button
                    className="secondary"
                    disabled={busy || !canRead}
                    onClick={check}
                  >
                    <RefreshCw size={16} />
                    {busy
                      ? "Reading exact observation…"
                      : "Read exact observation"}
                  </button>
                </div>
              )}
            </div>
          )}
          {!pending && eligible && !blocked && !reviewing && (
            <div className="native-learning-actions">
              <button
                ref={opener}
                className="secondary"
                disabled={!canObserve || busy}
                onClick={() => setReviewing(true)}
              >
                Review source observation
              </button>
            </div>
          )}
          {reviewing && !pending && !blocked && (
            <form
              className="native-learning-review"
              aria-label="Review source observation"
              onSubmit={(event) => {
                event.preventDefault();
                void observe();
              }}
            >
              <h4>Observe an existing source</h4>
              <p>
                Read its scoped content, versions and citations. This action
                creates no source upload.
              </p>
              {!projection?.source_id && (
                <div className="field">
                  <label htmlFor="native-learning-source">
                    Existing source ID
                  </label>
                  <input
                    id="native-learning-source"
                    value={source}
                    onChange={(event) => setSource(event.target.value)}
                    maxLength={64}
                    autoComplete="off"
                  />
                </div>
              )}
              <label className="native-learning-ack">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                <span>
                  I checked this existing source belongs to the saved scope and
                  content.
                </span>
              </label>
              <div className="native-learning-actions">
                <button
                  className="primary"
                  disabled={
                    !acknowledged || !validSource || busy || !canObserve
                  }
                >
                  Observe this source
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() => {
                    setReviewing(false);
                    opener.current?.focus();
                  }}
                >
                  Cancel review
                </button>
              </div>
            </form>
          )}
          {!pending && evidence?.observationRequest && (
            <div className="native-learning-actions">
              <button
                className="secondary"
                onClick={readRecorded}
                disabled={!canRead || busy}
              >
                Read recorded receipt
              </button>
            </div>
          )}
          {!canObserve && (
            <p className="subtle">
              Current project edit and review permission is required to start a
              source observation.
            </p>
          )}
          {!canRead && pending && (
            <p className="subtle">
              Current project review permission is required to read the saved
              receipt.
            </p>
          )}
          <div className="native-learning-actions">
            <button className="secondary" onClick={onRefresh} disabled={busy}>
              <RefreshCw size={16} />
              Refresh source status
            </button>
          </div>
          {failure && (
            <div className="alert" role="alert" tabIndex={-1} ref={alert}>
              {failure}
            </div>
          )}
          {versions.length ? (
            <details
              className="native-learning-versions"
              open={Boolean(receipt)}
            >
              <summary>Source versions and citations</summary>
              <div
                className="native-learning-version-scroll"
                role="region"
                aria-label="Source versions and citations"
                tabIndex={0}
              >
                {versions.map((version) => (
                  <dl
                    className="learning-record-summary"
                    key={version.asset_id}
                  >
                    <div>
                      <dt>Document</dt>
                      <dd>{version.asset_id}</dd>
                    </div>
                    <div>
                      <dt>Native source</dt>
                      <dd>{version.native_source_id}</dd>
                    </div>
                    <div>
                      <dt>Source version</dt>
                      <dd>{version.source_version_id}</dd>
                    </div>
                    <div>
                      <dt>Extraction</dt>
                      <dd>{version.extraction_id}</dd>
                    </div>
                    {citations
                      .filter((item) => item.asset_id === version.asset_id)
                      .map((item) => (
                        <div key={item.chunk_id}>
                          <dt>Citation</dt>
                          <dd>{item.chunk_id}</dd>
                        </div>
                      ))}
                  </dl>
                ))}
              </div>
            </details>
          ) : (
            evidence && (
              <p className="subtle">
                Source versions are not recorded yet. An observation must read
                their actual identities.
              </p>
            )
          )}
          {(pending || receipt) && (
            <details className="native-learning-identities">
              <summary>Request and receipt identifiers</summary>
              <dl className="learning-record-summary">
                <div>
                  <dt>Request</dt>
                  <dd>{receipt?.request_id ?? pending?.requestId}</dd>
                </div>
                <div>
                  <dt>Original actor</dt>
                  <dd>
                    {receipt
                      ? (receipt.actor_id ?? "Not recorded")
                      : pending?.actor}
                  </dd>
                </div>
                <div>
                  <dt>Source content digest</dt>
                  <dd>{receipt?.content_sha256 ?? pending?.contentHash}</dd>
                </div>
                <div>
                  <dt>Local payload digest</dt>
                  <dd>{receipt?.payload_digest ?? pending?.payloadDigest}</dd>
                </div>
                {receipt && (
                  <>
                    <div>
                      <dt>Recorded at</dt>
                      <dd>
                        <time dateTime={receipt.created_at}>
                          {new Date(receipt.created_at).toLocaleString()}
                        </time>
                      </dd>
                    </div>
                    <div>
                      <dt>Event digest</dt>
                      <dd>{receipt.digest}</dd>
                    </div>
                  </>
                )}
              </dl>
            </details>
          )}
        </>
      )}
    </section>
  );
}
