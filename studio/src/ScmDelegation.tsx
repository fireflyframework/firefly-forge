/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  GitBranch,
  History,
  ShieldCheck,
  UserRound,
  X,
} from "lucide-react";
import {
  ApiError,
  normalizeBase,
  request,
  type Connection,
  type ProjectSnapshot,
} from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  delegationBindingKey,
  delegationInput,
  delegationMarkerBinding,
  readDelegationCandidates,
  readDelegationHeads,
  readDelegationMarkers,
  readDelegationReceipt,
  retainDelegationMarker,
  type CandidatePage,
  type DelegatedAction,
  type DelegationCandidate,
  type DelegationContext,
  type DelegationHead,
  type DelegationInput,
  type DelegationMarker,
  type DelegationReceipt,
} from "./ScmDelegationData";
import "./ScmDelegation.css";

type Person = {
  id: string;
  display_name: string | null;
  email: string | null;
  contact_email?: string | null;
  enabled: boolean;
};
const actionNames: Record<DelegatedAction, string> = {
  respond: "Answer agent questions",
  status: "Read run status",
  cancel: "Request cancellation",
};
const statusNames = {
  active: "Recorded as active",
  revoked: "Revoked",
  stale_project: "Project changed",
  stale_connection: "Connection changed",
};
const personName = (person?: Person) =>
  person?.display_name ||
  person?.contact_email ||
  person?.email ||
  "Registered account";
const unavailable =
  "Delegation information is unavailable on this server. This is not an empty or revoked configuration.";
const failed =
  "The delegation result is unconfirmed. Keep the original request and check it without sending again.";
