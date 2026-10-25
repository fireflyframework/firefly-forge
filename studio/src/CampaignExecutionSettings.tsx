/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Gauge, RefreshCw, Save } from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import {
  executionPolicy,
  executionCampaign,
  executionCampaigns,
  positiveInteger,
  renewalBody,
  type ExecutionCampaign,
  type ExecutionPolicyView,
} from "./CampaignExecutionData";
import "./CampaignExecutionSettings.css";

type Confirmation = {
  campaign: ExecutionCampaign;
  policy: ExecutionPolicyView;
  body: ReturnType<typeof renewalBody>;
};
type ScopeProps = {
  connection: Connection;
  projectId: string;
  session: IdentitySession;
};
function semanticScope({ connection, projectId, session }: ScopeProps): string {
  return JSON.stringify([
    connection.base,
    connection.token,
    connection.authenticated === true,
    projectId,
    session.user.id,
    session.user.authentication_method,
    session.global_admin,
    session.memberships
      .map((item) => [
        item.project_id,
        item.role,
        item.revision,
        [...item.permissions].sort(),
      ])
      .sort((left, right) => String(left[0]).localeCompare(String(right[0]))),
  ]);
}
export function CampaignExecutionSettings(props: ScopeProps) {
  const scopeKey = semanticScope(props);
  return (
    <CampaignExecutionScope key={scopeKey} {...props} scopeKey={scopeKey} />
  );
}
function CampaignExecutionScope({
  connection,
  projectId,
  session,
  scopeKey,
}: ScopeProps & { scopeKey: string }) {
  const [opened, setOpened] = useState(false),
    [current, setCurrent] = useState<ExecutionPolicyView | null>(null);
  const [reservationsOpen, setReservationsOpen] = useState(false),
    [interacted, setInteracted] = useState(false);
  const [window, setWindow] = useState(""),
    [parallel, setParallel] = useState(""),
    [enabled, setEnabled] = useState(false);
  const [items, setItems] = useState<ExecutionCampaign[] | null>(null),
    [selected, setSelected] = useState<ExecutionCampaign | null>(null);
  const [additional, setAdditional] = useState(""),
    [renewParallel, setRenewParallel] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const live = useRef(true),
    locked = useRef(false),
    reads = useRef<AbortController | null>(null);
  const scope = useRef({ key: scopeKey, connection, projectId, session });
  scope.current = { key: scopeKey, connection, projectId, session };
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      reads.current?.abort();
    };
  }, []);
  useEffect(() => {
    const expected = scope.current;
    const { connection, projectId } = expected;
    const controller = new AbortController();
    reads.current = controller;
    locked.current = true;
    setBusy(true);
    request<unknown>(
      connection,
      `/projects/${projectId}/campaign-execution-policy`,
      { signal: controller.signal },
    )
      .then((value) => {
        const saved = executionPolicy(value, projectId);
        if (
          !controller.signal.aborted &&
          live.current &&
          scope.current.key === expected.key
        ) {
          setCurrent(saved);
          setWindow(String(saved.policy.dispatch_window));
          setParallel(String(saved.policy.max_parallel_runs));
          setEnabled(saved.policy.enabled);
        }
      })
      .catch(() => {
        if (
          !controller.signal.aborted &&
          live.current &&
          scope.current.key === expected.key
        ) {
          setCurrent(null);
          setError(
            "Current execution settings could not be read. Refresh to check your access and saved policy.",
          );
        }
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          live.current &&
          scope.current.key === expected.key
        ) {
          setBusy(false);
          locked.current = false;
        }
      });
    return () => controller.abort();
  }, [scopeKey]);
  const same = (expected: typeof scope.current) =>
    live.current && scope.current.key === expected.key;
  const policyPath = `/projects/${projectId}/campaign-execution-policy`,
    campaignsPath = `/projects/${projectId}/campaigns`;
  const permitted = (permission: "execute" | "release") =>
    session.global_admin ||
    session.memberships.some(
      (item) =>
        item.project_id === projectId && item.permissions.includes(permission),
    );
  async function readPolicy(signal?: AbortSignal) {
    const value = executionPolicy(
      await request<unknown>(connection, policyPath, { signal }),
      projectId,
    );
    return value;
  }
  function applyPolicy(value: ExecutionPolicyView) {
    setCurrent(value);
    setWindow(String(value.policy.dispatch_window));
    setParallel(String(value.policy.max_parallel_runs));
    setEnabled(value.policy.enabled);
  }
  async function refresh() {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setOpened(true);
    setError("");
    setNotice("");
    setConfirmation(null);
    reads.current?.abort();
    const controller = new AbortController();
    reads.current = controller;
    const expected = scope.current;
    try {
      const value = await readPolicy(controller.signal);
      if (same(expected) && !controller.signal.aborted) {
        applyPolicy(value);
        setSelected(null);
        setItems(null);
      }
    } catch {
      if (same(expected) && !controller.signal.aborted) {
        setCurrent(null);
        setSelected(null);
        setItems(null);
        setError(
          "Current execution settings could not be read. Refresh to check your access and saved policy.",
        );
      }
    } finally {
      if (same(expected)) setBusy(false);
      locked.current = false;
    }
  }
  let valid = false;
  try {
    positiveInteger(window);
    positiveInteger(parallel);
    valid = true;
  } catch {
    /* Keep incomplete integer drafts editable. */
  }
  const changed =
    !!current &&
    (String(current.policy.dispatch_window) !== window ||
      String(current.policy.max_parallel_runs) !== parallel ||
      current.policy.enabled !== enabled);
  const canRenew =
    !!selected &&
    permitted("execute") &&
    (!selected.publish || permitted("release"));
  let renewalValid = false;
  try {
    if (selected && current) {
      renewalBody(selected, current, "0".repeat(32), additional, renewParallel);
      renewalValid = canRenew;
    }
  } catch {
    /* The review control remains unavailable until the window is valid. */
  }
  function failure(cause: unknown, action: string) {
    const refused =
      cause instanceof ApiError &&
      cause.responseKind === "http" &&
      [401, 403, 409, 422].includes(cause.status);
    return refused
      ? `The server declined this ${action}. Review current access and settings before trying again. No automatic retry was sent.`
      : `The ${action} outcome was not confirmed. Review current records before deciding what to do next. No automatic retry was sent.`;
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (locked.current || !current?.permissions.configure || !valid || !changed)
      return;
    const expected = scope.current,
      previous = current,
      desired = {
        enabled,
        dispatch_window: positiveInteger(window),
        max_parallel_runs: positiveInteger(parallel),
      },
      requestId = crypto.randomUUID().replaceAll("-", "");
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    setConfirmation(null);
    let message = "",
      problem = "";
    try {
      const ack = executionPolicy(
        await request<unknown>(connection, policyPath, {
          method: "PUT",
          body: JSON.stringify({
            request_id: requestId,
            expected_previous_digest: previous.digest,
            policy: desired,
          }),
        }),
        projectId,
      );
      if (
        ack.request_id !== requestId ||
        !ack.digest ||
        JSON.stringify(ack.policy) !== JSON.stringify(desired)
      )
        throw new Error("Unmatched settings acknowledgment");
      message =
        "Execution settings saved. Existing campaigns keep their consumed dispatches and require an explicit renewal for additional work.";
    } catch (cause) {
      problem = failure(cause, "save");
    }
    if (same(expected)) {
      try {
        const value = await readPolicy();
        if (same(expected)) {
          applyPolicy(value);
          setSelected(null);
          setItems(null);
        }
      } catch {
        if (same(expected)) {
          setCurrent(null);
          setSelected(null);
          setItems(null);
          problem +=
            " Current policy could not be reread. Refresh before continuing.";
        }
      }
      if (same(expected)) {
        setError(problem);
        setNotice(problem ? "" : message);
        setBusy(false);
      }
    }
    locked.current = false;
  }
  async function list() {
    if (locked.current || !current) return;
    const expected = scope.current;
    setInteracted(true);
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    setSelected(null);
    setConfirmation(null);
    try {
      const value = executionCampaigns(
        await request<unknown>(connection, campaignsPath),
        projectId,
      );
      if (same(expected)) setItems(value);
    } catch {
      if (same(expected)) {
        setItems(null);
        setError(
          "Campaign reservations could not be verified. Refresh current records before renewing.",
        );
      }
    } finally {
      if (same(expected)) setBusy(false);
      locked.current = false;
    }
  }
  async function choose(id: string) {
    setConfirmation(null);
    setSelected(null);
    setNotice("");
    if (!id || locked.current) return;
    const expected = scope.current;
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      const value = executionCampaign(
        await request<unknown>(connection, `${campaignsPath}/${id}`),
        projectId,
        id,
      );
      const policy = await readPolicy();
      if (same(expected)) {
        applyPolicy(policy);
        setSelected(value);
        setAdditional(String(policy.policy.dispatch_window));
        setRenewParallel(
          String(
            Math.min(
              policy.policy.max_parallel_runs,
              value.budget?.max_parallel_runs ??
                policy.policy.max_parallel_runs,
            ),
          ),
        );
      }
    } catch {
      if (same(expected))
        setError(
          "This campaign or its current policy could not be verified. No renewal is available.",
        );
    } finally {
      if (same(expected)) setBusy(false);
      locked.current = false;
    }
  }
  function review(event: FormEvent) {
    event.preventDefault();
    if (!selected || !current || !renewalValid || busy) return;
    setConfirmation({
      campaign: selected,
      policy: current,
      body: renewalBody(
        selected,
        current,
        crypto.randomUUID().replaceAll("-", ""),
        additional,
        renewParallel,
      ),
    });
  }
  async function renew() {
    if (!confirmation || locked.current || !canRenew) return;
    const expected = scope.current,
      reviewed = confirmation;
    locked.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    let message = "",
      problem = "";
    try {
      const ack = executionCampaign(
        await request<unknown>(
          connection,
          `${campaignsPath}/${reviewed.campaign.id}/renew-budget`,
          { method: "POST", body: JSON.stringify(reviewed.body) },
        ),
        projectId,
        reviewed.campaign.id,
      );
      if (
        !ack.budget ||
        !reviewed.campaign.budget ||
        ack.budget.granted_dispatches !==
          reviewed.campaign.budget.granted_dispatches +
            reviewed.body.additional_dispatches ||
        ack.budget.consumed_dispatches <
          reviewed.campaign.budget.consumed_dispatches
      )
        throw new Error("Unmatched renewal acknowledgment");
      message =
        "Additional execution window confirmed. Previous consumption remains recorded.";
    } catch (cause) {
      problem = failure(cause, "renewal");
    }
    if (same(expected)) {
      try {
        const value = executionCampaign(
            await request<unknown>(
              connection,
              `${campaignsPath}/${reviewed.campaign.id}`,
            ),
            projectId,
            reviewed.campaign.id,
          ),
          policy = await readPolicy();
        if (same(expected)) {
          setSelected(value);
          applyPolicy(policy);
        }
      } catch {
        if (same(expected)) {
          setSelected(null);
          setCurrent(null);
          problem +=
            " Current records could not be reread. Refresh before continuing.";
        }
      }
      if (same(expected)) {
        setConfirmation(null);
        setError(problem);
        setNotice(problem ? "" : message);
        setBusy(false);
      }
    }
    locked.current = false;
  }
  return (
    <section
      className="campaign-execution integration-card"
      aria-label="Campaign execution"
    >
      <div className="section-heading">
        <div>
          <h3>
            <Gauge size={20} aria-hidden="true" />
            Execution settings
          </h3>
          <p className="campaign-execution-summary">
            {current
              ? `${current.policy.dispatch_window} dispatches per window · ${current.policy.max_parallel_runs} parallel runs · ${current.policy.enabled ? "Enabled" : "Dispatches disabled"}`
              : busy
                ? "Loading saved settings…"
                : "Saved settings unavailable"}
          </p>
          {changed && (
            <span className="campaign-execution-unsaved">
              Changes not saved
            </span>
          )}
        </div>
        <div className="campaign-execution-disclosures">
          <button
            className="secondary"
            disabled={busy}
            aria-expanded={opened}
            aria-controls="campaign-policy-editor"
            onClick={() => {
              setInteracted(true);
              setOpened((value) => !value);
            }}
          >
            {current?.permissions.configure === false
              ? "View settings"
              : "Configure"}
          </button>
          <button
            className="secondary"
            disabled={busy || !current}
            aria-expanded={reservationsOpen}
            aria-controls="campaign-reservations"
            onClick={() => {
              setInteracted(true);
              if (reservationsOpen) {
                setReservationsOpen(false);
                setConfirmation(null);
              } else {
                setReservationsOpen(true);
                void list();
              }
            }}
          >
            Review campaign reservations
          </button>
        </div>
      </div>
      {error && interacted && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="campaign-execution-notice" role="status">
          {notice}
        </p>
      )}
      {opened && !current && (
        <div id="campaign-policy-editor">
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void refresh()}
          >
            <RefreshCw size={16} />
            Refresh settings
          </button>
        </div>
      )}
      {opened && current && (
        <div id="campaign-policy-editor" className="campaign-policy-editor">
          <p className="subtle">
            Any number of components can be selected. These finite execution
            windows do not replace runner or command budgets.
          </p>
          <p className="subtle">
            {current.digest
              ? `Saved revision ${current.digest.slice(0, 8)}`
              : "Factory defaults · no saved project revision"}
          </p>
          <form
            className="campaign-policy-form"
            onSubmit={(event) => void save(event)}
          >
            <label className="campaign-execution-toggle">
              <input
                type="checkbox"
                checked={enabled}
                disabled={busy || !current.permissions.configure}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Allow new campaign dispatches
            </label>
            <div className="campaign-execution-fields">
              <div className="field">
                <label htmlFor="campaign-dispatch-window">
                  Dispatches per execution window
                </label>
                <input
                  id="campaign-dispatch-window"
                  type="number"
                  min="1"
                  step="1"
                  value={window}
                  disabled={busy || !current.permissions.configure}
                  onChange={(event) => {
                    setWindow(event.target.value);
                    setConfirmation(null);
                  }}
                  aria-describedby="campaign-window-help"
                />
                <small id="campaign-window-help">
                  Each admitted component attempt spends one dispatch. Renewals
                  add to the existing total.
                </small>
              </div>
              <div className="field">
                <label htmlFor="campaign-parallel">Parallel runs</label>
                <input
                  id="campaign-parallel"
                  type="number"
                  min="1"
                  step="1"
                  value={parallel}
                  disabled={busy || !current.permissions.configure}
                  onChange={(event) => {
                    setParallel(event.target.value);
                    setConfirmation(null);
                  }}
                />
                <small>
                  Paused and interrupted runs keep their reservations until
                  their recorded lifecycle releases them.
                </small>
              </div>
            </div>
            <div className="campaign-policy-actions">
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void refresh()}
              >
                <RefreshCw size={16} />
                Refresh settings
              </button>
              {current.permissions.configure && (
                <button
                  className="primary"
                  disabled={busy || !valid || !changed}
                >
                  <Save size={16} />
                  Save execution settings
                </button>
              )}
            </div>
            {!current.permissions.configure && (
              <p className="subtle">
                A factory administrator with project membership access can
                configure this policy.
              </p>
            )}
          </form>
        </div>
      )}
      {reservationsOpen && current && (
        <div
          id="campaign-reservations"
          className="campaign-execution-reservations"
        >
          <h4>Existing campaign reservations</h4>
          <p>
            Review consumption before adding work. Project settings do not reset
            a campaign or automatically renew it.
          </p>
          {items?.length === 0 && (
            <p>No campaigns are recorded for this project.</p>
          )}
          {!!items?.length && (
            <div className="field">
              <label htmlFor="campaign-budget-selection">
                Campaign to review
              </label>
              <select
                id="campaign-budget-selection"
                disabled={busy}
                value={selected?.id ?? ""}
                onChange={(event) => void choose(event.target.value)}
              >
                <option value="">Choose a campaign</option>
                {items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.status.replaceAll("_", " ")} · {item.id.slice(0, 8)}
                  </option>
                ))}
              </select>
            </div>
          )}
          {selected && (
            <article className="campaign-execution-current">
              <h4>Current execution window</h4>
              {selected.budget ? (
                <>
                  <div className="campaign-execution-totals">
                    <span>{selected.budget.consumed_dispatches} consumed</span>
                    <span>
                      {selected.budget.remaining_dispatches} remaining
                    </span>
                    <span>{selected.held} held reservations</span>
                  </div>
                  <p>
                    Paused and interrupted runs keep their reservations.
                    Additional dispatches do not replay unknown effects or reset
                    past consumption.
                  </p>
                  {canRenew &&
                  selected.control !== "cancelled" &&
                  selected.status !== "needs_reconciliation" &&
                  current.policy.enabled ? (
                    <form onSubmit={review}>
                      <div className="campaign-execution-fields">
                        <div className="field">
                          <label htmlFor="campaign-additional">
                            Additional dispatches
                          </label>
                          <input
                            id="campaign-additional"
                            type="number"
                            min="1"
                            step="1"
                            value={additional}
                            disabled={busy || !!confirmation}
                            onChange={(event) =>
                              setAdditional(event.target.value)
                            }
                          />
                          <small>
                            Up to {current.policy.dispatch_window} in the
                            current project window.
                          </small>
                        </div>
                        <div className="field">
                          <label htmlFor="campaign-renew-parallel">
                            Parallel runs after renewal
                          </label>
                          <input
                            id="campaign-renew-parallel"
                            type="number"
                            min="1"
                            step="1"
                            value={renewParallel}
                            disabled={busy || !!confirmation}
                            onChange={(event) =>
                              setRenewParallel(event.target.value)
                            }
                          />
                          <small>
                            Up to {current.policy.max_parallel_runs} under
                            current project settings.
                          </small>
                        </div>
                      </div>
                      <button
                        className="secondary"
                        disabled={busy || !renewalValid || !!confirmation}
                      >
                        Review renewal
                      </button>
                    </form>
                  ) : (
                    <p className="subtle">
                      Renewal requires current execution access, release access
                      for publishing work, an enabled policy, and resolved
                      campaign holds.
                    </p>
                  )}
                </>
              ) : (
                <p>
                  This campaign predates renewable execution windows. Its
                  original controls remain in effect.
                </p>
              )}
            </article>
          )}
          {confirmation && (
            <div
              className="campaign-execution-confirm"
              role="dialog"
              aria-modal="false"
              aria-label="Confirm campaign renewal"
            >
              <h4>Confirm additional work</h4>
              <p>
                {confirmation.campaign.budget?.consumed_dispatches} dispatches
                already consumed. Add {confirmation.body.additional_dispatches}{" "}
                dispatches and allow {confirmation.body.max_parallel_runs}{" "}
                parallel runs. Existing reservations and consumption remain.
              </p>
              <p>
                Eligible waiting work may start after confirmation. Current
                access, project settings and campaign changes are checked again
                by the server.
              </p>
              <div className="campaign-execution-actions">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => setConfirmation(null)}
                >
                  Cancel renewal
                </button>
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => void renew()}
                >
                  Confirm {confirmation.body.additional_dispatches} additional
                  dispatches
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
