/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  ExternalLink,
  KeyRound,
  RefreshCw,
  ShieldCheck,
  Unplug,
  Globe,
  FolderKey,
  Server,
} from "lucide-react";
import { ApiError, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import { readProjects, type ToolProject } from "./ExternalToolsSettings";
import {
  connectionName,
  continuation,
  registrationPayload,
  readOAuthProviders,
  readOAuthSummary,
  readOAuthDirectory,
  readOAuthReceipt,
  receiptResolves,
  type OAuthPending,
  readResume,
  resumePacket,
  connectionResume,
  resumeKey,
  type OAuthProviderChoice,
  type OAuthSummary,
} from "./ConnectionOAuthSettings";
import { OAuthServiceConfiguration } from "./OAuthServiceConfiguration";
import {
  localProvider,
  readPolicyDirectory,
  type OAuthPolicyDirectory,
} from "./OAuthServicePolicy";
import "./ExternalTools.css";
import "./BrowserConnections.css";

const providerNames: Record<string, string> = {
  github: "GitHub",
  figma: "Figma",
  sentry: "Sentry",
  grafana: "Grafana",
  playwright: "Playwright",
  "local-repository-tools": "Local repository tools",
};
const statusNames: Record<OAuthSummary["status"], string> = {
  authorization_required: "Browser authorization required",
  authorized_unverified: "Authorization retained; tools unavailable",
  reauthorization_required: "New authorization required",
  disconnected: "Locally disconnected",
};
function failure(cause: unknown) {
  if (cause instanceof ApiError) {
    if (cause.status === 401 || cause.status === 403)
      return "Current factory administrator access is required. Sign in again before continuing.";
    if (cause.status === 404)
      return "This connection or lifecycle endpoint is unavailable. Check the current factory and connection name.";
    if (cause.status === 409 || cause.status === 422)
      return "The server rejected this action. Refresh the account state and review its authorization requirements.";
  }
  return "The response could not be confirmed. Refresh the account state before continuing. An outstanding provider request must not be repeated.";
}
export function BrowserConnections({
  connection,
  session,
  onTools,
}: {
  connection: Connection;
  session: IdentitySession | null;
  onTools: () => void;
}) {
  return (
    <>
      <div className="page-heading search-page-heading external-page-heading">
        <div>
          <h1>Connected accounts</h1>
        </div>
        <Globe size={26} />
      </div>
      {session?.global_admin ? (
        <Connections
          key={`${connection.base}:${session.user.id}`}
          connection={connection}
          actor={session.user.id}
          onTools={onTools}
        />
      ) : (
        <section className="access-notice">
          <ShieldCheck size={22} />
          <div>
            <h2>Factory administrator access required</h2>
            <p>
              Browser authorization stores private credentials. A project role
              does not grant permission to manage them.
            </p>
          </div>
        </section>
      )}
    </>
  );
}
function Connections({
  connection,
  actor,
  onTools,
}: {
  connection: Connection;
  actor: string;
  onTools: () => void;
}) {
  const [providers, setProviders] = useState<OAuthProviderChoice[]>([]),
    [projects, setProjects] = useState<ToolProject[]>([]),
    [saved, setSaved] = useState<OAuthSummary | null>(null),
    [directory, setDirectory] = useState<OAuthSummary[]>([]),
    [policyDirectory, setPolicyDirectory] =
      useState<OAuthPolicyDirectory | null>(null),
    [policyEditing, setPolicyEditing] = useState(false);
  const [step, setStep] = useState(0),
    [editing, setEditing] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [uncertain, setUncertain] = useState(false);
  const [provider, setProvider] = useState(""),
    [name, setName] = useState(""),
    [project, setProject] = useState(""),
    [clientId, setClientId] = useState(""),
    [clientType, setClientType] = useState("public"),
    [authentication, setAuthentication] = useState("client_secret_post"),
    [secret, setSecret] = useState(""),
    [disconnecting, setDisconnecting] = useState(false),
    [recovering, setRecovering] = useState(false),
    [recoveryReason, setRecoveryReason] = useState(""),
    [acknowledged, setAcknowledged] = useState(false);
  const active = useRef<AbortController | null>(null),
    view = useRef<HTMLDivElement>(null),
    alert = useRef<HTMLDivElement>(null),
    loaded = useRef(false);
  const current = providers.find((p) => p.provider === provider),
    available = providers.filter((p) => p.status === "registration_available");
  const retain = useCallback(
    (currentName: string, uncertain: boolean, pending?: OAuthPending) => {
      try {
        sessionStorage.setItem(
          resumeKey,
          JSON.stringify(
            resumePacket(
              readResume(
                sessionStorage.getItem(resumeKey),
                connection.base,
                actor,
              ),
              currentName,
              connection.base,
              actor,
              uncertain,
              pending,
            ),
          ),
        );
        return true;
      } catch {
        return false;
      }
    },
    [connection.base, actor],
  );
  const read = useCallback(
    async (controller: AbortController, currentName?: string) => {
      setSaved(null);
      setProviders([]);
      setDirectory([]);
      const [catalog, products, connections, servicePolicy] = await Promise.all(
        [
          request<unknown>(connection, "/connection-oauth/providers", {
            signal: controller.signal,
          }),
          request<unknown>(connection, "/projects", {
            signal: controller.signal,
          }),
          request<unknown>(connection, "/connection-oauth", {
            signal: controller.signal,
          }),
          request<unknown>(connection, "/oauth-provider-policies", {
            signal: controller.signal,
          }),
        ],
      );
      const entries = readOAuthProviders(catalog),
        choices = readProjects(products),
        listed = readOAuthDirectory(connections),
        policyState = readPolicyDirectory(servicePolicy);
      const resume = readResume(
        sessionStorage.getItem(resumeKey),
        connection.base,
        actor,
      );
      const selectedMarker = currentName
        ? connectionResume(resume, currentName)
        : null;
      if (controller.signal.aborted) return;
      setProviders(entries);
      setProjects(choices);
      setDirectory(listed);
      setPolicyDirectory(policyState);
      let closed = false;
      if (currentName && selectedMarker?.pending) {
        const receipt = readOAuthReceipt(
          await request<unknown>(
            connection,
            `/connection-oauth/${currentName}/effects/${selectedMarker.pending.requestId}`,
            { signal: controller.signal },
          ),
        );
        closed = receiptResolves(receipt, selectedMarker.pending);
      }
      const summary = currentName
        ? readOAuthSummary(
            await request<unknown>(
              connection,
              `/connection-oauth/${currentName}`,
              { signal: controller.signal },
            ),
            currentName,
          )
        : null;
      if (controller.signal.aborted) return;
      setProviders(entries);
      setProjects(choices);
      setDirectory(
        summary
          ? listed.map((entry) =>
              entry.name === summary.name ? summary : entry,
            )
          : listed,
      );
      if (summary) {
        setSaved(summary);
        if (
          closed &&
          selectedMarker?.pending?.revision === summary.revision &&
          retain(summary.name, false)
        ) {
          setUncertain(false);
          setNotice(
            "The matching request has a closed server receipt. Current actions were checked again; no provider request was repeated.",
          );
        }
      }
      return closed;
    },
    [connection, actor, retain],
  );
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    const resume = readResume(
      sessionStorage.getItem(resumeKey),
      connection.base,
      actor,
    );
    if (resume) {
      setName(resume.name);
      setUncertain(resume.uncertain);
      setEditing(true);
      setStep(3);
    } else sessionStorage.removeItem(resumeKey);
    void read(controller, resume?.name)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(failure(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setBusy(false);
          loaded.current = true;
        }
      });
    return () => active.current?.abort();
  }, [read, actor, connection.base]);
  useLayoutEffect(() => {
    const main = view.current?.closest("main");
    if (main) main.scrollTop = 0;
  }, [step, editing]);
  useEffect(() => {
    if (error) alert.current?.focus();
  }, [error]);
  async function reload(event?: FormEvent) {
    event?.preventDefault();
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setSecret("");
    try {
      const selected = name ? connectionName(name) : undefined;
      setSaved(null);
      const resolved = await read(controller, selected);
      if (!controller.signal.aborted) {
        setNotice(
          resolved
            ? "The matching request has a closed server receipt. Current actions were checked again; no provider request was repeated."
            : "Current account state refreshed. An unknown provider outcome still requires its server receipt or explicit recovery.",
        );
        if (selected) {
          setEditing(true);
          setStep(3);
        }
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(failure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function start() {
    if (busy) return;
    setUncertain(false);
    setSaved(null);
    setName("");
    setClientId("");
    setProject("");
    setSecret("");
    setProvider(available[0]?.provider || "");
    setStep(0);
    setEditing(true);
    setNotice("");
    setError("");
    setDisconnecting(false);
    setRecovering(false);
    setRecoveryReason("");
    setAcknowledged(false);
  }
  function recover() {
    if (
      !saved ||
      busy ||
      !saved.allowed_actions.supersede ||
      current?.status === "prerequisite_required"
    )
      return;
    setProvider(saved.provider);
    setName(saved.name);
    setProject(saved.project_id || "");
    setClientId(saved.client_id);
    setClientType(saved.public_client ? "public" : "confidential");
    setAuthentication(saved.authentication);
    setSecret("");
    setRecoveryReason("");
    setAcknowledged(false);
    setRecovering(true);
    setStep(1);
    setNotice("");
    setError("");
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (
      busy ||
      (uncertain && !recovering) ||
      (saved && !recovering) ||
      (recovering &&
        (!saved?.allowed_actions.supersede ||
          !acknowledged ||
          !recoveryReason.trim())) ||
      current?.status !== "registration_available"
    )
      return;
    let payload;
    try {
      if (
        !recovering &&
        connectionResume(
          readResume(sessionStorage.getItem(resumeKey), connection.base, actor),
          name,
        )?.uncertain
      )
        throw new Error(
          "This account still has an unconfirmed action. Open its retained record instead of starting another request.",
        );
      if (!recovering && directory.some((entry) => entry.name === name))
        throw new Error(
          "That connection already exists. Open it from Connected accounts instead of replacing its registration.",
        );
      payload = registrationPayload({
        name,
        provider,
        project,
        clientId,
        clientType,
        authentication,
        secret,
      });
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Review the registration fields.",
      );
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setSecret("");
    setNotice("");
    if (!retain(name, true)) {
      setError(
        "This browser cannot remember another action reference. Resolve outstanding account requests or allow more session storage before continuing.",
      );
      setBusy(false);
      return;
    }
    try {
      const result = readOAuthSummary(
        await request<unknown>(
          connection,
          `/connection-oauth/${name}${recovering ? "/supersede" : ""}`,
          {
            method: recovering ? "POST" : "PUT",
            body: JSON.stringify(
              recovering
                ? {
                    expected_revision: saved!.revision,
                    unknown_effect_ids: saved!.unresolved_effects
                      .filter(
                        (e) =>
                          e.state === "unknown" &&
                          e.registration_revision === saved!.revision,
                      )
                      .map((e) => e.id),
                    reason: recoveryReason,
                    acknowledge_unknown: true,
                    registration: payload,
                  }
                : { ...payload, expected_revision: 0 },
            ),
            signal: controller.signal,
          },
        ),
        name,
      );
      if (controller.signal.aborted) return;
      if (
        result.provider !== payload.provider ||
        result.client_id !== payload.client_id ||
        result.project_id !== payload.project_id ||
        result.authentication !== payload.authentication ||
        result.public_client !== (clientType === "public") ||
        result.secret_configured !== (clientType === "confidential") ||
        result.status !== "authorization_required" ||
        (!recovering && result.revision !== 1) ||
        (recovering &&
          (result.revision !== saved!.revision + 1 ||
            saved!.unresolved_effects.some(
              (e) =>
                !result.archived_unknown_effects.some(
                  (old) => old.id === e.id && old.state === "unknown",
                ),
            )))
      )
        throw new Error(
          "The registration response did not match the submitted scope.",
        );
      setSaved(result);
      retain(name, false);
      setUncertain(false);
      setRecovering(false);
      setRecoveryReason("");
      setAcknowledged(false);
      setStep(2);
      setNotice(
        recovering
          ? "New registration saved. Earlier provider outcomes remain unknown; browser consent has not started."
          : "Registration saved. Browser consent has not started.",
      );
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(failure(cause));
        setUncertain(true);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function consent() {
    if (
      !saved ||
      busy ||
      uncertain ||
      !saved.allowed_actions.authorize ||
      !saved.policy_current
    )
      return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setSecret("");
    try {
      const response = await request<unknown>(
        connection,
        `/connection-oauth/${saved.name}/prepare`,
        { method: "POST", signal: controller.signal },
      );
      const url = continuation(
        response,
        saved.name,
        connection.base,
        window.location.href,
      );
      if (controller.signal.aborted) return;
      if (!retain(saved.name, false))
        throw new Error("Resume storage unavailable");
      window.location.assign(url);
    } catch (cause) {
      if (!controller.signal.aborted) setError(failure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function mutate(kind: "refresh" | "disconnect") {
    if (
      !saved ||
      busy ||
      !saved.allowed_actions[kind] ||
      (kind === "refresh" && (uncertain || !saved.policy_current))
    )
      return;
    const previousMarker = connectionResume(
      readResume(sessionStorage.getItem(resumeKey), connection.base, actor),
      saved.name,
    );
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    const pending: OAuthPending | undefined =
      kind === "refresh"
        ? {
            requestId: crypto.randomUUID().replaceAll("-", ""),
            revision: saved.revision,
            kind: "refresh",
          }
        : previousMarker?.pending;
    if (!retain(saved.name, true, pending)) {
      setError(
        "This browser cannot remember another action reference. Resolve outstanding account requests or allow more session storage before continuing.",
      );
      setBusy(false);
      return;
    }
    try {
      const result = readOAuthSummary(
        await request<unknown>(
          connection,
          `/connection-oauth/${saved.name}/${kind}`,
          {
            method: "POST",
            ...(kind === "refresh"
              ? {
                  body: JSON.stringify({
                    request_id: pending!.requestId,
                  }),
                }
              : {}),
            signal: controller.signal,
          },
        ),
        saved.name,
      );
      if (controller.signal.aborted) return;
      setSaved(result);
      setDisconnecting(false);
      const stillUnknown =
        kind === "disconnect" && Boolean(previousMarker?.uncertain);
      retain(
        saved.name,
        stillUnknown,
        stillUnknown ? previousMarker?.pending : undefined,
      );
      setUncertain(stillUnknown);
      setNotice(
        kind === "refresh"
          ? "Refresh response retained. This does not verify the account or enable agent tools."
          : "Credentials disconnected locally. Provider-side authorization was not revoked.",
      );
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(failure(cause));
        setUncertain(true);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  if (policyEditing && policyDirectory)
    return (
      <OAuthServiceConfiguration
        connection={connection}
        actor={actor}
        initial={policyDirectory}
        onClose={() => setPolicyEditing(false)}
        onComplete={() => {
          const controller = new AbortController();
          active.current?.abort();
          active.current = controller;
          setBusy(true);
          setError("");
          void read(controller)
            .then(() => {
              if (controller.signal.aborted) return;
              setPolicyEditing(false);
              setUncertain(false);
              setSaved(null);
              setName("");
              setClientId("");
              setProject("");
              setSecret("");
              setProvider(localProvider);
              setStep(1);
              setEditing(true);
              setNotice("");
              setRecovering(false);
            })
            .catch((cause) => {
              if (!controller.signal.aborted) setError(failure(cause));
            })
            .finally(() => {
              if (!controller.signal.aborted) setBusy(false);
            });
        }}
      />
    );
  const titles = [
    "Choose a service",
    "Register the authorized client",
    "Review browser authorization",
    "Inspect the retained connection",
  ];
  const descriptions = [
    "Choose a service available here.",
    "Use the client registration approved for this factory. Credentials stay private.",
    "Check the exact resource and scopes before opening the provider's consent page.",
    "Retained authorization and executable tools are separate decisions.",
  ];
  const details = saved ? (
    <>
      {!saved.policy_current && (
        <section className="oauth-policy-changed" role="status">
          <h3>Service configuration changed</h3>
          <p>
            Review this service’s configuration before another authorization.
            These records retain the earlier permissions; they do not enable
            provider access.
          </p>
        </section>
      )}
      <dl className="wizard-summary">
        <div>
          <dt>Current state</dt>
          <dd>{statusNames[saved.status]}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd>{providerNames[saved.provider] || saved.provider}</dd>
        </div>
        <div>
          <dt>Account scope</dt>
          <dd>
            {saved.project_id
              ? projects.find((p) => p.id === saved.project_id)?.definition
                  .name || "Project identity unavailable"
              : "Factory-wide"}
          </dd>
        </div>
        <div>
          <dt>Resource</dt>
          <dd>{saved.resource}</dd>
        </div>
        <div>
          <dt>Requested scopes</dt>
          <dd>{saved.scopes.join(", ")}</dd>
        </div>
        {saved.grant_expires_at && (
          <div>
            <dt>Authorization expires</dt>
            <dd>{saved.grant_expires_at}</dd>
          </div>
        )}
        {saved.effect_state !== "none" && (
          <div>
            <dt>Outstanding request</dt>
            <dd>
              {saved.effect_state === "executing"
                ? "A provider request is in progress. Wait for its server receipt."
                : "The provider outcome is unknown. A new revision requires explicit recovery."}
            </dd>
          </div>
        )}
        <div>
          <dt>Agent tools</dt>
          <dd>
            Unavailable until the account and executable adapter pass their
            prerequisites.
          </dd>
        </div>
      </dl>
      {!!saved.archived_unknown_effects.length && (
        <section className="oauth-history">
          <h3>Previous provider outcomes remain unknown</h3>
          <p className="external-hint">
            {saved.archived_unknown_effects.length} earlier{" "}
            {saved.archived_unknown_effects.length === 1
              ? "request has"
              : "requests have"}{" "}
            no confirmed provider outcome. Starting a new revision did not
            reconcile them.
          </p>
        </section>
      )}
      <details className="oauth-identity">
        <summary>Connection details and request history</summary>
        <dl className="wizard-summary">
          <div>
            <dt>Registration</dt>
            <dd>
              {saved.name} · revision {saved.revision}
            </dd>
          </div>
          <div>
            <dt>Client ID</dt>
            <dd>{saved.client_id}</dd>
          </div>
          <div>
            <dt>Client credential</dt>
            <dd>
              {saved.secret_configured
                ? "Private secret configured; write-only"
                : "Public client; no secret"}
            </dd>
          </div>
          <div>
            <dt>Callback address</dt>
            <dd>{saved.callback_uri}</dd>
          </div>
          {saved.grant_revision && (
            <div>
              <dt>Authorization revision</dt>
              <dd>{saved.grant_revision}</dd>
            </div>
          )}
        </dl>
        {[...saved.unresolved_effects, ...saved.archived_unknown_effects].map(
          (effect) => (
            <article className="oauth-effect" key={effect.id}>
              <strong>
                {effect.kind === "code"
                  ? "Browser authorization"
                  : "Token refresh"}{" "}
                · {effect.state}
              </strong>
              <p>
                Registration revision {effect.registration_revision}
                {effect.lease_expired
                  ? " · lease expired"
                  : " · lease not expired"}
              </p>
              <code>{effect.id}</code>
            </article>
          ),
        )}
      </details>
    </>
  ) : null;
  return (
    <div className="external-tools browser-connections" ref={view}>
      {error && (
        <div ref={alert} role="alert" tabIndex={-1} className="alert">
          <p>{error}</p>
        </div>
      )}
      {uncertain && (
        <section className="oauth-uncertain" role="status">
          <ShieldCheck size={20} />
          <div>
            <strong>Outstanding action needs server reconciliation</strong>
            <p>
              The last response was not confirmed. Refreshing shows current
              account state; it cannot resolve an unknown provider request.
              Check the matching receipt or use explicit recovery before another
              authorization or refresh.
            </p>
          </div>
        </section>
      )}
      {!editing ? (
        <>
          <section className="oauth-introduction">
            <div>
              <h2>Authorize a service in your browser</h2>
              <p>
                Register a client, review its scope, then authorize it with your
                signed-in account. Saving a registration does not enable tools.
              </p>
            </div>
            <button
              className="primary"
              disabled={busy || !available.length || uncertain}
              onClick={start}
            >
              Set up a connection
              <ArrowRight size={16} />
            </button>
          </section>
          {policyDirectory?.local_development_available && (
            <section className="oauth-local-entry">
              <Server size={22} />
              <div>
                <h3>Owned local service</h3>
                <p>
                  Configure the fixed local repository service before
                  registering an account. Vendor services keep their separate
                  prerequisites.
                </p>
              </div>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setSecret("");
                  setPolicyEditing(true);
                }}
              >
                Configure local service
                <ArrowRight size={16} />
              </button>
            </section>
          )}
          <div
            className="administration-cards"
            aria-label="OAuth provider prerequisites"
          >
            {providers.map((p) => (
              <article key={p.provider}>
                <Globe size={22} />
                <h3>{providerNames[p.provider] || p.provider}</h3>
                <span className="badge">
                  {p.status === "registration_available"
                    ? "Registration available"
                    : "Prerequisites required"}
                </span>
                <p>{p.reason}</p>
                <button className="secondary" onClick={onTools}>
                  Review tool prerequisites
                  <ArrowRight size={16} />
                </button>
              </article>
            ))}
          </div>
          <section
            className="oauth-directory"
            aria-label="Connected accounts directory"
          >
            <div className="oauth-toolbar">
              <h2>Saved accounts</h2>
              <button
                className="text-button"
                disabled={busy}
                onClick={() => void reload()}
              >
                <RefreshCw size={16} />
                Refresh
              </button>
            </div>
            {!directory.length && !busy && (
              <p className="external-hint">
                No accounts are registered in this factory.
              </p>
            )}
            <div className="administration-cards">
              {directory.map((entry) => (
                <article key={entry.name}>
                  <Globe size={22} />
                  <h3>{entry.name}</h3>
                  <span className="badge">{statusNames[entry.status]}</span>
                  <p>
                    {providerNames[entry.provider] || entry.provider} ·{" "}
                    {entry.project_id
                      ? projects.find((p) => p.id === entry.project_id)
                          ?.definition.name || "Project identity unavailable"
                      : "Factory-wide"}
                  </p>
                  <button
                    className="secondary"
                    disabled={busy}
                    aria-label={`Open ${entry.name}`}
                    onClick={() => {
                      setName(entry.name);
                      setSaved(null);
                      setEditing(true);
                      setStep(3);
                      setSecret("");
                      setError("");
                      setBusy(true);
                      const controller = new AbortController();
                      active.current?.abort();
                      active.current = controller;
                      const marker = readResume(
                        sessionStorage.getItem(resumeKey),
                        connection.base,
                        actor,
                      );
                      setUncertain(
                        marker?.name === entry.name && marker.uncertain,
                      );
                      void read(controller, entry.name)
                        .catch((cause) => {
                          if (!controller.signal.aborted)
                            setError(failure(cause));
                        })
                        .finally(() => {
                          if (!controller.signal.aborted) setBusy(false);
                        });
                    }}
                  >
                    Open account
                    <ArrowRight size={16} />
                  </button>
                </article>
              ))}
            </div>
          </section>
        </>
      ) : (
        <>
          <div className="oauth-toolbar">
            <button
              className="text-button"
              disabled={busy}
              onClick={() => {
                setEditing(false);
                setSecret("");
                setError("");
              }}
            >
              <ArrowLeft size={16} />
              Provider overview
            </button>
            <button
              className="text-button"
              disabled={busy || !loaded.current}
              onClick={() => void reload()}
            >
              <RefreshCw size={16} />
              Refresh
            </button>
          </div>
          <Wizard
            className={`external-tools-wizard oauth-wizard${step === 0 ? " oauth-provider-step" : ""}`}
            parentLayout={step === 0}
            progressMode="visited"
            minimumFieldHeight={64}
            steps={[
              "Prerequisites",
              "Client & scope",
              "Browser consent",
              "Connection state",
            ]}
            active={step}
            onStep={(value) => {
              if (saved && value < 2) return;
              setStep(value);
              setSecret("");
            }}
            locked={busy}
            title={
              recovering && step === 1
                ? "Start a new authorization revision"
                : titles[step]
            }
            description={descriptions[step]}
            headingAction={
              notice ? (
                <p role="status" className="external-wizard-notice">
                  {notice}
                </p>
              ) : undefined
            }
            guide={
              <>
                <ShieldCheck size={28} />
                <h3>
                  {step === 1
                    ? "Private credentials"
                    : step === 2
                      ? "Consent stays in your browser"
                      : "Before agent tools can use this account"}
                </h3>
                <p>
                  {step === 1
                    ? "Confidential clients need a secret; public clients do not. Both use PKCE. Secret in body sends the secret in the token request; Basic authentication uses HTTP Basic. The secret is write-only and cleared after submission. Keep credentials out of model messages. Account scope still requires a separate tool grant."
                    : step === 2
                      ? "The factory checks your signed-in browser, one-time state and PKCE. The consent handoff expires after five minutes. No token or authorization code is shown here."
                      : "The factory must separately admit the account, adapter, resource and project tool grant. This flow cannot enable a blocked vendor template."}
                </p>
                <button className="text-button" onClick={onTools}>
                  <span>Review external tool prerequisites</span>
                  <ExternalLink size={16} />
                </button>
              </>
            }
          >
            <p className="external-short-context">{descriptions[step]}</p>
            {step === 0 ? (
              <form onSubmit={(event) => event.preventDefault()}>
                <div className="field">
                  <label htmlFor="oauth-provider">
                    <Globe size={16} />
                    Service
                  </label>
                  <select
                    id="oauth-provider"
                    value={provider}
                    onChange={(e) => setProvider(e.target.value)}
                    disabled={busy}
                  >
                    <option value="">Choose a service</option>
                    {providers.map((p) => (
                      <option
                        key={p.provider}
                        value={p.provider}
                        disabled={p.status !== "registration_available"}
                      >
                        {providerNames[p.provider] || p.provider}
                        {p.status !== "registration_available"
                          ? " · prerequisites required"
                          : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <details className="oauth-service-details">
                  <summary>Service requirements</summary>
                  <p className="external-hint">
                    {current?.reason ||
                      "Services need a supported factory configuration before you can authorize an account."}
                  </p>
                </details>
                <div className="wizard-actions">
                  <button
                    className="primary"
                    disabled={
                      busy || current?.status !== "registration_available"
                    }
                    onClick={() => setStep(1)}
                  >
                    Continue
                    <ArrowRight size={16} />
                  </button>
                </div>
              </form>
            ) : step === 1 ? (
              <form onSubmit={save} autoComplete="off">
                <fieldset
                  className="oauth-client-fields"
                  disabled={busy || (uncertain && !recovering)}
                >
                  <legend className="sr-only">
                    Authorized client registration
                  </legend>
                  <div className="field">
                    <label htmlFor="oauth-name">Connection name</label>
                    <input
                      id="oauth-name"
                      readOnly={recovering}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                      required
                      pattern="[a-z][a-z0-9-]{0,63}"
                      maxLength={64}
                    />
                    <small>Lowercase name; providers is reserved.</small>
                  </div>
                  <div className="field">
                    <label htmlFor="oauth-project">
                      <FolderKey size={16} />
                      Account scope
                    </label>
                    <select
                      id="oauth-project"
                      value={project}
                      onChange={(e) => setProject(e.target.value)}
                    >
                      <option value="">Factory-wide</option>
                      {projects.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.definition.name}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="oauth-client-id">Client ID</label>
                    <input
                      id="oauth-client-id"
                      value={clientId}
                      onChange={(e) => setClientId(e.target.value)}
                      maxLength={200}
                      required
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="oauth-client-type">Client type</label>
                    <select
                      id="oauth-client-type"
                      value={clientType}
                      onChange={(e) => {
                        setClientType(e.target.value);
                        setSecret("");
                      }}
                    >
                      <option value="public">Public</option>
                      <option value="confidential">Confidential</option>
                    </select>
                  </div>
                  {clientType === "confidential" && (
                    <>
                      <div className="field">
                        <label htmlFor="oauth-auth-method">
                          Client authentication
                        </label>
                        <select
                          id="oauth-auth-method"
                          value={authentication}
                          onChange={(e) => setAuthentication(e.target.value)}
                        >
                          <option value="client_secret_post">
                            Secret in body
                          </option>
                          <option value="client_secret_basic">
                            Basic authentication
                          </option>
                        </select>
                      </div>
                      <div className="field">
                        <label htmlFor="oauth-client-secret">
                          <KeyRound size={16} />
                          Client secret
                        </label>
                        <input
                          id="oauth-client-secret"
                          type="password"
                          value={secret}
                          onChange={(e) => setSecret(e.target.value)}
                          autoComplete="new-password"
                          required
                          spellCheck={false}
                        />
                        <small className="oauth-secret-hint">
                          Write-only; cleared after saving.
                        </small>
                      </div>
                    </>
                  )}
                </fieldset>
                {recovering && (
                  <section className="oauth-recovery">
                    <h3>Earlier provider outcomes remain unknown</h3>
                    <p className="external-hint">
                      This creates a new registration. It cannot prove whether
                      the earlier provider request completed, and the old tokens
                      cannot be reused.
                    </p>
                    <div className="field">
                      <label htmlFor="oauth-recovery-reason">
                        Reason for the new authorization
                      </label>
                      <textarea
                        id="oauth-recovery-reason"
                        value={recoveryReason}
                        onChange={(e) => setRecoveryReason(e.target.value)}
                        required
                        maxLength={2000}
                        disabled={busy}
                      />
                    </div>
                    <label className="oauth-acknowledgment">
                      <input
                        type="checkbox"
                        checked={acknowledged}
                        onChange={(e) => setAcknowledged(e.target.checked)}
                        disabled={busy}
                      />
                      I understand the previous provider outcome remains
                      unknown.
                    </label>
                  </section>
                )}
                <div className="wizard-actions">
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => {
                      setSecret("");
                      if (recovering) {
                        setRecovering(false);
                        setStep(3);
                      } else setStep(0);
                    }}
                  >
                    <ArrowLeft size={16} />
                    Back
                  </button>
                  <button
                    className="primary"
                    disabled={
                      busy ||
                      (uncertain && !recovering) ||
                      (recovering && (!acknowledged || !recoveryReason.trim()))
                    }
                  >
                    {busy
                      ? "Saving…"
                      : recovering
                        ? "Save new authorization revision"
                        : "Save registration"}
                    <ArrowRight size={16} />
                  </button>
                </div>
              </form>
            ) : step === 2 ? (
              <form onSubmit={(event) => event.preventDefault()}>
                {details}
                <p className="external-hint">
                  Opening consent takes you to the selected service. Saving and
                  refreshing account state do not contact it. Use your signed-in
                  factory browser.
                </p>
                <div className="wizard-actions">
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => setStep(3)}
                  >
                    Inspect state
                  </button>
                  <button
                    className="primary"
                    disabled={
                      busy ||
                      uncertain ||
                      !saved ||
                      !saved.allowed_actions.authorize ||
                      !saved.policy_current
                    }
                    onClick={() => void consent()}
                  >
                    Open browser consent
                    <ExternalLink size={16} />
                  </button>
                </div>
              </form>
            ) : (
              <form onSubmit={(event) => event.preventDefault()}>
                {details}
                {!saved && (
                  <p className="external-hint">
                    Current account state is unavailable. Refresh before making
                    a change.
                  </p>
                )}
                <p className="external-hint">
                  {saved?.refresh_available
                    ? "A refresh token is retained. The server decides whether your current account can use it."
                    : "No refresh token is available for this registration."}
                </p>
                {disconnecting && (
                  <div className="oauth-disconnect">
                    <h3>Disconnect locally?</h3>
                    <p>
                      This erases the retained active tokens and blocks further
                      factory use. Provider authorization must be revoked
                      separately in the provider account.
                    </p>
                  </div>
                )}
                <div className="wizard-actions">
                  {disconnecting ? (
                    <>
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={() => setDisconnecting(false)}
                      >
                        Keep connection
                      </button>
                      <button
                        className="primary"
                        disabled={busy || !saved?.allowed_actions.disconnect}
                        onClick={() => void mutate("disconnect")}
                      >
                        <Unplug size={16} />
                        Disconnect locally
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        className="secondary"
                        disabled={
                          busy || !saved || !saved.allowed_actions.disconnect
                        }
                        onClick={() => setDisconnecting(true)}
                      >
                        <Unplug size={16} />
                        Disconnect
                      </button>
                      {saved?.allowed_actions.supersede ? (
                        <button
                          className="primary"
                          disabled={
                            busy ||
                            !providers.some(
                              (p) =>
                                p.provider === saved.provider &&
                                p.status === "registration_available",
                            )
                          }
                          onClick={recover}
                        >
                          Start new authorization
                          <ArrowRight size={16} />
                        </button>
                      ) : saved?.status === "authorization_required" ? (
                        <button
                          className="primary"
                          disabled={busy || uncertain}
                          onClick={() => setStep(2)}
                        >
                          Review consent
                          <ArrowRight size={16} />
                        </button>
                      ) : (
                        <button
                          className="primary"
                          disabled={
                            busy ||
                            uncertain ||
                            !saved?.allowed_actions.refresh ||
                            !saved.policy_current
                          }
                          onClick={() => void mutate("refresh")}
                        >
                          <RefreshCw size={16} />
                          Request refresh
                        </button>
                      )}
                    </>
                  )}
                </div>
              </form>
            )}
          </Wizard>
        </>
      )}
    </div>
  );
}