function problem(cause: unknown): string {
  return cause instanceof ApiError && cause.status === 404
    ? unavailable
    : cause instanceof ApiError && [401, 403].includes(cause.status)
      ? "Current access was refused. Private delegation details have been cleared."
      : "The delegation response could not be verified. Keep any original request for review.";
}
export function ScmDelegation({
  connection,
  session,
  project,
  people,
  onConfigure,
  onProjectRefresh,
}: {
  connection: Connection;
  session: IdentitySession;
  project: ProjectSnapshot;
  people: Person[];
  onConfigure: () => void;
  onProjectRefresh: () => void;
}) {
  const repositories = [
    ...new Set(project.definition.components.map((item) => item.repository_id)),
  ];
  const [repository, setRepository] = useState(repositories[0] ?? "");
  const context: DelegationContext = {
    base: new URL(
      normalizeBase(connection.base),
      window.location.href,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    project: project.id,
    repository,
  };
  const canManage =
    session.global_admin &&
    session.user.authentication_method !== "scm_mapping";
  const path = `/projects/${project.id}/repositories/${repository}/scm-actor-bindings`;
  const [heads, setHeads] = useState<DelegationHead[]>([]),
    [read, setRead] = useState(false),
    [candidates, setCandidates] = useState<CandidatePage | null>(null);
  const [markers, setMarkers] = useState<DelegationMarker[]>([]),
    [confirmed, setConfirmed] = useState<Set<string>>(new Set()),
    [tracking, setTracking] = useState("");
  const [receipt, setReceipt] = useState<DelegationReceipt | null>(null),
    [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false),
    [step, setStep] = useState(0),
    [candidateId, setCandidateId] = useState(""),
    [previous, setPrevious] = useState<DelegationReceipt | null>(null);
  const [query, setQuery] = useState(""),
    [matches, setMatches] = useState<Person[]>([]),
    [person, setPerson] = useState<Person | null>(null);
  const [components, setComponents] = useState<string[]>([]),
    [actions, setActions] = useState<DelegatedAction[]>([]),
    [active, setActive] = useState(true),
    [ack, setAck] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null),
    trigger = useRef<HTMLButtonElement>(null),
    recovery = useRef<HTMLHeadingElement>(null);
  const generation = useRef(0),
    current = useRef<AbortController | null>(null),
    inflight = useRef(false),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      current.current?.abort();
    };
  }, []);
  useEffect(() => {
    current.current?.abort();
    generation.current++;
    inflight.current = false;
    setBusy(false);
    setHeads([]);
    setRead(false);
    setReceipt(null);
    setCandidates(null);
    setOpen(false);
    setError("");
    setMessage("");
    setConfirmed(new Set());
    try {
      setMarkers(readDelegationMarkers(localStorage, context));
      setTracking("");
    } catch {
      setMarkers([]);
      setTracking(
        "The retained request history cannot be read. New delegation requests are held; preserve browser storage for review.",
      );
    }
    // This keyed component remounts for changes to API, identity, permissions or project cut.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repository]);
  useEffect(() => {
    const node = dialog.current;
    if (!open || !node) return;
    const measure = () => {
      const value = Number.parseFloat(
        getComputedStyle(document.documentElement).zoom,
      );
      const zoom = Number.isFinite(value) && value > 0 ? value : 1;
      node.style.setProperty("--delegation-zoom", String(zoom));
      node.dataset.compact = String(window.innerHeight / zoom <= 600);
    };
    measure();
    node.showModal();
    window.addEventListener("resize", measure);
    return () => {
      window.removeEventListener("resize", measure);
      node.close();
    };
  }, [open]);
  function begin() {
    if (inflight.current) return null;
    current.current?.abort();
    const controller = new AbortController();
    current.current = controller;
    const value = ++generation.current;
    inflight.current = true;
    setBusy(true);
    setError("");
    return {
      signal: controller.signal,
      valid: () =>
        alive.current &&
        !controller.signal.aborted &&
        generation.current === value,
    };
  }
  function finish(op: NonNullable<ReturnType<typeof begin>>) {
    if (!op.valid()) return;
    inflight.current = false;
    setBusy(false);
  }
  function clearDenied(cause: unknown) {
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setHeads([]);
      setRead(false);
      setCandidates(null);
      setReceipt(null);
      setOpen(false);
      setPerson(null);
      setMatches([]);
      setQuery("");
      setPrevious(null);
      setMessage("");
    }
  }
  function stored() {
    const value = readDelegationMarkers(localStorage, context);
    setMarkers(value);
    return value;
  }
  function held() {
    return stored().some(
      (item) => !confirmed.has(delegationMarkerBinding(item)),
    );
  }
  function close() {
    setOpen(false);
    requestAnimationFrame(() => trigger.current?.focus());
  }
  async function allHeads(signal: AbortSignal) {
    let cursor: string | null = null,
      cut: string | null = null;
    const result: DelegationHead[] = [],
      keys = new Set<string>();
    for (let page = 0; page < 2; page++) {
      const data = await readDelegationHeads(
        await request<unknown>(
          connection,
          path + (cursor ? `?cursor=${cursor}` : ""),
          { signal },
        ),
        context,
        cursor,
      );
      if (cut !== null && cut !== data.project_binding_digest)
        throw new Error("Project changed");
      cut = data.project_binding_digest;
      for (const item of data.items) {
        if (keys.has(item.binding_key))
          throw new Error("Current heads changed");
        keys.add(item.binding_key);
        result.push(item);
      }
      cursor = data.next_cursor;
      if (cursor === null) return { items: result, cut };
    }
    throw new Error("Current heads exceed the bounded read");
  }
  async function readCurrent() {
    const op = begin();
    if (!op) return;
    try {
      const data = await allHeads(op.signal);
      if (!op.valid()) return;
      setHeads(data.items);
      setRead(true);
      setMessage(
        "Current head records read. Active is a saved setting, not proof of current provider grants or the delegated person's permissions.",
      );
    } catch (cause) {
      if (op.valid()) {
        clearDenied(cause);
        setError(problem(cause));
      }
    } finally {
      finish(op);
    }
  }
  async function start() {
    try {
      if (held()) {
        setError(failed);
        return;
      }
    } catch {
      setTracking(
        "Retained tracking is unavailable. No new delegation request can be sent.",
      );
      return;
    }
    const op = begin();
    if (!op) return;
    try {
      const [data, page, reviewedCut] = await Promise.all([
        allHeads(op.signal),
        request<unknown>(connection, path + "/candidates", {
          signal: op.signal,
        }).then((value) => readDelegationCandidates(value, context)),
        attemptDigest(project),
      ]);
      if (!op.valid()) return;
      if (
        data.cut !== reviewedCut ||
        page.project_binding_digest !== reviewedCut
      ) {
        onProjectRefresh();
        throw new Error("Project cut changed");
      }
      setHeads(data.items);
      setRead(true);
      setCandidates(page);
      setCandidateId("");
      setPrevious(null);
      setPerson(null);
      setQuery("");
      setMatches([]);
      setComponents([]);
      setActions([]);
      setActive(true);
      setAck(false);
      setStep(0);
      setOpen(true);
    } catch (cause) {
      if (op.valid()) {
        clearDenied(cause);
        setError(problem(cause));
      }
    } finally {
      finish(op);
    }
  }
  async function exact(
    requestId: string,
    expected: Parameters<typeof readDelegationReceipt>[1],
    signal: AbortSignal,
  ) {
    return readDelegationReceipt(
      await request<unknown>(connection, `${path}/requests/${requestId}`, {
        signal,
      }),
      expected,
    );
  }
  async function selectCandidate(value: string) {
    if (!candidates) return;
    const op = begin();
    if (!op) return;
    try {
      const candidate = candidates.items.find(
        (item) => item.verified_receipt_id === value,
      );
      if (!candidate) throw new Error("Missing candidate");
      const key = await delegationBindingKey(
          project.id,
          candidate.verified_reference,
        ),
        head = heads.find((item) => item.binding_key === key);
      const prior = head
        ? await exact(
            head.request_id,
            {
              project: project.id,
              repository,
              requestId: head.request_id,
              eventId: head.event_id,
              receiptDigest: head.digest,
            },
            op.signal,
          )
        : null;
      if (!op.valid()) return;
      setCandidateId(value);
      setPrevious(prior);
      setPerson(null);
      setQuery("");
      setMatches([]);
      setComponents(
        prior?.request.component_ids.filter((id) =>
          project.definition.components.some(
            (item) => item.id === id && item.repository_id === repository,
          ),
        ) ?? [],
      );
      setActions(prior?.request.allowed_actions ?? []);
      setActive(prior?.request.active ?? true);
      setAck(false);
    } catch (cause) {
      if (op.valid()) {
        clearDenied(cause);
        setCandidateId("");
        setError(problem(cause));
      }
    } finally {
      finish(op);
    }
  }
  async function findPerson() {
    const term = query.trim();
    if (term.length < 3 || term.length > 120) {
      setError(
        "Enter 3 to 120 characters to find an existing registered person.",
      );
      return;
    }
    const op = begin();
    if (!op) return;
    try {
      const values = await request<unknown>(
        connection,
        `/projects/${project.id}/eligible-users?query=${encodeURIComponent(term)}`,
        { signal: op.signal },
      );
      if (
        !Array.isArray(values) ||
        values.length > 20 ||
        values.some(
          (item) =>
            !item ||
            typeof item.id !== "string" ||
            !/^[a-f0-9]{32}$/.test(item.id) ||
            item.enabled !== true ||
            [item.display_name, item.email, item.contact_email].some(
              (label) =>
                label != null &&
                (typeof label !== "string" || label.length > 320),
            ),
        )
      )
        throw new Error("Invalid people result");
      if (op.valid()) {
        setMatches(values as Person[]);
        setPerson(null);
      }
    } catch (cause) {
      if (op.valid()) {
        clearDenied(cause);
        setMatches([]);
        setError(problem(cause));
      }
    } finally {
      finish(op);
    }
  }
  async function openReceipt(
    requestId: string,
    head?: DelegationHead,
    marker?: DelegationMarker,
  ) {
    const op = begin();
    if (!op) return;
    try {
      const data = await exact(
        requestId,
        marker
          ? {
              ...context,
              requestId,
              actor: marker.actor,
              inputDigest: marker.inputDigest,
            }
          : {
              project: project.id,
              repository,
              requestId,
              eventId: head?.event_id,
              receiptDigest: head?.digest,
            },
        op.signal,
      );
      if (!op.valid()) return;
      setReceipt(data);
      setMessage(
        "Exact delegation revision read. This historical receipt does not establish present provider access.",
      );
      const saved = stored().find((item) => item.requestId === requestId);
      if (
        saved &&
        saved.actor === data.administering_user_id &&
        saved.inputDigest === data.request_digest
      )
        setConfirmed(
          (old) => new Set([...old, delegationMarkerBinding(saved)]),
        );
      requestAnimationFrame(() => recovery.current?.focus());
    } catch (cause) {
      if (op.valid()) {
        clearDenied(cause);
        setError(problem(cause));
      }
    } finally {
      finish(op);
    }
  }
  async function review() {
    try {
      if (held()) throw new Error("Tracking changed");
      if (!person || !components.length || !actions.length) {
        setError(
          "Choose an existing person, at least one component and at least one action.",
        );
        return;
      }
      setAck(false);
      setStep(2);
      setError("");
    } catch {
      setTracking(
        "The retained request binding changed or is unresolved. Check the original request; no new save is allowed.",
      );
      close();
    }
  }
  async function save() {
    if (!canManage || !ack || !person || !candidates) return;
    const candidate = candidates.items.find(
      (item) => item.verified_receipt_id === candidateId,
    );
    if (!candidate) return;
    const op = begin();
    if (!op) return;
    let retained: DelegationMarker | null = null;
    try {
      if (held()) throw new Error("Held original identity");
      const body: DelegationInput = delegationInput({
        version: 1,
        request_id: crypto.randomUUID().replaceAll("-", ""),
        expected_previous_digest: previous?.digest ?? null,
        expected_project_binding_digest: candidates.project_binding_digest,
        verified_receipt_id: candidate.verified_receipt_id,
        expected_verified_reference_digest: candidate.verified_reference_digest,
        native_user_id: person.id,
        component_ids: components,
        allowed_actions: actions,
        active,
        acknowledge_webhook_asserted_identity: true,
      });
      const inputDigest = await attemptDigest(body);
      if (!op.valid()) return;
      retained = retainDelegationMarker(
        localStorage,
        { ...context, version: 1, requestId: body.request_id, inputDigest },
        confirmed,
      );
      const tracked = stored().find(
        (item) => item.requestId === retained!.requestId,
      );
      if (
        !tracked ||
        delegationMarkerBinding(tracked) !== delegationMarkerBinding(retained)
      )
        throw new Error("Retained identity changed before dispatch");
      setOpen(false);
      setHeads([]);
      setRead(false);
      setMessage(
        "Original delegation request retained. Waiting for its exact receipt…",
      );
      await request<unknown>(connection, path, {
        method: "POST",
        body: JSON.stringify(body),
        signal: op.signal,
      });
      const data = await exact(
        retained.requestId,
        {
          ...context,
          requestId: retained.requestId,
          actor: retained.actor,
          inputDigest: retained.inputDigest,
        },
        op.signal,
      );
      if (!op.valid()) return;
      const latest = stored().find(
        (item) => item.requestId === retained!.requestId,
      );
      if (
        !latest ||
        delegationMarkerBinding(latest) !== delegationMarkerBinding(retained)
      )
        throw new Error("Retained identity changed");
      setReceipt(data);
      setConfirmed(
        (old) => new Set([...old, delegationMarkerBinding(retained!)]),
      );
      setMessage(
        "Delegation recorded. Each later action still requires current native permissions and application access.",
      );
    } catch (cause) {
      if (op.valid()) {
        clearDenied(cause);
        setError(
          retained
            ? failed
            : "The request could not be retained safely. No new delegation request was sent.",
        );
        if (!retained)
          setTracking(
            "Tracking must be readable and preserve all unresolved requests before saving.",
          );
      }
    } finally {
      if (op.valid()) requestAnimationFrame(() => recovery.current?.focus());
      finish(op);
    }
  }
  const candidate: DelegationCandidate | undefined = candidates?.items.find(
    (item) => item.verified_receipt_id === candidateId,
  );
  const known = (id: string) =>
    personName(people.find((item) => item.id === id));
  const unresolved = markers.filter(
    (item) => !confirmed.has(delegationMarkerBinding(item)),
  );
  return (
    <section className="scm-delegation" aria-label="Source-control delegation">
      <div className="section-heading">
        <div>
          <h2>
            <GitBranch size={22} /> Source-control delegation
          </h2>
          <p>
            Let a webhook-reported account request specific actions as an
            existing native user. This grants no project membership.
          </p>
        </div>
      </div>
      <div className="field">
        <label htmlFor="scm-delegation-repository">Component repository</label>
        <select
          id="scm-delegation-repository"
          value={repository}
          disabled={busy}
          onChange={(event) => setRepository(event.target.value)}
        >
          {repositories.map((id) => (
            <option key={id} value={id}>
              {project.definition.components
                .filter((item) => item.repository_id === id)
                .map((item) => item.name)
                .join(" / ")}
            </option>
          ))}
        </select>
      </div>
      <div className="scm-delegation-actions">
        <button
          className="secondary"
          disabled={busy || !repository}
          onClick={() => void readCurrent()}
        >
          <History size={16} />
          Read current delegations
        </button>
        {canManage && (
          <button
            className="primary"
            ref={trigger}
            disabled={
              busy || !repository || Boolean(tracking) || unresolved.length > 0
            }
            onClick={() => void start()}
          >
            <ShieldCheck size={16} />
            Review a delegation
          </button>
        )}
      </div>
      {!canManage && (
        <p>A factory administrator must review delegation changes.</p>
      )}
      {tracking && (
        <p role="alert" className="alert">
          {tracking}
        </p>
      )}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {busy && <p role="status">Reading or saving the reviewed delegation…</p>}
      {message && <p role="status">{message}</p>}
      {!!markers.length && (
        <section className="scm-delegation-recovery">
          <h3 ref={recovery} tabIndex={-1}>
            Retained delegation requests
          </h3>
          <p>
            A missing receipt or error cannot authorize another request. These
            are the references retained by this browser, not a complete server
            history. A conflict can be refused before a receipt is saved. Keep
            the original reference and ask an administrator to check the project
            and delegation; a missing receipt does not permit another save.
          </p>
          {markers.map((item, index) => (
            <div key={item.requestId}>
              <p>
                {confirmed.has(delegationMarkerBinding(item))
                  ? "Exact receipt confirmed"
                  : "Original result unconfirmed"}
              </p>
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  void openReceipt(item.requestId, undefined, item)
                }
              >
                Check original delegation {index + 1}
              </button>
              <details>
                <summary>Original request reference</summary>
                <code>{item.requestId}</code>
              </details>
            </div>
          ))}
        </section>
      )}
      {read && !heads.length && (
        <p>
          No current delegation heads are recorded for this project repository.
        </p>
      )}
      {heads.map((head) => (
        <article className="scm-delegation-record" key={head.binding_key}>
          <h3>
            {head.provider === "github"
              ? "GitHub"
              : head.provider === "gitlab"
                ? "GitLab"
                : "Bitbucket"}{" "}
            account {head.external_actor_id}
          </h3>
          <p>
            {statusNames[head.head_status]} · revision {head.revision}
          </p>
          <p>
            {known(head.native_user_id)} ·{" "}
            {head.component_ids
              .map(
                (id) =>
                  project.definition.components.find((item) => item.id === id)
                    ?.name ?? "Historical component",
              )
              .join(", ")}
          </p>
          <p>
            {head.allowed_actions
              .map((action) => actionNames[action])
              .join(" · ")}
          </p>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void openReceipt(head.request_id, head)}
          >
            Read exact revision {head.revision}
          </button>
        </article>
      ))}
      {receipt && (
        <section
          className="scm-delegation-receipt"
          aria-label="Exact delegation receipt"
        >
          <h3>Recorded delegation revision {receipt.revision}</h3>
          <p>
            {receipt.request.active
              ? "This revision recorded an active setting."
              : "This revision revoked the delegation."}{" "}
            Current permissions and provider access are checked separately.
          </p>
          <p>
            {known(receipt.request.native_user_id)} ·{" "}
            {receipt.request.allowed_actions
              .map((action) => actionNames[action])
              .join(" · ")}
          </p>
          <p>Prepared {new Date(receipt.prepared_at).toLocaleString()}</p>
          <details>
            <summary>Exact receipt references</summary>
            <dl>
              {Object.entries({
                Request: receipt.request.request_id,
                Event: receipt.event_id,
                Administrator: receipt.administering_user_id,
                "Project cut": receipt.project_binding_digest,
                "Input digest": receipt.request_digest,
                "Receipt digest": receipt.digest,
              }).map(([name, value]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>
                    <code>{value}</code>
                  </dd>
                </div>
              ))}
            </dl>
          </details>
          <button className="secondary" onClick={() => setReceipt(null)}>
            Close receipt
          </button>
        </section>
      )}
      {open && (
        <dialog
          ref={dialog}
          className="access-member-dialog scm-delegation-dialog"
          aria-label="Review SCM delegation"
          onCancel={(event) => {
            if (busy) event.preventDefault();
            else close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Review delegation</h2>
            <button
              className="quiet"
              aria-label="Close delegation review"
              disabled={busy}
              onClick={close}
            >
              <X size={20} />
            </button>
          </div>
          <Wizard
            steps={["Account", "Scope", "Review"]}
            active={step}
            onStep={(value) => {
              setStep(value);
              setAck(false);
            }}
            locked={busy}
            title={
              ["Choose an account", "Choose scope", "Confirm delegation"][step]
            }
            description={
              [
                "Choose a signed delivery.",
                "No membership is created.",
                "No personal OAuth is granted.",
              ][step]
            }
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (step === 0 && candidate) setStep(1);
                else if (step === 1) void review();
                else if (step === 2) void save();
              }}
            >
              {step === 0 && (
                <>
                  <p>
                    The factory verified a delivery signature from the
                    configured webhook sender. The account below was asserted in
                    that delivery; it is not a personal OAuth login.
                  </p>
                  {!candidates?.items.length ? (
                    <div className="empty">
                      <p>
                        No current signed delivery is available for this
                        repository. Configure the application and receive a
                        signed delivery before reviewing delegation.
                      </p>
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => {
                          close();
                          onConfigure();
                        }}
                      >
                        Open connections
                        <ArrowRight size={16} />
                      </button>
                    </div>
                  ) : (
                    <div className="field">
                      <label htmlFor="scm-reported-account">
                        Webhook-reported account
                      </label>
                      <select
                        id="scm-reported-account"
                        value={candidateId}
                        disabled={busy}
                        onChange={(event) =>
                          void selectCandidate(event.target.value)
                        }
                      >
                        <option value="">
                          Choose a retained signed delivery
                        </option>
                        {candidates.items.map((item, index) => (
                          <option
                            key={item.verified_receipt_id}
                            value={item.verified_receipt_id}
                          >
                            {item.verified_reference.provider} account{" "}
                            {item.verified_reference.external_actor_id} ·
                            reference {index + 1}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                  {candidate && (
                    <details>
                      <summary>Application and signed-delivery details</summary>
                      <p>{candidate.verified_reference.api_origin}</p>
                      <p>
                        Application installation:{" "}
                        {candidate.verified_reference.installation_id ??
                          "Not used by this provider"}
                      </p>
                      <code>{candidate.verified_receipt_id}</code>
                    </details>
                  )}
                </>
              )}
              {step === 1 && (
                <>
                  <div className="field">
                    <label htmlFor="scm-person-query">
                      Find a registered person
                    </label>
                    <input
                      id="scm-person-query"
                      disabled={busy}
                      value={query}
                      maxLength={120}
                      onChange={(event) => {
                        setQuery(event.target.value);
                        setPerson(null);
                        setMatches([]);
                      }}
                    />
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() => void findPerson()}
                    >
                      <UserRound size={16} />
                      Find person
                    </button>
                  </div>
                  <div className="field">
                    <label htmlFor="scm-person">Registered person</label>
                    <select
                      id="scm-person"
                      value={person?.id ?? ""}
                      onChange={(event) =>
                        setPerson(
                          matches.find(
                            (item) => item.id === event.target.value,
                          ) ?? null,
                        )
                      }
                    >
                      <option value="">
                        Choose an existing registered person
                      </option>
                      {matches.map((item) => (
                        <option key={item.id} value={item.id}>
                          {personName(item)}
                        </option>
                      ))}
                    </select>
                  </div>
                  <p>
                    This does not add the person to the project. Each later
                    action still needs their current project and repository
                    permission.
                  </p>
                  <fieldset>
                    <legend>Components in this repository</legend>
                    {project.definition.components
                      .filter((item) => item.repository_id === repository)
                      .map((item) => (
                        <label className="scm-delegation-check" key={item.id}>
                          <input
                            type="checkbox"
                            checked={components.includes(item.id)}
                            onChange={(event) =>
                              setComponents((old) =>
                                event.target.checked
                                  ? [...old, item.id]
                                  : old.filter((id) => id !== item.id),
                              )
                            }
                          />
                          {item.name}
                        </label>
                      ))}
                  </fieldset>
                  <fieldset>
                    <legend>Allowed requests</legend>
                    {(Object.keys(actionNames) as DelegatedAction[]).map(
                      (action) => (
                        <label className="scm-delegation-check" key={action}>
                          <input
                            type="checkbox"
                            checked={actions.includes(action)}
                            onChange={(event) =>
                              setActions((old) =>
                                event.target.checked
                                  ? [...old, action]
                                  : old.filter((item) => item !== action),
                              )
                            }
                          />
                          {actionNames[action]}
                        </label>
                      ),
                    )}
                  </fieldset>
                  <label className="scm-delegation-check">
                    <input
                      type="checkbox"
                      checked={active}
                      onChange={(event) => setActive(event.target.checked)}
                    />
                    Allow the listed requests. Clear to record revocation.
                  </label>
                </>
              )}
              {step === 2 && (
                <>
                  <p>
                    <strong>{personName(person ?? undefined)}</strong> will be
                    the native user for the selected account's requests.
                  </p>
                  <p>
                    {components
                      .map(
                        (id) =>
                          project.definition.components.find(
                            (item) => item.id === id,
                          )?.name,
                      )
                      .join(", ")}
                  </p>
                  <p>
                    {actions.map((action) => actionNames[action]).join(" · ")}
                  </p>
                  <p>
                    {active
                      ? "Record an active delegation."
                      : "Revoke this delegation."}{" "}
                    Saving does not change membership, answer a question, resume
                    a run or cancel work.
                  </p>
                  <p>
                    A trusted webhook sender can assert this account. Review the
                    application connection and sender trust before saving.
                  </p>
                  <details>
                    <summary>Reviewed source and previous revision</summary>
                    <dl>
                      <dt>Stored project cut</dt>
                      <dd>
                        <code>{candidates?.project_binding_digest}</code>
                      </dd>
                      <dt>Previous revision</dt>
                      <dd>
                        {previous
                          ? `${previous.revision}`
                          : "No current head was returned by the complete bounded read."}
                      </dd>
                    </dl>
                  </details>
                  <label className="scm-delegation-check">
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(event) => setAck(event.target.checked)}
                    />
                    I trust this webhook assertion.
                  </label>
                </>
              )}
              {error && (
                <p role="alert" className="alert">
                  {error}
                </p>
              )}
              <div className="wizard-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={close}
                >
                  Cancel
                </button>
                <button
                  className="primary"
                  disabled={
                    busy ||
                    (step === 0 && !candidate) ||
                    (step === 1 &&
                      (!person || !components.length || !actions.length)) ||
                    (step === 2 && !ack)
                  }
                >
                  {step === 0
                    ? "Continue to scope"
                    : step === 1
                      ? "Review delegation"
                      : "Save delegation"}
                </button>
              </div>
            </form>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}
