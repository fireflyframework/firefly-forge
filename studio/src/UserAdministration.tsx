/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Mail,
  Plus,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { ApiError, post, request, type Connection } from "./api";
import type { IdentitySession } from "./Identity";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";

type User = {
  id: string;
  display_name: string | null;
  email: string | null;
  contact_email?: string | null;
  email_verified?: boolean;
  enabled: boolean;
};
type Catalog = {
  provider: string;
  configured: boolean;
  client_id: string;
  issuer: string;
  provisioning_verified: boolean;
  mail_delivery_verified: boolean;
  required_permissions: string[];
  local_passwords_supported: false;
};
type Operation = {
  id: string;
  kind: string;
  status: string;
  user_id?: string;
  provider_created?: boolean;
  invitation_status?: string;
  mail_delivery_verified?: boolean;
  detail?: string;
  request?: { username?: string; email?: string; user_id?: string };
  journal_valid?: boolean;
};
const contact = (user: User) => user.contact_email || user.email;
const errorText = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The account request could not be completed.";

export function UserAdministration({
  connection,
  session,
  onConfigure,
  onAssign,
}: {
  connection: Connection;
  session: IdentitySession | null;
  onConfigure: () => void;
  onAssign: (user: string) => void;
}) {
  const [users, setUsers] = useState<User[]>([]),
    [catalog, setCatalog] = useState<Catalog | null>(null),
    [operations, setOperations] = useState<Operation[]>([]),
    [revision, setRevision] = useState(0),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [creating, setCreating] = useState(false),
    [result, setResult] = useState<Operation | null>(null);
  const [action, setAction] = useState<{
      user: User;
      kind: "invite" | "enable" | "suspend";
    } | null>(null),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!session?.global_admin) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.all([
      request<User[]>(connection, "/identity/users", {
        signal: controller.signal,
      }),
      request<Catalog>(connection, "/identity/administration", {
        signal: controller.signal,
      }),
      request<Operation[]>(connection, "/identity/operations", {
        signal: controller.signal,
      }),
    ])
      .then(([people, capabilities, receipts]) => {
        if (!controller.signal.aborted) {
          setUsers(people);
          setCatalog(capabilities);
          setOperations(receipts);
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, session, revision]);
  const refresh = () => setRevision((value) => value + 1);
  const name = (user: User) =>
    user.display_name ||
    contact(user) ||
    (user.id === session?.user.id &&
    session.user.authentication_method === "local_bootstrap"
      ? "Local operator"
      : "Registered user");
  async function confirmAction() {
    if (!action) return;
    setBusy(true);
    setError("");
    try {
      if (action.kind === "invite") {
        const receipt = await post<Operation>(
          connection,
          `/identity/users/${action.user.id}/invite`,
          { request_id: crypto.randomUUID().replaceAll("-", "") },
        );
        setResult(receipt);
      } else
        await request(connection, `/identity/users/${action.user.id}`, {
          method: "PUT",
          body: JSON.stringify({ enabled: action.kind === "enable" }),
        });
      setAction(null);
      refresh();
    } catch (cause) {
      setError(
        `${errorText(cause)} Refresh recorded activity before repeating an uncertain request.`,
      );
      setAction(null);
      refresh();
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>People</h1>
          <p>Add people, send invitations, and manage account access.</p>
        </div>
        <div className="feature-actions">
          <button className="secondary" disabled={loading} onClick={refresh}>
            <RefreshCw size={16} />
            Refresh activity
          </button>
          {session?.global_admin && (
            <button
              className="primary"
              disabled={!catalog?.configured}
              onClick={() => setCreating(true)}
            >
              <Plus size={16} />
              Add user
            </button>
          )}
        </div>
      </div>
      {!session ? (
        <div className="empty">
          <h2>Sign in first</h2>
          <button className="primary" onClick={onConfigure}>
            Sign in
          </button>
        </div>
      ) : !session.global_admin ? (
        <div className="access-notice">
          <ShieldCheck size={20} />
          <p>
            The people directory requires a factory administrator. Your own
            project access remains available in your account.
          </p>
        </div>
      ) : (
        <>
          {loading && (
            <p role="status">Loading people and recorded activity…</p>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          {catalog && !catalog.configured && (
            <div className="access-notice">
              <ShieldCheck size={20} />
              <p>
                Account creation and invitations are unavailable. Ask your
                factory administrator to connect the organization directory.
              </p>
            </div>
          )}
          {result && (
            <section className="access-operation-result" role="status">
              <h2>
                {result.status === "completed" && result.provider_created
                  ? "Account created"
                  : result.invitation_status === "provider_accepted"
                    ? "Invitation requested"
                    : "Account request recorded"}
              </h2>
              <p>
                {result.invitation_status === "provider_accepted"
                  ? "Your identity provider accepted the invitation request. Email delivery has not been verified."
                  : result.status === "needs_reconciliation"
                    ? "The provider outcome is uncertain. Inspect the recorded activity; no request is repeated automatically."
                    : result.detail ||
                      "Creating an account does not grant access to any product project."}
              </p>
              {result.status === "completed" && result.user_id && (
                <button
                  className="secondary"
                  onClick={() => onAssign(result.user_id!)}
                >
                  Assign project access
                  <ArrowRight size={16} />
                </button>
              )}
              <details>
                <summary>Request details</summary>
                <code>{result.id}</code>
                <p>{result.status.replaceAll("_", " ")}</p>
              </details>
            </section>
          )}
          <section
            className="access-members"
            aria-label="Registered user directory"
          >
            {users.map((user) => (
              <article className="access-member access-person" key={user.id}>
                <div>
                  <h2>{name(user)}</h2>
                  {contact(user) && <p>{contact(user)}</p>}
                  <details>
                    <summary>Account details</summary>
                    <code>{user.id}</code>
                    {typeof user.email_verified === "boolean" && (
                      <p>
                        {user.email_verified
                          ? "Email verified by organization sign-in"
                          : "Invitation address; not verified by organization sign-in"}
                      </p>
                    )}
                  </details>
                </div>
                <span className="badge">
                  {user.enabled ? "Access enabled" : "Access suspended"}
                </span>
                <div className="access-person-actions">
                  <button
                    className="secondary"
                    onClick={() => onAssign(user.id)}
                  >
                    Project access
                  </button>
                  <button
                    className="secondary"
                    disabled={
                      !catalog?.configured || !contact(user) || !user.enabled
                    }
                    onClick={() => setAction({ user, kind: "invite" })}
                  >
                    <Mail size={16} />
                    Invite
                  </button>
                  <button
                    className="quiet"
                    disabled={!catalog || user.id === session.user.id}
                    onClick={() =>
                      setAction({
                        user,
                        kind: user.enabled ? "suspend" : "enable",
                      })
                    }
                  >
                    {user.enabled ? "Suspend access" : "Restore access"}
                  </button>
                </div>
              </article>
            ))}
          </section>
          {!loading && !error && !users.length && (
            <p>No accounts are registered with this factory.</p>
          )}
          <details className="access-administration-details">
            <summary>Connection and recorded activity</summary>
            {catalog && (
              <dl>
                <div>
                  <dt>Organization directory</dt>
                  <dd>{catalog.provider}</dd>
                </div>
                <div>
                  <dt>Connection settings</dt>
                  <dd>
                    {catalog.configured
                      ? "Configured; permissions checked during the requested action"
                      : "Not configured"}
                  </dd>
                </div>
              </dl>
            )}
            <details>
              <summary>Advanced connection diagnostics</summary>
              <pre>{JSON.stringify(catalog, null, 2)}</pre>
            </details>
            {operations.map((operation) => (
              <article key={operation.id}>
                <h3>
                  {operation.kind === "invite"
                    ? "Invitation"
                    : "Account creation"}{" "}
                  ·{" "}
                  {operation.request?.email ||
                    operation.request?.username ||
                    "recorded request"}
                </h3>
                <p>
                  {operation.status.replaceAll("_", " ")}
                  {operation.detail ? ` · ${operation.detail}` : ""}
                </p>
                {operation.journal_valid === false && (
                  <p className="alert">
                    The recorded history did not verify. Inspect it before
                    taking another action.
                  </p>
                )}
                <details>
                  <summary>Technical receipt</summary>
                  <pre>{JSON.stringify(operation, null, 2)}</pre>
                </details>
              </article>
            ))}
          </details>
          {creating && catalog && (
            <CreateUser
              connection={connection}
              provider={catalog.provider}
              onCancel={() => setCreating(false)}
              onCreated={(receipt) => {
                setCreating(false);
                setResult(receipt);
                refresh();
              }}
            />
          )}
          {action && (
            <ActionReview
              title={
                action.kind === "invite"
                  ? "Invite this person"
                  : action.kind === "suspend"
                    ? "Suspend factory access"
                    : "Restore factory access"
              }
              busy={busy}
              onCancel={() => setAction(null)}
              onConfirm={() => void confirmAction()}
            >
              <h3>{name(action.user)}</h3>
              {contact(action.user) && <p>{contact(action.user)}</p>}
              <p>
                {action.kind === "invite"
                  ? "Ask the organization directory to email password setup and email verification. This does not grant project access, and delivery is not assumed."
                  : action.kind === "suspend"
                    ? "This revokes this person’s factory sessions and blocks future factory access. It does not delete their organization account."
                    : "Allow this registered person to access the factory again. Their recorded project memberships remain separate."}
              </p>
            </ActionReview>
          )}
        </>
      )}
    </>
  );
}

function CreateUser({
  connection,
  provider,
  onCancel,
  onCreated,
}: {
  connection: Connection;
  provider: string;
  onCancel: () => void;
  onCreated: (receipt: Operation) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);
  const [step, setStep] = useState(0),
    [username, setUsername] = useState(""),
    [email, setEmail] = useState(""),
    [first, setFirst] = useState(""),
    [last, setLast] = useState(""),
    [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [uncertain, setUncertain] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (step < 3) {
      setStep((value) => value + 1);
      return;
    }
    setBusy(true);
    setError("");
    try {
      onCreated(
        await post<Operation>(connection, "/identity/users", {
          request_id: crypto.randomUUID().replaceAll("-", ""),
          username: username.trim(),
          email: email.trim() || null,
          first_name: first.trim() || null,
          last_name: last.trim() || null,
        }),
      );
    } catch (cause) {
      const rejected = cause instanceof ApiError && cause.status === 422;
      setUncertain(!rejected);
      setConfirmed(false);
      setError(
        rejected
          ? errorText(cause)
          : `${errorText(cause)} Close this form and refresh recorded activity before requesting another account.`,
      );
      if (rejected) setStep(0);
    } finally {
      setBusy(false);
    }
  }
  const edit = (setter: (value: string) => void) => (value: string) => {
    setter(value);
    setConfirmed(false);
    if (!uncertain) setError("");
  };
  return (
    <dialog
      ref={dialog}
      className="access-member-dialog"
      aria-label="Add organization user"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onCancel();
      }}
    >
      <div className="access-dialog-heading">
        <h2>Add a person</h2>
        <button
          className="quiet"
          aria-label="Close user setup"
          disabled={busy}
          onClick={onCancel}
        >
          <X size={20} />
        </button>
      </div>
      <Wizard
        steps={["Person", "Connection", "Review", "Create"]}
        active={step}
        onStep={setStep}
        locked={busy || uncertain}
        title={
          [
            "Identify the person",
            "Choose the organization directory",
            "Review the new account",
            "Create the reviewed account",
          ][step]
        }
        description={
          [
            "A sign-in name is required. Add an email address if this person should receive an invitation.",
            "Use this factory’s configured directory. No password or administrator credential is entered here.",
            "Check the person and directory before creating an account. No project access is granted yet.",
            "Creating the account records the directory result. Invitation and project access are separate next actions.",
          ][step]
        }
      >
        <form onSubmit={(event) => void submit(event)}>
          {step === 0 && (
            <>
              <div className="field">
                <label htmlFor="new-login">Sign-in name</label>
                <input
                  id="new-login"
                  required
                  maxLength={150}
                  pattern="[A-Za-z0-9][A-Za-z0-9_.@-]{0,149}"
                  value={username}
                  onChange={(event) => edit(setUsername)(event.target.value)}
                  autoComplete="off"
                />
              </div>
              <div className="field">
                <label htmlFor="new-email">
                  Email · needed for an invitation
                </label>
                <input
                  id="new-email"
                  type="email"
                  maxLength={320}
                  value={email}
                  onChange={(event) => edit(setEmail)(event.target.value)}
                  autoComplete="email"
                />
              </div>
              <details>
                <summary>Display name · optional</summary>
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="new-first">First name · optional</label>
                    <input
                      id="new-first"
                      maxLength={100}
                      value={first}
                      onChange={(event) => edit(setFirst)(event.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="new-last">Last name · optional</label>
                    <input
                      id="new-last"
                      maxLength={100}
                      value={last}
                      onChange={(event) => edit(setLast)(event.target.value)}
                    />
                  </div>
                </div>
              </details>
            </>
          )}
          {step === 1 && (
            <div className="field">
              <label htmlFor="new-directory">Organization directory</label>
              <select
                id="new-directory"
                value={provider}
                onChange={() => setConfirmed(false)}
              >
                <option value={provider}>
                  {provider === "keycloak" ? "Keycloak" : provider}
                </option>
              </select>
              <p>
                Directory permissions are checked when the account is created. A
                saved connection alone is not a successful account operation.
              </p>
            </div>
          )}
          {step >= 2 && (
            <section className="access-membership-review">
              <h3>{[first, last].filter(Boolean).join(" ") || username}</h3>
              <dl>
                <div>
                  <dt>Sign-in name</dt>
                  <dd>{username}</dd>
                </div>
                <div>
                  <dt>Email</dt>
                  <dd>
                    {email || "Not provided; no invitation can be requested"}
                  </dd>
                </div>
                <div>
                  <dt>Directory</dt>
                  <dd>{provider === "keycloak" ? "Keycloak" : provider}</dd>
                </div>
              </dl>
              {step === 2 && (
                <label className="check-choice">
                  <input
                    type="checkbox"
                    required
                    checked={confirmed}
                    onChange={(event) => setConfirmed(event.target.checked)}
                  />
                  I reviewed this person and the account to create.
                </label>
              )}
            </section>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className="wizard-actions">
            {step ? (
              <button
                type="button"
                className="secondary"
                disabled={busy || uncertain}
                onClick={() => setStep((value) => value - 1)}
              >
                <ArrowLeft size={16} />
                Back
              </button>
            ) : (
              <button type="button" className="secondary" onClick={onCancel}>
                Cancel
              </button>
            )}
            <button
              className="primary"
              disabled={busy || uncertain || (step === 3 && !confirmed)}
            >
              {busy ? "Creating…" : step === 3 ? "Create account" : "Continue"}
              <ArrowRight size={16} />
            </button>
          </div>
        </form>
      </Wizard>
    </dialog>
  );
}

function ActionReview({
  title,
  busy,
  children,
  onCancel,
  onConfirm,
}: {
  title: string;
  busy: boolean;
  children: ReactNode;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="identity-dialog"
      aria-label={title}
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onCancel();
      }}
    >
      <header>
        <h2>{title}</h2>
        <button
          className="quiet"
          disabled={busy}
          aria-label="Close account action"
          onClick={onCancel}
        >
          <X size={20} />
        </button>
      </header>
      {children}
      <div className="identity-account-actions">
        <button className="secondary" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        <button className="primary" disabled={busy} onClick={onConfirm}>
          {busy ? "Recording…" : title}
        </button>
      </div>
    </dialog>
  );
}
