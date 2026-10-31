/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Archive, FileCheck2, RefreshCw, X } from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import type { CampaignReport } from "./CampaignReportData";
import {
  readCampaignArchiveCandidate,
  readCampaignArchivePage,
  readCampaignArchiveReceipt,
  type CampaignArchivePage,
  type CampaignArchiveReceipt,
  type CampaignArchiveSummary,
  type ArchiveExpected,
} from "./CampaignReportArchiveData";
import {
  archiveMarkerBinding,
  readArchiveMarkers,
  retainArchiveMarker,
  type ArchiveMarker,
} from "./CampaignReportArchiveMarker";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";
import "./CampaignReportHistory.css";
const stateLabel = (value: string) => value.replaceAll("_", " ");
export function CampaignReportHistory({
  connection,
  session,
  project,
  campaign,
  report,
  onDenied,
  renderReport,
  onArchiveSelected,
}: {
  connection: Connection;
  session: IdentitySession;
  project: string;
  campaign: string;
  report: CampaignReport | null;
  onDenied: () => void;
  renderReport: (report: CampaignReport) => ReactNode;
  onArchiveSelected?: (receipt: CampaignArchiveReceipt | null) => void;
}) {
  const context = {
    base: new URL(
      normalizeBase(connection.base),
      window.location.href,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    project,
    campaign,
  };
  const [retained] = useState(() => {
    try {
      return {
        items: readArchiveMarkers(localStorage, context),
        blocked: false,
      };
    } catch {
      return { items: [] as ArchiveMarker[], blocked: true };
    }
  });
  const [markers, setMarkers] = useState(retained.items),
    [blocked, setBlocked] = useState(retained.blocked),
    [page, setPage] = useState<CampaignArchivePage | null>(null),
    [receipt, setReceipt] = useState<CampaignArchiveReceipt | null>(null),
    [confirmed, setConfirmed] = useState<Set<string>>(new Set()),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [candidate, setCandidate] = useState<CampaignReport | null>(null),
    [integrityChecked, setIntegrityChecked] = useState(false),
    [review, setReview] = useState<CampaignReport | null>(null),
    [step, setStep] = useState(0),
    [ack, setAck] = useState(false);
  const live = useRef(true),
    active = useRef<AbortController | null>(null),
    generation = useRef(0),
    inflight = useRef(false),
    dialog = useRef<HTMLDialogElement>(null),
    opener = useRef<HTMLButtonElement>(null),
    heading = useRef<HTMLHeadingElement>(null),
    pendingHeading = useRef<HTMLHeadingElement>(null),
    restoreFocus = useRef(false);
  useEffect(() => {
    onArchiveSelected?.(receipt);
    return () => onArchiveSelected?.(null);
  }, [receipt, onArchiveSelected]);
  const canSave =
    session.global_admin ||
    session.memberships.some(
      (m) => m.project_id === project && m.permissions.includes("execute"),
    );
  const unresolved = markers.filter(
    (m) => !confirmed.has(archiveMarkerBinding(m)),
  );
  const pendingIdentity = unresolved.map((item) => item.requestId).join(":");
  const endpoint = `/projects/${project}/campaigns/${campaign}/report-archives`;
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    setCandidate(null);
    setIntegrityChecked(false);
    if (report)
      void readCampaignArchiveCandidate(report, { project, campaign })
        .then((value) => {
          if (!cancelled) {
            setCandidate(value);
            setIntegrityChecked(true);
          }
        })
        .catch(() => {
          if (!cancelled) setIntegrityChecked(true);
        });
    return () => {
      cancelled = true;
    };
  }, [report, project, campaign]);
  useEffect(() => {
    const node = dialog.current;
    if (review) node?.showModal();
    return () => node?.close();
  }, [review]);
  useEffect(() => {
    if (!review && restoreFocus.current) {
      restoreFocus.current = false;
      (opener.current ?? heading.current)?.focus();
    }
  }, [review]);
  useEffect(() => {
    if (receipt) heading.current?.focus({ preventScroll: true });
  }, [receipt]);
  useEffect(() => {
    if (pendingIdentity && !review) pendingHeading.current?.focus();
  }, [pendingIdentity, review]);
  function close() {
    if (inflight.current) return;
    restoreFocus.current = true;
    setReview(null);
  }
  function begin() {
    if (inflight.current) return null;
    inflight.current = true;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    const version = ++generation.current;
    setBusy(true);
    setError("");
    return {
      controller,
      current: () =>
        live.current &&
        !controller.signal.aborted &&
        generation.current === version,
    };
  }
  function finish(op: NonNullable<ReturnType<typeof begin>>) {
    inflight.current = false;
    if (op.current()) setBusy(false);
  }
  function failure(cause: unknown) {
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      active.current?.abort();
      generation.current++;
      setPage(null);
      setReceipt(null);
      setReview(null);
      setMarkers([]);
      onDenied();
      return;
    }
    if (cause instanceof ApiError && cause.status === 404)
      setError(
        "No exact saved report was observed. It may still be in flight, or this server may not support report history. Keep the original request; do not submit it again.",
      );
    else if (cause instanceof ApiError && cause.status === 409)
      setError(
        "The report or history changed before saving could be confirmed. Keep the original request and check its exact receipt; no replacement request was sent.",
      );
    else
      setError(
        "The saved report could not be confirmed. Keep its original identity for an exact check. No request was repeated.",
      );
  }
  async function load(after = 0) {
    const op = begin();
    if (!op) return;
    setReceipt(null);
    setPage(null);
    try {
      const parsed = await readCampaignArchivePage(
        await request<unknown>(connection, `${endpoint}?after=${after}`, {
          signal: op.controller.signal,
        }),
        { project, campaign },
        after,
      );
      if (op.current()) setPage(parsed);
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  async function exact(
    expected: ArchiveExpected,
    op: NonNullable<ReturnType<typeof begin>>,
  ) {
    const parsed = await readCampaignArchiveReceipt(
      await request<unknown>(connection, `${endpoint}/${expected.requestId}`, {
        signal: op.controller.signal,
      }),
      { project, campaign },
      expected,
    );
    if (!op.current()) return;
    setReceipt(parsed);
    try {
      const stored = readArchiveMarkers(localStorage, context);
      setMarkers(stored);
      const marker = stored.find(
        (item) => item.requestId === parsed.request_id,
      );
      if (
        marker &&
        marker.actor === parsed.actor_id &&
        marker.reportDigest === parsed.report.digest
      )
        setConfirmed((old) => new Set([...old, archiveMarkerBinding(marker)]));
    } catch {
      setBlocked(true);
    }
    setError("");
  }
  async function readSummary(item: CampaignArchiveSummary) {
    const op = begin();
    if (!op) return;
    setReceipt(null);
    try {
      await exact(
        {
          requestId: item.request_id,
          actorId: item.actor_id,
          reportDigest: item.report_digest,
          receiptDigest: item.receipt_digest,
        },
        op,
      );
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  async function check(marker: ArchiveMarker) {
    const op = begin();
    if (!op) return;
    setReceipt(null);
    try {
      const stored = readArchiveMarkers(localStorage, context).find(
        (m) => m.requestId === marker.requestId,
      );
      if (!stored || JSON.stringify(stored) !== JSON.stringify(marker))
        throw new Error("Retained identity changed");
      await exact(
        {
          requestId: marker.requestId,
          actorId: marker.actor,
          reportDigest: marker.reportDigest,
        },
        op,
      );
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  async function openReview() {
    if (!canSave || !candidate || blocked || unresolved.length) return;
    const op = begin();
    if (!op) return;
    try {
      const parsed = await readCampaignArchiveCandidate(candidate, {
        project,
        campaign,
      });
      const stored = readArchiveMarkers(localStorage, context);
      if (!op.current()) return;
      setMarkers(stored);
      if (stored.some((m) => !confirmed.has(archiveMarkerBinding(m))))
        throw new Error("Unresolved request");
      if (op.current()) {
        setReview(parsed);
        setStep(0);
        setAck(false);
      }
    } catch {
      if (op.current())
        setError(
          "This report or retained request storage could not be verified. Read the current report and check any original requests before saving.",
        );
    } finally {
      finish(op);
    }
  }
  async function save() {
    if (
      !canSave ||
      !review ||
      step !== 1 ||
      !ack ||
      blocked ||
      unresolved.length
    )
      return;
    const op = begin();
    if (!op) return;
    let marker: ArchiveMarker | null = null;
    try {
      const parsed = await readCampaignArchiveCandidate(review, {
        project,
        campaign,
      });
      if (!op.current()) return;
      marker = retainArchiveMarker(
        localStorage,
        {
          ...context,
          version: 1,
          requestId: crypto.randomUUID().replaceAll("-", ""),
          reportDigest: parsed.digest,
        },
        confirmed,
      );
      setMarkers(readArchiveMarkers(localStorage, context));
      setReview(null);
      setReceipt(null);
      await request<unknown>(connection, endpoint, {
        method: "POST",
        signal: op.controller.signal,
        body: JSON.stringify({
          request_id: marker.requestId,
          expected_report_digest: marker.reportDigest,
        }),
      });
      if (op.current())
        await exact(
          {
            requestId: marker.requestId,
            actorId: marker.actor,
            reportDigest: marker.reportDigest,
          },
          op,
        );
    } catch (cause) {
      if (op.current()) {
        if (!marker) {
          setBlocked(true);
          setReview(null);
        }
        failure(cause);
      }
    } finally {
      finish(op);
    }
  }
  return (
    <section
      className="campaign-report-history"
      aria-label="Saved campaign reports"
      aria-busy={busy}
    >
      <div className="section-heading">
        <div>
          <h3>Saved reports</h3>
          <p className="subtle">
            Keep an exact evidence snapshot for later review. Saving does not
            approve, run, deploy, or refresh work.
          </p>
        </div>
        <div className="campaign-history-actions">
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void load()}
          >
            <RefreshCw size={16} />
            Load saved reports
          </button>
          {canSave && candidate && (
            <button
              className="primary"
              ref={opener}
              disabled={busy || blocked || unresolved.length > 0}
              onClick={() => void openReview()}
            >
              <Archive size={16} />
              Save this report
            </button>
          )}
        </div>
      </div>
      {canSave && report && !candidate && (
        <p role="status">
          {integrityChecked
            ? "Saving is unavailable because this report's evidence cut could not be verified."
            : "Checking report integrity before offering save…"}
        </p>
      )}
      {!canSave && (
        <p className="subtle">
          You can read authorized saved reports. Saving requires project
          execution permission.
        </p>
      )}
      {blocked && (
        <p role="status">
          Retained request storage cannot be verified. Saving is unavailable;
          existing server history can still be read.
        </p>
      )}
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {unresolved.length > 0 && (
        <section
          className="campaign-history-pending"
          aria-label="Retained save requests"
        >
          <h4 ref={pendingHeading} tabIndex={-1}>
            Saved request unresolved
          </h4>
          <p>
            The original identity is retained. A missing receipt never
            authorizes another save.
          </p>
          {unresolved.map((m, i) => (
            <div key={m.requestId}>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void check(m)}
              >
                Check saved request {i + 1}
              </button>
              <details>
                <summary>Original request reference</summary>
                <code>{m.requestId}</code>
                <br />
                <code>{m.reportDigest}</code>
              </details>
            </div>
          ))}
        </section>
      )}
      {markers.length > 0 && (
        <details className="campaign-report-references">
          <summary>Retained save identities</summary>
          <p>
            Confirmed identities stay recoverable. Reloading requires an exact
            check before another save. Other tabs may also save; browser storage
            is not a cross-tab lock.
          </p>
          {markers.map((m, i) => (
            <button
              className="secondary"
              key={m.requestId}
              disabled={busy}
              onClick={() => void check(m)}
            >
              Read retained request {i + 1}
            </button>
          ))}
        </details>
      )}
      {page && (
        <>
          <p role="status">
            {page.items.length
              ? `${page.items.length} saved report${page.items.length === 1 ? "" : "s"} on this page.`
              : "No saved reports were returned for this page."}
          </p>
          <ol className="campaign-history-list">
            {page.items.map((item) => (
              <li key={item.request_id}>
                <div>
                  <strong>Report {item.sequence}</strong>
                  <span>{stateLabel(item.state)}</span>
                  <time dateTime={item.prepared_at}>
                    {new Date(item.prepared_at).toLocaleString()}
                  </time>
                </div>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => void readSummary(item)}
                >
                  Open saved report {item.sequence}
                </button>
              </li>
            ))}
          </ol>
          {page.next_after !== null && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void load(page.next_after!)}
            >
              Next saved reports
            </button>
          )}
        </>
      )}
      {receipt && (
        <section aria-label="Exact saved report">
          <h3 tabIndex={-1} ref={heading}>
            <FileCheck2 size={20} />
            Saved campaign report
          </h3>
          <dl className="campaign-report-summary">
            <div>
              <dt>Saved sequence</dt>
              <dd>{receipt.sequence}</dd>
            </div>
            <div>
              <dt>Prepared by the server</dt>
              <dd>
                <time dateTime={receipt.prepared_at}>
                  {new Date(receipt.prepared_at).toLocaleString()}
                </time>
              </dd>
            </div>
          </dl>
          <p>
            Historical evidence at preparation time. This is not a new live
            report or proof of current SCM synchronization.
          </p>
          <details className="campaign-report-references">
            <summary>Original receipt references</summary>
            <dl>
              <dt>Original actor</dt>
              <dd>{receipt.actor_id}</dd>
              <dt>Request</dt>
              <dd>{receipt.request_id}</dd>
              <dt>Receipt digest</dt>
              <dd>{receipt.digest}</dd>
              <dt>Campaign journal head</dt>
              <dd>{receipt.source.campaign_journal_head}</dd>
            </dl>
          </details>
          {renderReport(receipt.report)}
        </section>
      )}
      {review && (
        <dialog
          ref={dialog}
          className="access-member-dialog campaign-archive-dialog"
          aria-label="Save campaign report"
          onCancel={(e) => {
            e.preventDefault();
            close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Save campaign report</h2>
            <button
              className="quiet"
              aria-label="Close save review"
              disabled={busy}
              onClick={close}
            >
              <X size={20} />
            </button>
          </div>
          <Wizard
            layout="flow"
            steps={["Report", "Review"]}
            active={step}
            onStep={(value) => {
              setStep(value);
              setAck(false);
            }}
            title={
              step === 0 ? "Keep this evidence snapshot" : "Confirm this save"
            }
            description="The server will compare this evidence before storing a receipt. No campaign work will be started."
            locked={busy}
          >
            <div>
              <p>
                Evidence state: <strong>{stateLabel(review.state)}</strong>.
              </p>
              <p>
                Component counts and recorded checks describe the evidence
                already retained. Saving preserves that cut; it does not
                establish live SCM freshness.
              </p>
              <details className="campaign-report-references">
                <summary>Evidence and campaign references</summary>
                <dl>
                  <dt>Campaign</dt>
                  <dd>{campaign}</dd>
                  <dt>Report digest</dt>
                  <dd>{review.digest}</dd>
                  <dt>Server</dt>
                  <dd>{context.base}</dd>
                </dl>
              </details>
              {step === 1 && (
                <label className="campaign-archive-ack">
                  <input
                    type="checkbox"
                    checked={ack}
                    onChange={(e) => setAck(e.target.checked)}
                  />
                  I understand that saving does not approve or run work.
                </label>
              )}
            </div>
            <div className="wizard-actions">
              <button className="secondary" disabled={busy} onClick={close}>
                Cancel
              </button>
              {step === 0 ? (
                <button className="primary" onClick={() => setStep(1)}>
                  Review save
                </button>
              ) : (
                <button
                  className="primary"
                  disabled={!ack || busy}
                  onClick={() => void save()}
                >
                  Save report
                </button>
              )}
            </div>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
