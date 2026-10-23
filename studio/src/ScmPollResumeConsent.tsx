/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { ScmPollFunctionalConsent } from "./ScmPollFunctionalConsent";
import { useEffect, useRef, useState } from "react";
import { GitBranch, History, ShieldCheck, X } from "lucide-react";
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
import type { DelegationContext } from "./ScmDelegationData";
import {
  readPollingBindingReceipt,
  type PollingBindingReceipt,
} from "./ScmPollingData";
import {
  readPollResumeHeads,
  readPollResumeMarkers,
  readPollResumeReceipt,
  pollResumeGrantReference,
  pollResumeInput,
  pollResumeMarkerBinding,
  retainPollResumeMarker,
  type PollResumeInput,
  type PollResumeMarker,
  type PollResumeReceipt,
} from "./ScmPollResumeData";
import "./ScmResumeConsent.css";

export function ScmPollResumeConsent({
  connection,
  session,
  project,
  binding,
  personName,
}: {
  connection: Connection;
  session: IdentitySession;
  project: ProjectSnapshot;
  binding: PollingBindingReceipt;
  personName: string;
}) {
  const scope: DelegationContext = {
    base: new URL(
      normalizeBase(connection.base),
      window.location.href,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    project: project.id,
    repository: binding.repository_id,
  };
  const canManage =
    session.global_admin &&
    session.user.authentication_method !== "scm_mapping";
  const canRead =
    session.global_admin ||
    Boolean(
      session.memberships
        .find((item) => item.project_id === project.id)
        ?.permissions.includes("read"),
    );
  const scopeKey = JSON.stringify([
    scope,
    connection.token,
    canManage,
    canRead,
    binding.digest,
    project.revision,
    project.digest,
  ]);
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;
  const generation = useRef(0),
    inflight = useRef(false),
    controller = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState(false),
    [current, setCurrent] = useState<PollResumeReceipt | null>(null),
    [viewed, setViewed] = useState<PollResumeReceipt | null>(null),
    [loaded, setLoaded] = useState(false);
  const [markers, setMarkers] = useState<PollResumeMarker[]>([]),
    [confirmed, setConfirmed] = useState<Set<string>>(new Set()),
    [tracking, setTracking] = useState("");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [open, setOpen] = useState(false),
    [step, setStep] = useState(0),
    [components, setComponents] = useState<string[]>([]),
    [active, setActive] = useState(true),
    [ack, setAck] = useState(false),
    [review, setReview] = useState<PollResumeInput | null>(null);
  const dialog = useRef<HTMLDialogElement>(null),
    trigger = useRef<HTMLButtonElement>(null),
    recovery = useRef<HTMLHeadingElement>(null);
  const path = `/projects/${scope.project}/repositories/${scope.repository}/scm-polling/resume-grants`,
    bindingPath = `/projects/${scope.project}/repositories/${scope.repository}/scm-polling/bindings`;
  useEffect(() => {
    controller.current?.abort();
    generation.current++;
    inflight.current = false;
    setBusy(false);
    setCurrent(null);
    setViewed(null);
    setLoaded(false);
    setOpen(false);
    setReview(null);
    setConfirmed(new Set());
    setError("");
    setNotice("");
    try {
      setMarkers(readPollResumeMarkers(localStorage, scope));
      setTracking("");
    } catch {
      setMarkers([]);
      setTracking(
        "Original consent requests cannot be read. New requests are held; preserve browser storage for review.",
      );
    }
    return () => {
      controller.current?.abort();
    };
    // The identity includes the selected API-observed V2 receipt and current native permissions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey]);
  useEffect(() => {
    if (!open || !dialog.current) return;
    const node = dialog.current;
    const measure = () => {
      const value = parseFloat(getComputedStyle(document.documentElement).zoom),
        zoom = Number.isFinite(value) && value > 0 ? value : 1;
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
    const abort = new AbortController(),
      cycle = ++generation.current,
      key = scopeKey;
    controller.current?.abort();
    controller.current = abort;
    inflight.current = true;
    setBusy(true);
    setError("");
    return {
      signal: abort.signal,
      valid: () =>
        !abort.signal.aborted &&
        generation.current === cycle &&
        currentScope.current === key,
    };
  }
  type Operation = NonNullable<ReturnType<typeof begin>>;
  function finish(op: Operation) {
    if (op.valid()) {
      inflight.current = false;
      setBusy(false);
    }
  }
  function originalRequests() {
    const saved = readPollResumeMarkers(localStorage, scope);
    setMarkers(saved);
    if (saved.some((item) => !confirmed.has(pollResumeMarkerBinding(item))))
      throw new Error(
        "Check every original consent request before reviewing another.",
      );
  }
  function fail(cause: unknown, op: Operation, retained = false) {
    if (!op.valid()) return;
    if (cause instanceof ApiError && [401, 403].includes(cause.status)) {
      setCurrent(null);
      setViewed(null);
      setLoaded(false);
      setOpen(false);
      setReview(null);
      setConfirmed(new Set());
      setNotice("");
      setError(
        "Current access was refused. Private consent details have been cleared. Original requests remain retained.",
      );
    } else {
      if (!retained) {
        setOpen(false);
        setReview(null);
      }
      setError(
        retained
          ? "The consent result is unconfirmed. Check the original request without sending it again. A conflict or missing receipt does not permit a replacement."
          : cause instanceof Error
            ? cause.message
            : "Consent could not be verified. Preserve any original request.",
      );
    }
  }
  async function verifiedGrant(
    raw: unknown,
    op: Operation,
    marker?: PollResumeMarker,
  ) {
    const reference = pollResumeGrantReference(raw);
    const source = await readPollingBindingReceipt(
      await request<unknown>(
        connection,
        bindingPath + "/requests/" + reference.requestId,
        { signal: op.signal },
      ),
      {
        project: scope.project,
        repository: scope.repository,
        requestId: reference.requestId,
        receiptDigest: reference.digest,
      },
    );
    if (!op.valid()) throw new Error("The consent context changed.");
    return readPollResumeReceipt(raw, source, scope, marker);
  }
  async function currentGrant(op: Operation) {
    let raw: unknown;
    try {
      raw = await request<unknown>(
        connection,
        path + "/current/" + binding.request.request_id,
        { signal: op.signal },
      );
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) return null;
      throw cause;
    }
    const value = await verifiedGrant(raw, op);
    if (value.binding_key !== binding.binding_key)
      throw new Error(
        "The current consent belongs to a different mapping. No new request was sent.",
      );
    return value;
  }
  async function currentBinding(op: Operation) {
    let cursor: string | null = null;
    const keys = new Set<string>();
    let matched = false;
    for (let page = 0; page < 2; page++) {
      const result = await readPollResumeHeads(
        await request<unknown>(
          connection,
          bindingPath + (cursor ? "?after=" + cursor : ""),
          { signal: op.signal },
        ),
        scope,
        cursor,
      );
      if (!op.valid()) throw new Error("The consent context changed.");
      for (const head of result.items) {
        if (keys.has(head.binding_key))
          throw new Error(
            "Current GitHub account mappings changed during the read.",
          );
        keys.add(head.binding_key);
        if (head.binding_key === binding.binding_key)
          matched =
            head.digest === binding.digest &&
            head.request.request_id === binding.request.request_id &&
            head.request.active &&
            head.request.allowed_actions.includes("respond");
      }
      cursor = result.next_after;
      if (cursor === null) {
        if (!matched)
          throw new Error(
            "Read the current active GitHub account mapping before reviewing consent.",
          );
        return;
      }
    }
    throw new Error(
      "Current GitHub mappings exceed the bounded read. No consent was sent.",
    );
  }
  async function refresh() {
    if (!canRead) return;
    const op = begin();
    if (!op) return;
    try {
      setMarkers(readPollResumeMarkers(localStorage, scope));
      const value = await currentGrant(op);
      if (!op.valid()) return;
      setCurrent(value);
      setLoaded(true);
      setNotice(
        value
          ? "Current recorded consent read. Saved consent does not prove current execution permission."
          : "No current consent was returned. Any original request still needs its own exact check.",
      );
    } catch (cause) {
      fail(cause, op);
    } finally {
      finish(op);
    }
  }
  async function start() {
    if (
      !canManage ||
      !binding.request.active ||
      !binding.request.allowed_actions.includes("respond")
    )
      return;
    const op = begin();
    if (!op) return;
    try {
      originalRequests();
      await currentBinding(op);
      const value = await currentGrant(op);
      originalRequests();
      if (!op.valid()) return;
      setCurrent(value);
      setLoaded(true);
      setComponents(
        binding.request.component_ids.filter((id) =>
          project.definition.components.some(
            (item) => item.id === id && item.repository_id === scope.repository,
          ),
        ),
      );
      setActive(true);
      setAck(false);
      setReview(null);
      setStep(0);
      setOpen(true);
    } catch (cause) {
      fail(cause, op);
    } finally {
      finish(op);
    }
  }
  function close() {
    if (busy) return;
    setOpen(false);
    setReview(null);
    requestAnimationFrame(() => trigger.current?.focus());
  }
  function next() {
    if (step === 0) {
      setStep(1);
      return;
    }
    if (!components.length) return;
    try {
      originalRequests();
      setReview(
        pollResumeInput({
          version: 3,
          transport: "github_rest_poll_v1",
          request_id: crypto.randomUUID().replaceAll("-", ""),
          expected_previous_digest: current?.digest ?? null,
          expected_project_binding_digest: binding.project_binding_digest,
          expected_binding_request_id: binding.request.request_id,
          expected_binding_digest: binding.digest,
          component_ids: [...components].sort(),
          allowed_actions: ["resume"],
          active,
          acknowledge_same_run_resume: true,
        }),
      );
      setAck(false);
      setStep(2);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Original request tracking is unavailable.",
      );
    }
  }
  async function exact(marker: PollResumeMarker, op: Operation) {
    const value = await verifiedGrant(
      await request<unknown>(
        connection,
        path + "/requests/" + marker.requestId,
        { signal: op.signal },
      ),
      op,
      marker,
    );
    if (!op.valid()) return;
    // Confirm the complete still-persisted identity, including its API-observed V2 cut.
    const stored = readPollResumeMarkers(localStorage, scope).find(
      (item) => item.requestId === marker.requestId,
    );
    if (
      !stored ||
      pollResumeMarkerBinding(stored) !== pollResumeMarkerBinding(marker)
    )
      throw new Error(
        "The retained consent identity changed. Keep it for review.",
      );
    setViewed(value);
    setConfirmed((old) => new Set([...old, pollResumeMarkerBinding(marker)]));
    setNotice(
      "Exact consent receipt confirmed. Saving consent does not resume a run.",
    );
  }
  async function checkOriginal(marker: PollResumeMarker) {
    const op = begin();
    if (!op) return;
    try {
      await exact(marker, op);
    } catch (cause) {
      fail(cause, op, true);
    } finally {
      finish(op);
    }
  }
  async function save() {
    if (!review || !ack || !canManage) return;
    if (!navigator.locks) {
      setTracking(
        "This browser cannot coordinate consent requests across tabs. Use a browser with Web Locks on HTTPS or localhost, then check original requests before continuing. No consent was sent.",
      );
      setOpen(false);
      setReview(null);
      return;
    }
    const op = begin();
    if (!op) return;
    let retained: PollResumeMarker | null = null;
    let retaining = false;
    try {
      originalRequests();
      await currentBinding(op);
      const latest = await currentGrant(op);
      if ((latest?.digest ?? null) !== review.expected_previous_digest)
        throw new Error(
          "The current consent changed. Close this review and read it again before making a new explicit choice.",
        );
      const inputDigest = await attemptDigest(review);
      if (!op.valid()) return;
      retaining = true;
      retained = await retainPollResumeMarker(
        localStorage,
        {
          ...scope,
          version: 3,
          requestId: review.request_id,
          inputDigest,
          bindingRequestId: review.expected_binding_request_id,
          bindingDigest: review.expected_binding_digest,
          projectDigest: review.expected_project_binding_digest,
          expectedHead: review.expected_previous_digest,
        },
        confirmed,
        navigator.locks,
        op.valid,
      );
      if (!op.valid()) return;
      setMarkers(readPollResumeMarkers(localStorage, scope));
      setOpen(false);
      setReview(null);
      await request<unknown>(connection, path, {
        method: "POST",
        body: JSON.stringify(review),
        signal: op.signal,
      });
      await exact(retained, op);
      if (op.valid()) {
        setCurrent(null);
        setLoaded(false);
      }
    } catch (cause) {
      fail(cause, op, retained !== null);
      if (retaining && !retained && op.valid())
        setTracking(
          "Request coordination or storage could not be verified. New consent is held; preserve original requests for review.",
        );
      if (!retained && op.valid()) {
        try {
          setMarkers(readPollResumeMarkers(localStorage, scope));
        } catch {
          setTracking(
            "Original requests cannot be read. New consent is held; preserve storage for review.",
          );
        }
      }
    } finally {
      if (op.valid()) requestAnimationFrame(() => recovery.current?.focus());
      finish(op);
    }
  }
  const unresolved = markers.some(
    (item) => !confirmed.has(pollResumeMarkerBinding(item)),
  );
  const eligible =
    canManage &&
    binding.request.active &&
    binding.request.allowed_actions.includes("respond");
  const names = (ids: string[]) =>
    ids
      .map(
        (id) =>
          project.definition.components.find((item) => item.id === id)?.name ??
          "Historical component",
      )
      .join(", ");
  if (!canRead)
    return (
      <p>Current project access does not permit reading resume consent.</p>
    );
  return (
    <section
      className="scm-resume-consent"
      aria-label="GitHub continuation consent"
    >
      <h3>
        <ShieldCheck size={20} /> GitHub continuation consent
      </h3>
      <p>
        A separate administrator decision lets this mapped person request
        continuation after answering a paused question. Saving consent does not
        start a run. The original execution actor and configuration stay in
        place.
      </p>
      <div className="scm-delegation-actions">
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void refresh()}
        >
          <History size={16} />
          Read current GitHub consent
        </button>
        {eligible && (
          <button
            className="primary"
            ref={trigger}
            disabled={busy || Boolean(tracking) || unresolved}
            onClick={() => void start()}
          >
            Review GitHub continuation
          </button>
        )}
      </div>
      {!eligible && (
        <p>
          Consent changes require a native factory administrator and a current
          active delegation that allows answers.
        </p>
      )}
      {tracking && <p role="alert">{tracking}</p>}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {loaded && current && (
        <article>
          <h4>Current recorded consent · revision {current.revision}</h4>
          <p>
            {current.request.active ? "Recorded as active" : "Revoked"}
            {current.request.expected_binding_digest !== binding.digest
              ? "; tied to another delegation revision. Review that mapping before changing consent."
              : ". Current role, connection and answered-run checks still apply."}
          </p>
          <p>{names(current.request.component_ids)}</p>
        </article>
      )}
      {loaded && !current && (
        <p>
          No polling continuation consent was returned for this mapping. This
          does not resolve an unknown request.
        </p>
      )}
      {(markers.length > 0 || tracking) && (
        <section
          className="scm-delegation-recovery"
          aria-label="Original resume consent requests"
        >
          <h4 ref={recovery} tabIndex={-1}>
            Check original consent requests
          </h4>
          <p>
            A missing receipt, conflict or lost response leaves its request
            retained. Check it explicitly; no request is sent again.
          </p>
          {markers.map((marker, index) => (
            <div key={marker.requestId}>
              <p>
                {confirmed.has(pollResumeMarkerBinding(marker))
                  ? "Exact receipt confirmed"
                  : "Original result unconfirmed"}
              </p>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void checkOriginal(marker)}
              >
                Check original consent {index + 1}
              </button>
            </div>
          ))}
        </section>
      )}
      {viewed && (
        <section
          className="scm-delegation-receipt"
          aria-label="Exact resume consent receipt"
        >
          <h4>Recorded consent revision {viewed.revision}</h4>
          <p>
            {viewed.request.active
              ? "This revision allowed same-run resume requests."
              : "This revision revoked resume consent."}{" "}
            It does not prove a run resumed or completed.
          </p>
          <p>Prepared {new Date(viewed.prepared_at).toLocaleString()}</p>
          <details>
            <summary>Exact consent references</summary>
            <dl>
              {Object.entries({
                Request: viewed.request.request_id,
                Event: viewed.event_id,
                "Polling account binding":
                  viewed.request.expected_binding_digest,
                "Input digest": viewed.request_digest,
                "Receipt digest": viewed.digest,
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
        </section>
      )}
      {open && (
        <dialog
          ref={dialog}
          className="access-member-dialog scm-delegation-dialog scm-resume-dialog"
          aria-label="Review GitHub continuation consent"
          onCancel={(event) => {
            if (busy) event.preventDefault();
            else close();
          }}
        >
          <div className="access-dialog-heading">
            <h2>Review GitHub continuation</h2>
            <button
              className="quiet"
              aria-label="Close resume consent review"
              disabled={busy}
              onClick={close}
            >
              <X size={20} />
            </button>
          </div>
          <Wizard
            steps={["Mapping", "Scope", "Review"]}
            active={step}
            onStep={(value) => {
              setStep(value);
              setAck(false);
            }}
            locked={busy}
            title={
              [
                "Review the GitHub account",
                "Choose components",
                "Confirm GitHub continuation",
              ][step]
            }
            description={
              [
                "This GitHub account is already linked to a registered person.",
                "Choose where this account can request continuation.",
                "Saving this permission does not start work.",
              ][step]
            }
          >
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (step < 2) next();
                else void save();
              }}
            >
              {step === 0 && (
                <>
                  <h3>{personName}</h3>
                  <p>
                    GitHub account{" "}
                    {binding.verified_reference.external_actor_id} is linked to{" "}
                    {personName}.
                  </p>
                  <p>
                    They can ask forge to continue the same paused work after
                    the agent's question is answered. Their current project
                    permissions are checked before work starts.
                  </p>
                </>
              )}
              {step === 1 && (
                <>
                  <fieldset>
                    <legend>Allowed components</legend>
                    {binding.request.component_ids
                      .filter((id) =>
                        project.definition.components.some(
                          (item) =>
                            item.id === id &&
                            item.repository_id === scope.repository,
                        ),
                      )
                      .map((id) => (
                        <label className="scm-delegation-check" key={id}>
                          <input
                            type="checkbox"
                            checked={components.includes(id)}
                            onChange={(event) =>
                              setComponents((old) =>
                                event.target.checked
                                  ? [...old, id]
                                  : old.filter((value) => value !== id),
                              )
                            }
                          />
                          <span>{names([id])}</span>
                        </label>
                      ))}
                  </fieldset>
                  <label className="scm-delegation-check">
                    <input
                      type="checkbox"
                      checked={active}
                      onChange={(event) => setActive(event.target.checked)}
                    />
                    <span>
                      Allow this person to continue answered work. Clear this
                      choice to turn off this permission.
                    </span>
                  </label>
                </>
              )}
              {step === 2 && (
                <>
                  <h3>
                    {active
                      ? "Allow answered work to continue"
                      : "Turn off GitHub continuation"}
                  </h3>
                  <p>
                    {personName} · {names(components)}
                  </p>
                  <p>
                    This permission applies to the original paused work. It does
                    not allow publishing, merging or changing the selected
                    model.
                  </p>
                  <p>
                    After an answer is recorded, the person can request
                    continuation in GitHub. forge checks the answer and current
                    project access again.
                  </p>
                  <label className="scm-delegation-check">
                    <input
                      type="checkbox"
                      checked={ack}
                      onChange={(event) => setAck(event.target.checked)}
                    />
                    <span>
                      I approve this GitHub account and these components for
                      continuing answered work.
                    </span>
                  </label>
                </>
              )}
              <div className="wizard-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    step ? (setStep(step - 1), setAck(false)) : close()
                  }
                >
                  {step ? "Back" : "Cancel"}
                </button>
                <button
                  className="primary"
                  disabled={
                    busy ||
                    (step === 1 && !components.length) ||
                    (step === 2 && !ack)
                  }
                >
                  {step === 2 ? "Save permission" : "Continue"}
                </button>
              </div>
            </form>
          </Wizard>
        </dialog>
      )}
    </section>
  );
}

type PollPanelProps = {
  connection: Connection;
  session: IdentitySession;
  project: ProjectSnapshot;
  repository: string;
  people: {
    id: string;
    display_name: string | null;
    email?: string | null;
    contact_email?: string | null;
  }[];
};
export function ScmPollResumePanel(props: PollPanelProps) {
  const key = JSON.stringify([
    props.connection.base,
    props.connection.token,
    props.session.user.id,
    props.session.global_admin,
    props.session.memberships,
    props.project.id,
    props.project.revision,
    props.project.digest,
    props.repository,
  ]);
  return <PollingAccounts key={key} {...props} />;
}
function PollingAccounts({
  connection,
  session,
  project,
  repository,
  people,
}: PollPanelProps) {
  const [bindings, setBindings] = useState<PollingBindingReceipt[]>([]),
    [selected, setSelected] = useState(""),
    [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(false),
    [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      pending.current?.abort();
      pending.current = null;
    },
    [],
  );
  const canRead =
    session.global_admin ||
    Boolean(
      session.memberships
        .find((x) => x.project_id === project.id)
        ?.permissions.includes("read"),
    );
  const scope: DelegationContext = {
    base: new URL(
      normalizeBase(connection.base),
      window.location.href,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    project: project.id,
    repository,
  };
  async function read() {
    if (!canRead || pending.current) return;
    const control = new AbortController();
    pending.current = control;
    setBusy(true);
    setError("");
    setBindings([]);
    setSelected("");
    setLoaded(false);
    try {
      let cursor: string | null = null;
      const rows: PollingBindingReceipt[] = [];
      const seen = new Set<string>();
      for (let page = 0; page < 2; page++) {
        const result = await readPollResumeHeads(
          await request<unknown>(
            connection,
            `/projects/${project.id}/repositories/${repository}/scm-polling/bindings` +
              (cursor ? "?after=" + cursor : ""),
            { signal: control.signal },
          ),
          scope,
          cursor,
        );
        if (control.signal.aborted) return;
        for (const binding of result.items) {
          if (seen.has(binding.binding_key))
            throw new Error(
              "Current account mappings changed during this read.",
            );
          seen.add(binding.binding_key);
          rows.push(binding);
        }
        cursor = result.next_after;
        if (cursor === null) break;
      }
      if (cursor !== null)
        throw new Error(
          "There are more account mappings than this bounded view can verify.",
        );
      if (pending.current === control) {
        setBindings(rows);
        setLoaded(true);
      }
    } catch (cause) {
      if (pending.current === control && !control.signal.aborted)
        setError(
          cause instanceof ApiError && [401, 403].includes(cause.status)
            ? "Current access was refused. Account details have been cleared."
            : "GitHub polling accounts could not be verified. Check the connection and current project access before trying again.",
        );
    } finally {
      if (pending.current === control) {
        pending.current = null;
        setBusy(false);
      }
    }
  }
  if (!canRead) return null;
  const binding = bindings.find((x) => x.request.request_id === selected);
  const name = (item: PollingBindingReceipt) => {
    const person = people.find((x) => x.id === item.request.native_user_id);
    return (
      person?.display_name ||
      person?.contact_email ||
      person?.email ||
      "Registered person"
    );
  };
  return (
    <section
      className="scm-resume-consent"
      aria-label="Local GitHub continuation"
    >
      <h3>
        <GitBranch size={20} />
        Local GitHub continuation
      </h3>
      <p>
        Let an existing GitHub account request continuation after an answered
        agent question. Issue polling must already be configured. This uses a
        separate consent from signed webhooks and does not start work.
      </p>
      <button
        type="button"
        className="secondary"
        disabled={busy}
        onClick={() => void read()}
      >
        <History size={16} />
        {busy ? "Reading GitHub accounts…" : "Read GitHub accounts"}
      </button>
      {error && <p role="alert">{error}</p>}
      {loaded && !bindings.length && (
        <p>
          No account mappings were returned for this repository. Configure issue
          polling and its account mapping before reviewing continuation consent.
        </p>
      )}
      {loaded && bindings.length > 0 && (
        <label className="field">
          GitHub account
          <select
            aria-label="GitHub continuation account"
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
          >
            <option value="">Choose an account to review</option>
            {bindings.map((item) => (
              <option key={item.binding_key} value={item.request.request_id}>
                {name(item)} · GitHub{" "}
                {item.verified_reference.external_actor_id}
                {!item.request.active ? " · Revoked" : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      {binding && (
        <>
          <ScmPollFunctionalConsent
            connection={connection}
            session={session}
            project={project}
            binding={binding}
            personName={name(binding)}
          />
          <ScmPollResumeConsent
            key={binding.digest}
            connection={connection}
            session={session}
            project={project}
            binding={binding}
            personName={name(binding)}
          />
        </>
      )}
    </section>
  );
}
