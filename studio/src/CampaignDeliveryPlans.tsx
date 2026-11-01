/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { ListChecks, X } from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import type { CampaignReport } from "./CampaignReportData";
import type { CampaignArchiveReceipt } from "./CampaignReportArchiveData";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  readDeliveryGraph,
  readDeliveryPlanPage,
  readDeliveryPlanReceipt,
  readDeliveryProgress,
  validateDeliveryGraph,
  type DeliveryGraph,
  type DeliveryInput,
  type DeliveryReceipt,
  type DeliverySummary,
  type DeliveryProgress,
  type DeliveryPhase,
} from "./CampaignDeliveryPlanData";
import {
  deliveryMarkerBinding,
  readDeliveryMarkers,
  retainDeliveryMarker,
  type DeliveryMarker,
} from "./CampaignDeliveryPlanMarker";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";
import "./CampaignReportHistory.css";
import "./CampaignDeliveryPlans.css";
const label = (s: string) => s.replaceAll("_", " ");
const newId = () => crypto.randomUUID().replaceAll("-", "");
const slugId = (kind: string) => `${kind}-${newId().slice(0, 12)}`;
export function CampaignDeliveryPlans({
  connection,
  session,
  project,
  campaign,
  report,
  archive,
  onDenied,
}: {
  connection: Connection;
  session: IdentitySession;
  project: string;
  campaign: string;
  report: CampaignReport | null;
  archive: CampaignArchiveReceipt | null;
  onDenied: () => void;
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
  const scope = { project, campaign },
    endpoint = `/projects/${project}/campaigns/${campaign}/delivery-plan-revisions`;
  const [initial] = useState(() => {
    try {
      return {
        items: readDeliveryMarkers(localStorage, context),
        blocked: false,
      };
    } catch {
      return { items: [] as DeliveryMarker[], blocked: true };
    }
  });
  const [markers, setMarkers] = useState(initial.items),
    [blocked, setBlocked] = useState(initial.blocked),
    [confirmed, setConfirmed] = useState<Set<string>>(new Set()),
    [items, setItems] = useState<DeliverySummary[] | null>(null),
    [receipt, setReceipt] = useState<DeliveryReceipt | null>(null),
    [progress, setProgress] = useState<DeliveryProgress | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [draft, setDraft] = useState<DeliveryInput | null>(null),
    [graph, setGraph] = useState<DeliveryGraph | null>(null),
    [step, setStep] = useState(0),
    [ack, setAck] = useState(false),
    [historyPage, setHistoryPage] = useState(0);
  const live = useRef(true),
    generation = useRef(0),
    active = useRef<AbortController | null>(null),
    inflight = useRef(false),
    dialog = useRef<HTMLDialogElement>(null),
    opener = useRef<HTMLButtonElement>(null),
    result = useRef<HTMLHeadingElement>(null),
    pending = useRef<HTMLHeadingElement>(null),
    restore = useRef(false);
  const canSave =
    session.global_admin ||
    session.memberships.some(
      (m) => m.project_id === project && m.permissions.includes("execute"),
    );
  const unresolved = markers.filter(
    (m) => !confirmed.has(deliveryMarkerBinding(m)),
  );
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      active.current?.abort();
    };
  }, []);
  useEffect(() => {
    active.current?.abort();
    generation.current++;
    inflight.current = false;
    setBusy(false);
    setProgress(null);
  }, [archive]);
  const reviewing = Boolean(draft);
  useEffect(() => {
    const node = dialog.current;
    if (!reviewing || !node) return;
    const measure = () => {
      const zoom = Number.parseFloat(
        getComputedStyle(document.documentElement).zoom,
      );
      node.style.setProperty(
        "--delivery-zoom",
        String(Number.isFinite(zoom) && zoom > 0 ? zoom : 1),
      );
    };
    measure();
    node.showModal();
    window.addEventListener("resize", measure);
    return () => {
      window.removeEventListener("resize", measure);
      node.close();
      node.style.removeProperty("--delivery-zoom");
    };
  }, [reviewing]);
  useEffect(() => {
    if (!draft && restore.current) {
      restore.current = false;
      (unresolved.length ? pending.current : opener.current)?.focus();
    }
  }, [draft, unresolved.length]);
  useEffect(() => {
    if (receipt) result.current?.focus({ preventScroll: true });
  }, [receipt]);
  function begin() {
    if (inflight.current) return null;
    inflight.current = true;
    const controller = new AbortController(),
      version = ++generation.current;
    active.current?.abort();
    active.current = controller;
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
    if (!op.current()) return;
    inflight.current = false;
    setBusy(false);
  }
  function stored() {
    try {
      const rows = readDeliveryMarkers(localStorage, context);
      setMarkers(rows);
      return rows;
    } catch (cause) {
      setBlocked(true);
      throw cause;
    }
  }
  function failure(cause: unknown) {
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      active.current?.abort();
      generation.current++;
      setItems(null);
      setReceipt(null);
      setProgress(null);
      setDraft(null);
      setGraph(null);
      setMarkers([]);
      onDenied();
    } else
      setError(
        "The reporting plan or progress could not be confirmed. Keep any original request and check its exact receipt; no request was repeated.",
      );
  }
  async function list(op: NonNullable<ReturnType<typeof begin>>) {
    const page = await readDeliveryPlanPage(
      await request<unknown>(connection, endpoint, {
        signal: op.controller.signal,
      }),
      scope,
    );
    if (op.current()) {
      setItems(page.items);
      setHistoryPage(0);
    }
    return page.items;
  }
  async function load() {
    const op = begin();
    if (!op) return;
    try {
      await list(op);
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  async function exact(
    id: string,
    expected: {
      actor: string;
      requestId: string;
      inputDigest: string;
      receiptDigest?: string;
      revision?: number;
    },
    op: NonNullable<ReturnType<typeof begin>>,
    marker?: DeliveryMarker,
  ) {
    const value = await readDeliveryPlanReceipt(
      await request<unknown>(connection, `${endpoint}/${id}`, {
        signal: op.controller.signal,
      }),
      scope,
      expected,
    );
    if (!op.current()) return value;
    if (marker) {
      if (
        value.previous_digest !== marker.previousDigest ||
        value.source.project_digest !== marker.projectDigest ||
        value.source.request_digest !== marker.campaignDigest ||
        !stored().some(
          (m) => deliveryMarkerBinding(m) === deliveryMarkerBinding(marker),
        )
      )
        throw new Error("Original reporting identity changed");
      setConfirmed((old) => new Set([...old, deliveryMarkerBinding(marker)]));
    }
    setReceipt(value);
    setProgress(null);
    return value;
  }
  async function check(marker: DeliveryMarker) {
    const op = begin();
    if (!op) return;
    try {
      await exact(
        marker.requestId,
        {
          actor: marker.actor,
          requestId: marker.requestId,
          inputDigest: marker.inputDigest,
        },
        op,
        marker,
      );
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  async function read(summary: DeliverySummary) {
    const op = begin();
    if (!op) return;
    try {
      const value = await exact(
        summary.request_id,
        {
          actor: summary.actor_id,
          requestId: summary.request_id,
          inputDigest: summary.request_digest,
          receiptDigest: summary.receipt_digest,
          revision: summary.revision,
        },
        op,
      );
      if (
        value.digest !== summary.receipt_digest ||
        value.revision !== summary.revision
      )
        throw new Error("History changed");
      const marker = stored().find((m) => m.requestId === value.request_id);
      if (
        marker &&
        marker.actor === value.actor_id &&
        marker.inputDigest === value.request_digest &&
        marker.previousDigest === value.previous_digest &&
        marker.projectDigest === value.source.project_digest &&
        marker.campaignDigest === value.source.request_digest
      )
        setConfirmed((old) => new Set([...old, deliveryMarkerBinding(marker)]));
    } catch (cause) {
      if (op.current()) {
        setReceipt(null);
        failure(cause);
      }
    } finally {
      finish(op);
    }
  }
  async function open() {
    if (!canSave || !report || blocked) return;
    const op = begin();
    if (!op) return;
    try {
      if (stored().some((m) => !confirmed.has(deliveryMarkerBinding(m))))
        throw new Error("Unresolved request");
      const history = await list(op);
      const frozen = await readDeliveryGraph(
        await request<unknown>(
          connection,
          `/projects/${project}/campaigns/${campaign}`,
          { signal: op.controller.signal },
        ),
        report,
      );
      const last = history.at(-1);
      let previous: DeliveryReceipt | null = null;
      if (last) {
        previous = await readDeliveryPlanReceipt(
          await request<unknown>(connection, `${endpoint}/${last.request_id}`, {
            signal: op.controller.signal,
          }),
          scope,
          {
            actor: last.actor_id,
            requestId: last.request_id,
            inputDigest: last.request_digest,
            receiptDigest: last.receipt_digest,
            revision: last.revision,
          },
        );
        if (previous.digest !== last.receipt_digest)
          throw new Error("Previous receipt mismatch");
      }
      if (!op.current()) return;
      setGraph(frozen);
      setStep(0);
      setAck(false);
      setDraft({
        request_id: newId(),
        expected_previous_digest: previous?.digest ?? null,
        expected_campaign_request_digest: frozen.campaignDigest,
        expected_project_digest: frozen.projectDigest,
        title: previous?.request.title ?? "",
        waves: structuredClone(previous?.request.waves ?? []),
        milestones: structuredClone(previous?.request.milestones ?? []),
      });
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  function close() {
    if (inflight.current) return;
    restore.current = true;
    setDraft(null);
  }
  function update(next: DeliveryInput) {
    setDraft(next);
    setAck(false);
    setError("");
  }
  function next() {
    if (!draft || !graph) return;
    try {
      if (step === 0) {
        const placed = new Map(
          draft.waves.flatMap((w, i) =>
            w.component_ids.map((id) => [id, i] as const),
          ),
        );
        if (
          !draft.title.trim() ||
          !draft.waves.length ||
          draft.waves.some((w) => !w.name.trim() || !w.component_ids.length) ||
          placed.size !== graph.components.length ||
          draft.waves.flatMap((w) => w.component_ids).length !== placed.size ||
          graph.components.some(
            (c) =>
              !placed.has(c.id) ||
              c.depends_on.some(
                (d) => !placed.has(d) || placed.get(d)! > placed.get(c.id)!,
              ),
          )
        )
          throw new Error("waves");
      } else validateDeliveryGraph(draft, graph);
      setAck(false);
      setStep(step + 1);
      setError("");
    } catch {
      setError(
        step === 0
          ? "Name the plan and waves, assign every campaign component once, and keep dependencies in the same or an earlier wave."
          : "Each wave needs a named milestone with unique conditions from its own components and available task phases.",
      );
    }
  }
  async function save() {
    if (!draft || !graph || !ack || !canSave || blocked) return;
    const op = begin();
    if (!op) return;
    try {
      validateDeliveryGraph(draft, graph);
      const inputDigest = await attemptDigest(draft);
      if (!op.current()) return;
      let marker: DeliveryMarker;
      try {
        marker = retainDeliveryMarker(
          localStorage,
          {
            ...context,
            version: 1,
            requestId: draft.request_id,
            inputDigest,
            previousDigest: draft.expected_previous_digest,
            projectDigest: draft.expected_project_digest,
            campaignDigest: draft.expected_campaign_request_digest,
          },
          confirmed,
        );
        setMarkers(readDeliveryMarkers(localStorage, context));
      } catch (cause) {
        setBlocked(true);
        throw cause;
      }
      const body = draft;
      restore.current = true;
      setDraft(null);
      await request<unknown>(connection, endpoint, {
        method: "POST",
        body: JSON.stringify(body),
        signal: op.controller.signal,
      });
      if (op.current())
        await exact(
          marker.requestId,
          {
            actor: marker.actor,
            requestId: marker.requestId,
            inputDigest: marker.inputDigest,
          },
          op,
          marker,
        );
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  async function readProgress() {
    if (!receipt || !archive) return;
    const op = begin();
    if (!op) return;
    setProgress(null);
    try {
      const value = await readDeliveryProgress(
        await request<unknown>(
          connection,
          `${endpoint}/${receipt.request_id}/progress?report_request_id=${archive.request_id}`,
          { signal: op.controller.signal },
        ),
        receipt,
        archive,
      );
      if (op.current()) setProgress(value);
    } catch (cause) {
      if (op.current()) failure(cause);
    } finally {
      finish(op);
    }
  }
  const componentName = (id: string) =>
    graph?.components.find((c) => c.id === id)?.name ?? id;
  return (
    <section
      className="campaign-delivery-plans"
      aria-label="Campaign reporting plans"
    >
      <div className="section-heading">
        <div>
          <h3>Reporting plans and delivery progress</h3>
          <p className="subtle">
            Organize saved campaign evidence into waves and milestones. These
            criteria do not schedule or start work.
          </p>
        </div>
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void load()}
        >
          <ListChecks size={16} />
          Read reporting plans
        </button>
      </div>
      {canSave && (
        <button
          ref={opener}
          className="primary"
          disabled={
            busy ||
            blocked ||
            !report ||
            report.counts === null ||
            unresolved.length > 0
          }
          onClick={() => void open()}
        >
          Review a reporting plan
        </button>
      )}
      {!report && canSave && (
        <p className="subtle">
          View the campaign report before reviewing its frozen component graph.
        </p>
      )}
      {blocked && (
        <p className="alert" role="alert">
          The original identities could not be retained or read. Keep browser
          storage for review; no plan can be submitted.
        </p>
      )}
      {error && !draft && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {unresolved.length > 0 && (
        <div className="campaign-history-pending">
          <h4 ref={pending} tabIndex={-1}>
            Original reporting-plan request retained
          </h4>
          <p>
            An error or missing receipt cannot authorize another save. Check the
            original request without resending it.
          </p>
          {unresolved.map((m) => (
            <div key={m.requestId}>
              <details>
                <summary>Original request reference</summary>
                <code>{m.requestId}</code>
              </details>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void check(m)}
              >
                Check original reporting plan
              </button>
            </div>
          ))}
        </div>
      )}
      {items?.length === 0 && (
        <p role="status">No reporting plan is saved for this campaign.</p>
      )}
      {items && items.length > 0 && (
        <>
          <ul className="campaign-history-list">
            {items.slice(historyPage * 10, historyPage * 10 + 10).map((s) => (
              <li key={s.request_id}>
                <div>
                  <strong>{s.title}</strong>
                  <span>
                    Revision {s.revision} ·{" "}
                    {new Date(s.prepared_at).toLocaleString()}
                  </span>
                </div>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => void read(s)}
                >
                  Open reporting plan {s.revision}
                </button>
              </li>
            ))}
          </ul>
          {items.length > 10 && (
            <nav
              className="campaign-history-actions"
              aria-label="Reporting plan history pages"
            >
              <button
                className="secondary"
                disabled={historyPage === 0}
                onClick={() => setHistoryPage((p) => p - 1)}
              >
                Previous plans
              </button>
              <button
                className="secondary"
                disabled={(historyPage + 1) * 10 >= items.length}
                onClick={() => setHistoryPage((p) => p + 1)}
              >
                Next plans
              </button>
            </nav>
          )}
        </>
      )}
      {receipt && (
        <div className="delivery-result">
          <h4 ref={result} tabIndex={-1}>
            Reporting plan confirmed
          </h4>
          <p>
            {receipt.request.title} · Revision {receipt.revision}. Saved
            reporting criteria, not execution approval.
          </p>
          <details>
            <summary>Plan structure and original receipt</summary>
            {receipt.request.waves.map((w) => (
              <p key={w.id}>
                {w.name}: {w.component_ids.map(componentName).join(", ")}
              </p>
            ))}
            <dl>
              <dt>Original operator</dt>
              <dd>{receipt.actor_id}</dd>
              <dt>Request</dt>
              <dd>{receipt.request_id}</dd>
              <dt>Receipt</dt>
              <dd>{receipt.digest}</dd>
              <dt>Previous revision digest</dt>
              <dd>{receipt.previous_digest ?? "First revision"}</dd>
            </dl>
          </details>
          {archive ? (
            <>
              <p>
                Selected saved report:{" "}
                {new Date(archive.prepared_at).toLocaleString()} ·{" "}
                {label(archive.report.state)}.
              </p>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void readProgress()}
              >
                Read delivery progress
              </button>
            </>
          ) : (
            <p>
              Select an exact saved report in report history before reading
              delivery progress. No report is archived automatically.
            </p>
          )}
        </div>
      )}
      {progress && (
        <section aria-label="Saved-cut delivery progress">
          <h4>Delivery progress: {label(progress.state)}</h4>
          <p>
            Based on the selected saved report. SCM freshness is not observed;
            this is not a live execution or scheduling view.
          </p>
          {progress.waves.map((w) => (
            <details key={w.id}>
              <summary>
                {w.name} · {label(w.state)}
              </summary>
              <p>
                Observed evidence: {label(w.observed_state)}.{" "}
                {w.blocking_wave_ids.length > 0
                  ? `Waiting for earlier waves: ${w.blocking_wave_ids.map((id) => progress.waves.find((w) => w.id === id)?.name ?? id).join(", ")}.`
                  : "No earlier wave blocks this observed state."}
              </p>
              {progress.milestones
                .filter((m) => m.wave_id === w.id)
                .map((m) => (
                  <details key={m.id}>
                    <summary>
                      {m.name} · {label(m.state)}
                    </summary>
                    <ul>
                      {m.conditions.map((c, i) => (
                        <li key={i}>
                          {componentName(c.component_id)}:{" "}
                          {c.kind === "component_verified"
                            ? "Component verified"
                            : `${label(c.task_id!)} receipt`}{" "}
                          · {label(c.state)}
                          {c.reason && (
                            <p className="subtle">{label(c.reason)}</p>
                          )}
                          <details>
                            <summary>Exact condition evidence</summary>
                            <dl>
                              <dt>Run</dt>
                              <dd>{c.run_id ?? "Not observed"}</dd>
                              <dt>Component attempt</dt>
                              <dd>{c.component_attempt ?? "Not observed"}</dd>
                              <dt>Task attempt</dt>
                              <dd>{c.task_attempt ?? "Not applicable"}</dd>
                              <dt>Receipt sequence</dt>
                              <dd>{c.receipt?.sequence ?? "Not observed"}</dd>
                              <dt>Receipt digest</dt>
                              <dd>{c.receipt?.digest ?? "Not observed"}</dd>
                            </dl>
                          </details>
                        </li>
                      ))}
                    </ul>
                  </details>
                ))}
            </details>
          ))}
          <details>
            <summary>Progress counts and selected references</summary>
            <dl>
              {Object.entries(progress.counts).map(([kind, counts]) => (
                <div key={kind}>
                  <dt>{label(kind)}</dt>
                  <dd>
                    {Object.entries(counts)
                      .map(([state, n]) => `${label(state)}: ${n}`)
                      .join(" · ")}
                  </dd>
                </div>
              ))}
              <dt>Plan receipt</dt>
              <dd>{progress.plan_digest}</dd>
              <dt>Saved report receipt</dt>
              <dd>{progress.report_archive_digest}</dd>
              <dt>Progress digest</dt>
              <dd>{progress.digest}</dd>
            </dl>
          </details>
        </section>
      )}
      {draft && graph && (
        <dialog
          ref={dialog}
          className="access-member-dialog campaign-archive-dialog campaign-delivery-dialog"
          aria-label="Review reporting plan"
          onCancel={(e) => {
            e.preventDefault();
            close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Review reporting plan</h2>
            <button
              className="quiet"
              aria-label="Close reporting plan review"
              onClick={close}
            >
              <X size={20} />
            </button>
          </div>
          <Wizard
            layout="flow"
            steps={["Waves", "Milestones", "Review"]}
            active={step}
            onStep={(value) => {
              if (value <= step) {
                setStep(value);
                setAck(false);
              }
            }}
            title={
              ["Assign components", "Set milestones", "Confirm criteria"][step]
            }
            description="Reporting only. No work is started."
            locked={busy}
          >
            <div>
              {step === 0 && (
                <>
                  <div className="field">
                    <label htmlFor="delivery-title">Plan name</label>
                    <input
                      id="delivery-title"
                      maxLength={200}
                      value={draft.title}
                      onChange={(e) =>
                        update({ ...draft, title: e.target.value })
                      }
                    />
                  </div>
                  {draft.waves.map((w, i) => (
                    <fieldset key={w.id}>
                      <legend>Wave {i + 1}</legend>
                      <div className="field">
                        <label htmlFor={`name-${w.id}`}>
                          Wave {i + 1} name
                        </label>
                        <input
                          id={`name-${w.id}`}
                          maxLength={200}
                          value={w.name}
                          onChange={(e) =>
                            update({
                              ...draft,
                              waves: draft.waves.map((x) =>
                                x.id === w.id
                                  ? { ...x, name: e.target.value }
                                  : x,
                              ),
                            })
                          }
                        />
                      </div>
                      {graph.components.map((c) => (
                        <label className="delivery-check" key={c.id}>
                          <input
                            type="checkbox"
                            checked={w.component_ids.includes(c.id)}
                            onChange={(e) =>
                              update({
                                ...draft,
                                waves: draft.waves.map((x) =>
                                  x.id === w.id
                                    ? {
                                        ...x,
                                        component_ids: e.target.checked
                                          ? [...x.component_ids, c.id]
                                          : x.component_ids.filter(
                                              (id) => id !== c.id,
                                            ),
                                      }
                                    : x,
                                ),
                              })
                            }
                          />
                          {c.name}
                          {c.depends_on.length > 0 && (
                            <small>
                              After {c.depends_on.map(componentName).join(", ")}
                            </small>
                          )}
                        </label>
                      ))}
                      <div className="campaign-history-actions">
                        <button
                          className="secondary"
                          disabled={i === 0}
                          onClick={() => {
                            const waves = [...draft.waves];
                            [waves[i - 1], waves[i]] = [waves[i], waves[i - 1]];
                            update({ ...draft, waves });
                          }}
                        >
                          Move wave {i + 1} earlier
                        </button>
                        <button
                          className="secondary"
                          onClick={() =>
                            update({
                              ...draft,
                              waves: draft.waves.filter((x) => x.id !== w.id),
                              milestones: draft.milestones.filter(
                                (m) => m.wave_id !== w.id,
                              ),
                            })
                          }
                        >
                          Remove wave {i + 1}
                        </button>
                      </div>
                    </fieldset>
                  ))}
                  <button
                    className="secondary"
                    disabled={draft.waves.length >= 20}
                    onClick={() =>
                      update({
                        ...draft,
                        waves: [
                          ...draft.waves,
                          { id: slugId("wave"), name: "", component_ids: [] },
                        ],
                      })
                    }
                  >
                    Add wave
                  </button>
                </>
              )}
              {step === 1 && (
                <>
                  {draft.waves.map((w) => (
                    <fieldset key={w.id}>
                      <legend>{w.name}</legend>
                      {draft.milestones
                        .filter((m) => m.wave_id === w.id)
                        .map((m, mi) => (
                          <div className="delivery-milestone" key={m.id}>
                            <div className="field">
                              <label htmlFor={`milestone-${m.id}`}>
                                Milestone {mi + 1} name in {w.name}
                              </label>
                              <input
                                id={`milestone-${m.id}`}
                                maxLength={200}
                                value={m.name}
                                onChange={(e) =>
                                  update({
                                    ...draft,
                                    milestones: draft.milestones.map((x) =>
                                      x.id === m.id
                                        ? { ...x, name: e.target.value }
                                        : x,
                                    ),
                                  })
                                }
                              />
                            </div>
                            {m.conditions.map((c, ci) => (
                              <fieldset key={ci}>
                                <legend>Condition {ci + 1}</legend>
                                <label>
                                  Component
                                  <select
                                    value={c.component_id}
                                    onChange={(e) =>
                                      update({
                                        ...draft,
                                        milestones: draft.milestones.map((x) =>
                                          x.id === m.id
                                            ? {
                                                ...x,
                                                conditions: x.conditions.map(
                                                  (a, j) =>
                                                    j === ci
                                                      ? {
                                                          ...a,
                                                          component_id:
                                                            e.target.value,
                                                        }
                                                      : a,
                                                ),
                                              }
                                            : x,
                                        ),
                                      })
                                    }
                                  >
                                    {w.component_ids.map((id) => (
                                      <option key={id} value={id}>
                                        {componentName(id)}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                <label>
                                  Evidence required
                                  <select
                                    value={c.kind}
                                    onChange={(e) =>
                                      update({
                                        ...draft,
                                        milestones: draft.milestones.map((x) =>
                                          x.id === m.id
                                            ? {
                                                ...x,
                                                conditions: x.conditions.map(
                                                  (a, j) =>
                                                    j === ci
                                                      ? {
                                                          ...a,
                                                          kind: e.target
                                                            .value as
                                                            | "component_verified"
                                                            | "task_receipted",
                                                          task_id:
                                                            e.target.value ===
                                                            "task_receipted"
                                                              ? "verify"
                                                              : null,
                                                        }
                                                      : a,
                                                ),
                                              }
                                            : x,
                                        ),
                                      })
                                    }
                                  >
                                    <option value="component_verified">
                                      Verified component
                                    </option>
                                    <option value="task_receipted">
                                      Receipted task
                                    </option>
                                  </select>
                                </label>
                                {c.kind === "task_receipted" && (
                                  <label>
                                    Task phase
                                    <select
                                      value={c.task_id ?? "verify"}
                                      onChange={(e) =>
                                        update({
                                          ...draft,
                                          milestones: draft.milestones.map(
                                            (x) =>
                                              x.id === m.id
                                                ? {
                                                    ...x,
                                                    conditions:
                                                      x.conditions.map(
                                                        (a, j) =>
                                                          j === ci
                                                            ? {
                                                                ...a,
                                                                task_id: e
                                                                  .target
                                                                  .value as DeliveryPhase,
                                                              }
                                                            : a,
                                                      ),
                                                  }
                                                : x,
                                          ),
                                        })
                                      }
                                    >
                                      {graph.phases.map((p) => (
                                        <option key={p} value={p}>
                                          {label(p)}
                                        </option>
                                      ))}
                                    </select>
                                  </label>
                                )}
                                <button
                                  className="secondary"
                                  onClick={() =>
                                    update({
                                      ...draft,
                                      milestones: draft.milestones.map((x) =>
                                        x.id === m.id
                                          ? {
                                              ...x,
                                              conditions: x.conditions.filter(
                                                (_, j) => j !== ci,
                                              ),
                                            }
                                          : x,
                                      ),
                                    })
                                  }
                                >
                                  Remove condition {ci + 1}
                                </button>
                              </fieldset>
                            ))}
                            <button
                              className="secondary"
                              disabled={
                                m.conditions.length >= 20 ||
                                draft.milestones.reduce(
                                  (n, x) => n + x.conditions.length,
                                  0,
                                ) >= 200
                              }
                              onClick={() =>
                                update({
                                  ...draft,
                                  milestones: draft.milestones.map((x) =>
                                    x.id === m.id
                                      ? {
                                          ...x,
                                          conditions: [
                                            ...x.conditions,
                                            {
                                              kind: "component_verified",
                                              component_id: w.component_ids[0],
                                              task_id: null,
                                            },
                                          ],
                                        }
                                      : x,
                                  ),
                                })
                              }
                            >
                              Add condition to {m.name || `milestone ${mi + 1}`}
                            </button>
                            <details>
                              <summary>Frozen product links</summary>
                              <p>
                                New product-reference editing requires an
                                authenticated frozen product definition and is
                                not available in this editor. Existing revision
                                links remain unchanged.
                              </p>
                              {[
                                ...m.feature_ids,
                                ...m.journey_refs,
                                ...m.acceptance_refs,
                              ].map((ref) => (
                                <p key={ref}>{ref}</p>
                              ))}
                            </details>
                            <button
                              className="secondary"
                              onClick={() =>
                                update({
                                  ...draft,
                                  milestones: draft.milestones.filter(
                                    (x) => x.id !== m.id,
                                  ),
                                })
                              }
                            >
                              Remove milestone {mi + 1}
                            </button>
                          </div>
                        ))}
                      <button
                        className="secondary"
                        disabled={draft.milestones.length >= 20}
                        onClick={() =>
                          update({
                            ...draft,
                            milestones: [
                              ...draft.milestones,
                              {
                                id: slugId("milestone"),
                                name: "",
                                wave_id: w.id,
                                conditions: [
                                  {
                                    kind: "component_verified",
                                    component_id: w.component_ids[0],
                                    task_id: null,
                                  },
                                ],
                                feature_ids: [],
                                journey_refs: [],
                                acceptance_refs: [],
                              },
                            ],
                          })
                        }
                      >
                        Add milestone to {w.name}
                      </button>
                    </fieldset>
                  ))}
                </>
              )}
              {step === 2 && (
                <>
                  <h3>{draft.title}</h3>
                  {draft.waves.map((w) => (
                    <div key={w.id}>
                      <h4>{w.name}</h4>
                      <p>{w.component_ids.map(componentName).join(", ")}</p>
                      <ul>
                        {draft.milestones
                          .filter((m) => m.wave_id === w.id)
                          .map((m) => (
                            <li key={m.id}>
                              {m.name}:{" "}
                              {m.conditions
                                .map(
                                  (c) =>
                                    `${componentName(c.component_id)} — ${c.kind === "component_verified" ? "verified component" : `${label(c.task_id!)} receipt`}`,
                                )
                                .join("; ")}
                            </li>
                          ))}
                      </ul>
                    </div>
                  ))}
                  <details>
                    <summary>
                      Prior revision and frozen source references
                    </summary>
                    <dl>
                      <dt>Previous receipt</dt>
                      <dd>
                        {draft.expected_previous_digest ?? "First revision"}
                      </dd>
                      <dt>Frozen project</dt>
                      <dd>{draft.expected_project_digest}</dd>
                      <dt>Campaign launch</dt>
                      <dd>{draft.expected_campaign_request_digest}</dd>
                    </dl>
                  </details>
                  <label className="campaign-archive-ack">
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(e) => setAck(e.target.checked)}
                    />
                    I understand: reporting only.
                  </label>
                </>
              )}
              {error && (
                <p className="alert" role="alert">
                  {error}
                </p>
              )}
            </div>
            <div className="wizard-actions">
              <button className="secondary" onClick={close}>
                Cancel
              </button>
              {step < 2 ? (
                <button className="primary" onClick={next}>
                  {step === 0
                    ? "Continue to milestones"
                    : "Review reporting criteria"}
                </button>
              ) : (
                <button
                  className="primary"
                  disabled={!ack || busy}
                  onClick={() => void save()}
                >
                  Save reporting plan
                </button>
              )}
            </div>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
