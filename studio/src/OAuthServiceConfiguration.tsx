/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  RefreshCw,
  ShieldCheck,
  Server,
} from "lucide-react";
import { request, type Connection } from "./api";
import { Wizard } from "./Wizard";
import {
  readOAuthProviders,
  type OAuthProviderChoice,
} from "./ConnectionOAuthSettings";
import {
  localOrigin,
  localProvider,
  policyPending,
  policyPendingKey,
  readPolicyPending,
  readPolicyDirectory,
  readLocalPolicy,
  type OAuthPolicyDirectory,
  type OAuthPolicyInput,
  type OAuthLocalPolicy,
} from "./OAuthServicePolicy";

export function OAuthServiceConfiguration({
  connection,
  actor,
  initial,
  onClose,
  onComplete,
}: {
  connection: Connection;
  actor: string;
  initial: OAuthPolicyDirectory;
  onClose: () => void;
  onComplete: () => void;
}) {
  const storageKey = policyPendingKey(connection.base, actor);
  const [retained] = useState(() => {
    try {
      return {
        pending: readPolicyPending(
          sessionStorage.getItem(storageKey),
          connection.base,
          actor,
        ),
        readable: true,
      };
    } catch {
      return { pending: null, readable: false };
    }
  });
  const storageError = retained.readable
    ? ""
    : "This browser cannot read session storage. Allow access and reopen this setup before saving; an earlier request may still need confirmation.";
  const [pending, setPending] = useState(retained.pending);
  const previous = initial.policies[0];
  const initialOrigin = pending?.body.local_origin || previous?.local_origin;
  const [host, setHost] = useState(
    initialOrigin?.includes("[::1]")
      ? "[::1]"
      : initialOrigin
        ? "127.0.0.1"
        : new URL(connection.base, window.location.href).hostname === "[::1]"
          ? "127.0.0.1"
          : "[::1]",
  );
  const [port, setPort] = useState(initialOrigin?.split(":").at(-1) || "");
  const [enabled, setEnabled] = useState(
    pending?.body.enabled ?? previous?.enabled ?? true,
  );
  const [directory, setDirectory] = useState(initial);
  const [roster, setRoster] = useState<OAuthProviderChoice[]>([]);
  const [receipt, setReceipt] = useState<OAuthLocalPolicy | null>(null);
  const [step, setStep] = useState(pending ? 1 : 0);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(storageError);
  const active = useRef<AbortController | null>(null),
    alert = useRef<HTMLDivElement>(null);
  const current = directory.policies[0];
  const origin = `http://${host}:${port}`;
  const readyForRegistration =
    directory.local_development_available &&
    current?.enabled === true &&
    roster.some(
      (p) =>
        p.provider === localProvider && p.status === "registration_available",
    );
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  function review(event: FormEvent) {
    event.preventDefault();
    if (busy || pending) return;
    try {
      localOrigin(origin);
      setError(storageError);
      setStep(1);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Review the service address.",
      );
    }
  }
  async function readCurrent(controller: AbortController) {
    const [policy, providers] = await Promise.all([
      request<unknown>(connection, "/oauth-provider-policies", {
        signal: controller.signal,
      }),
      request<unknown>(connection, "/connection-oauth/providers", {
        signal: controller.signal,
      }),
    ]);
    const state = readPolicyDirectory(policy),
      catalog = readOAuthProviders(providers);
    if (controller.signal.aborted) return;
    setDirectory(state);
    setRoster(catalog);
  }
  async function refresh() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError(storageError);
    try {
      await readCurrent(controller);
    } catch {
      if (!controller.signal.aborted)
        setError(
          "Current service configuration could not be read. No save request was repeated.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || !directory.local_development_available || !retained.readable)
      return;
    let body: OAuthPolicyInput;
    try {
      body = pending?.body || {
        enabled,
        local_origin: localOrigin(origin),
        expected_revision: current?.revision || 0,
        request_id: crypto.randomUUID().replaceAll("-", ""),
      };
      const marker = policyPending(body, connection.base, actor);
      sessionStorage.setItem(storageKey, JSON.stringify(marker));
      setPending(marker);
    } catch {
      setError(
        "This browser cannot retain a valid configuration request. Check the address and session storage before saving.",
      );
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const result = readLocalPolicy(
        await request<unknown>(
          connection,
          `/oauth-provider-policies/${localProvider}`,
          {
            method: "PUT",
            body: JSON.stringify(body),
            signal: controller.signal,
          },
        ),
      );
      if (
        result.revision !== body.expected_revision + 1 ||
        result.local_origin !== body.local_origin ||
        result.enabled !== body.enabled
      )
        throw new Error("Unexpected policy receipt");
      // A replay returns its historical packet. Only the subsequent reads describe current availability.
      await readCurrent(controller);
      if (controller.signal.aborted) return;
      setReceipt(result);
      sessionStorage.removeItem(storageKey);
      setPending(null);
      setStep(2);
    } catch {
      if (!controller.signal.aborted)
        setError(
          "The save response or current configuration was not confirmed. Keep the original request; confirm it explicitly before editing these values.",
        );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  const reviewRows = (
    <dl className="wizard-summary">
      <div>
        <dt>Service address</dt>
        <dd>{pending?.body.local_origin || origin}</dd>
      </div>
      <div>
        <dt>New registrations</dt>
        <dd>
          {(pending?.body.enabled ?? enabled)
            ? "Allowed by this configuration"
            : "Disabled by this configuration"}
        </dd>
      </div>
      <div>
        <dt>Permitted resource</dt>
        <dd>{(pending?.body.local_origin || origin) + "/mcp/readonly"}</dd>
      </div>
      <div>
        <dt>Requested permission</dt>
        <dd>repository:read</dd>
      </div>
    </dl>
  );
  return (
    <div className="oauth-service-configuration">
      {error && (
        <div className="alert" role="alert" ref={alert} tabIndex={-1}>
          <p>{error}</p>
        </div>
      )}
      <div className="oauth-toolbar">
        <button className="text-button" disabled={busy} onClick={onClose}>
          <ArrowLeft size={16} />
          Account overview
        </button>
        <button
          className="text-button"
          disabled={busy}
          onClick={() => void refresh()}
        >
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>
      <Wizard
        className="external-tools-wizard oauth-wizard oauth-service-wizard"
        minimumFieldHeight={64}
        progressMode="visited"
        steps={["Service address", "Review", "Saved"]}
        active={step}
        onStep={(value) => {
          if (!pending && value < 2) setStep(value);
        }}
        locked={busy}
        title={
          [
            "Configure a local service",
            "Review the local service",
            "Current service configuration",
          ][step]
        }
        description={
          [
            "Use the address of your owned local authorization and repository service.",
            "Saving records this fixed local contract. It does not contact the service or authorize an account.",
            "Current availability comes from the factory’s latest configuration and service list.",
          ][step]
        }
        guide={
          <>
            <Server size={28} />
            <h3>Local testing only</h3>
            <p>
              The server must allow local development, loopback identity and a
              literal loopback factory address. This form cannot enable those
              runtime settings or configure vendor endpoints.
            </p>
            <p>
              Use a different literal host from the factory browser to keep
              their cookies separate. Ports alone do not separate browser
              cookies.
            </p>
            <ShieldCheck size={22} />
            <p>
              Browser consent and repository tool review remain separate steps.
            </p>
          </>
        }
      >
        {step === 0 ? (
          <form onSubmit={review}>
            <fieldset
              className="oauth-client-fields"
              disabled={busy || Boolean(pending)}
            >
              <legend className="sr-only">Local service address</legend>
              <div className="field">
                <label htmlFor="oauth-service-host">Local host</label>
                <select
                  id="oauth-service-host"
                  value={host}
                  onChange={(e) => setHost(e.target.value)}
                >
                  <option value="127.0.0.1">IPv4 loopback (127.0.0.1)</option>
                  <option value="[::1]">IPv6 loopback (::1)</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="oauth-service-port">Service port</label>
                <input
                  id="oauth-service-port"
                  type="number"
                  min={1024}
                  max={65535}
                  step={1}
                  required
                  value={port}
                  onChange={(e) => setPort(e.target.value)}
                  placeholder="49152"
                />
              </div>
              <div className="field oauth-service-enabled">
                <label htmlFor="oauth-service-enabled">
                  Browser registrations
                </label>
                <select
                  id="oauth-service-enabled"
                  value={enabled ? "enabled" : "disabled"}
                  onChange={(e) => setEnabled(e.target.value === "enabled")}
                >
                  <option value="enabled">Allow for this local service</option>
                  <option value="disabled">Keep disabled</option>
                </select>
              </div>
            </fieldset>
            <p className="external-hint">
              HTTP loopback only. The factory supplies the authorization, token
              and read-only resource paths; no credentials are stored in this
              configuration.
            </p>
            <div className="wizard-actions">
              <button type="button" className="secondary" onClick={onClose}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={busy || !directory.local_development_available}
              >
                Review configuration
                <ArrowRight size={16} />
              </button>
            </div>
          </form>
        ) : step === 1 ? (
          <form onSubmit={save}>
            {pending && (
              <p role="status" className="external-hint">
                An original save is awaiting confirmation. Its address, choice
                and request identifier are retained. Refreshing does not repeat
                it.
              </p>
            )}
            {reviewRows}
            <details className="oauth-identity">
              <summary>Fixed protocol and revision</summary>
              <dl className="wizard-summary">
                <div>
                  <dt>Authorization</dt>
                  <dd>
                    {(pending?.body.local_origin || origin) +
                      "/oauth2/authorize"}
                  </dd>
                </div>
                <div>
                  <dt>Token exchange</dt>
                  <dd>
                    {(pending?.body.local_origin || origin) + "/oauth2/token"}
                  </dd>
                </div>
                <div>
                  <dt>Expected revision</dt>
                  <dd>
                    {pending?.body.expected_revision ?? current?.revision ?? 0}
                  </dd>
                </div>
              </dl>
            </details>
            <div className="wizard-actions">
              <button
                type="button"
                className="secondary"
                disabled={busy || Boolean(pending)}
                onClick={() => setStep(0)}
              >
                Back
              </button>
              <button
                className="primary"
                disabled={
                  busy ||
                  !directory.local_development_available ||
                  !retained.readable
                }
              >
                {busy
                  ? "Saving…"
                  : pending
                    ? "Confirm original save"
                    : "Save service configuration"}
                <ArrowRight size={16} />
              </button>
            </div>
          </form>
        ) : (
          <form onSubmit={(e) => e.preventDefault()}>
            <dl className="wizard-summary">
              <div>
                <dt>Current configuration</dt>
                <dd>
                  {current?.enabled ? "Enabled for registration" : "Disabled"}
                </dd>
              </div>
              <div>
                <dt>Current revision</dt>
                <dd>{current?.revision || "Unavailable"}</dd>
              </div>
              <div>
                <dt>Current address</dt>
                <dd>{current?.local_origin || "Unavailable"}</dd>
              </div>
              <div>
                <dt>Runtime availability</dt>
                <dd>
                  {directory.local_development_available
                    ? "Local configuration allowed"
                    : "Local configuration unavailable"}
                </dd>
              </div>
            </dl>
            {receipt && current?.revision !== receipt.revision && (
              <p className="external-hint">
                The original save receipt is revision {receipt.revision}.
                Current configuration is revision{" "}
                {current?.revision || "unavailable"}; it may have changed since
                that request.
              </p>
            )}
            <p className="external-hint">
              An account still needs browser authorization. This configuration
              does not verify that the local service is running or enable agent
              tools.
            </p>
            <div className="wizard-actions">
              <button className="secondary" onClick={onClose}>
                Overview
              </button>
              <button
                className="primary"
                disabled={busy || !readyForRegistration}
                onClick={onComplete}
              >
                Set up an account
                <ArrowRight size={16} />
              </button>
            </div>
          </form>
        )}
      </Wizard>
    </div>
  );
}
