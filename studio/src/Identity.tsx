/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowRight,
  LogOut,
  RefreshCw,
  ShieldCheck,
  UserRound,
  X,
} from "lucide-react";
import { ApiError, normalizeBase, post, request, type Connection } from "./api";
import "./Identity.css";

export type Permission =
  "read" | "edit" | "execute" | "review" | "release" | "members";
export type ProjectRole =
  "owner" | "maintainer" | "developer" | "reviewer" | "viewer";
export type IdentitySession = {
  authenticated: true;
  user: {
    id: string;
    display_name: string | null;
    email: string | null;
    authentication_method: string;
  };
  global_admin: boolean;
  memberships: {
    project_id: string;
    role: ProjectRole;
    revision: number;
    permissions: Permission[];
  }[];
  roles: Record<ProjectRole, Permission[]>;
};
export type IdentityConfiguration = {
  mode: "oidc" | "local_bootstrap";
  issuer: string;
  client_id?: string;
  redirect_uri?: string;
  session_seconds?: number;
  cookie_secure?: boolean;
};
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The identity service could not be read.";

export function useIdentity(base: string) {
  const [configuration, setConfiguration] =
    useState<IdentityConfiguration | null>(null);
  const [session, setSession] = useState<IdentitySession | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const previousBase = useRef(base);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const forgetSession = useCallback(() => setSession(null), []);
  useEffect(() => {
    const controller = new AbortController();
    const connection: Connection = { base, token: "" };
    setLoading(true);
    if (previousBase.current !== base) {
      setSession(null);
      setConfiguration(null);
      previousBase.current = base;
    }
    async function read() {
      try {
        const config = await request<IdentityConfiguration>(
          connection,
          "/auth/configuration",
          { signal: controller.signal },
        );
        if (!["oidc", "local_bootstrap"].includes(config.mode))
          throw new Error(
            "This server did not return a supported sign-in configuration.",
          );
        if (controller.signal.aborted) return;
        setConfiguration(config);
        try {
          const value = await request<IdentitySession>(
            connection,
            "/auth/session",
            { signal: controller.signal },
          );
          if (
            !value.authenticated ||
            !value.user?.id ||
            !Array.isArray(value.memberships)
          )
            throw new Error("This server did not return a valid user session.");
          if (!controller.signal.aborted) setSession(value);
        } catch (cause) {
          if (!(cause instanceof ApiError && cause.status === 401)) throw cause;
          if (!controller.signal.aborted) setSession(null);
        }
        if (!controller.signal.aborted) setError("");
      } catch (cause) {
        if (!controller.signal.aborted) setError(message(cause));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void read();
    return () => controller.abort();
  }, [base, revision]);
  useEffect(() => {
    const onFocus = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("forge-session-expired", refresh);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("forge-session-expired", refresh);
    };
  }, [refresh]);
  return { configuration, session, loading, error, refresh, forgetSession };
}

export function SignIn({
  connection,
  configuration,
  loading,
  configurationError,
  pendingGoal,
  onAddress,
  onReady,
  onRefresh,
}: {
  connection: Connection;
  configuration: IdentityConfiguration | null;
  loading: boolean;
  configurationError: string;
  pendingGoal: "overview" | "product";
  onAddress: (base: string) => void;
  onReady: () => void;
  onRefresh: () => void;
}) {
  const [address, setAddress] = useState(connection.base);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bootstrap = configuration?.mode === "local_bootstrap";
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    try {
      const base = normalizeBase(address);
      if (base !== connection.base) {
        onAddress(base);
        return;
      }
      if (!configuration) return;
      setBusy(true);
      const value = await post<{
        authenticated?: boolean;
        authorization_url?: string;
      }>({ base, token: "" }, "/auth/login", bootstrap ? { token } : {});
      setToken("");
      if (bootstrap) {
        if (!value.authenticated)
          throw new Error("The server did not establish a session.");
        onReady();
      } else {
        const destination = new URL(value.authorization_url ?? "");
        const issuer = new URL(configuration.issuer);
        if (
          destination.origin !== issuer.origin ||
          destination.username ||
          destination.password ||
          !(
            destination.protocol === "https:" ||
            (destination.protocol === "http:" &&
              ["localhost", "127.0.0.1", "[::1]"].includes(
                destination.hostname,
              ))
          )
        )
          throw new Error("The server returned an unexpected sign-in address.");
        sessionStorage.setItem(
          "forge.pending-goal",
          pendingGoal === "product" ? "/product" : "/overview",
        );
        window.location.assign(destination.href);
      }
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="settings-form identity-signin"
      onSubmit={(event) => void submit(event)}
    >
      <span className="identity-eyebrow">
        <ShieldCheck size={20} />
        {bootstrap ? "Local developer bootstrap" : "Organization sign-in"}
      </span>
      <h2>Sign in to your factory</h2>
      <p>
        {bootstrap
          ? "This server explicitly allows its local operator token to establish a developer session. Production access uses your organization’s identity provider."
          : "Your organization’s identity provider confirms who you are. Project membership controls what you can view or change."}
      </p>
      <div className="field">
        <label htmlFor="identity-address">API address</label>
        <input
          id="identity-address"
          required
          autoComplete="url"
          value={address}
          onChange={(event) => setAddress(event.target.value)}
        />
        <small>
          Use /api/v1 for this portal. Changing the address first loads that
          server’s sign-in settings.
        </small>
      </div>
      {bootstrap && (
        <div className="field">
          <label htmlFor="identity-token">API token</label>
          <input
            id="identity-token"
            type="password"
            required
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
          <small>
            Exchanged once for a protected server session, then cleared. Never
            saved in browser storage.
          </small>
        </div>
      )}
      {(error || configurationError) && (
        <div className="alert" role="alert">
          {error || configurationError}
        </div>
      )}
      <div className="identity-signin-actions">
        <button
          className="primary"
          disabled={
            loading || busy || (!configuration && address === connection.base)
          }
        >
          {loading || busy
            ? "Connecting…"
            : address !== connection.base
              ? "Load server"
              : bootstrap
                ? "Save connection"
                : "Sign in"}
          <ArrowRight size={16} />
        </button>
        {configurationError && (
          <button type="button" className="secondary" onClick={onRefresh}>
            <RefreshCw size={16} />
            Retry connection
          </button>
        )}
      </div>
      <p className="identity-cookie-note">
        Your session stays on the server. Reopening the portal checks its expiry
        and project permissions; sign out revokes it.
      </p>
    </form>
  );
}

export function IdentityMenu({
  connection,
  session,
  onSignedOut,
  onAccess,
}: {
  connection: Connection;
  session: IdentitySession;
  onSignedOut: () => void;
  onAccess: () => void;
}) {
  const displayName =
    session.user.display_name?.trim() ||
    session.user.email ||
    (session.user.authentication_method === "local_bootstrap"
      ? "Local operator"
      : "Signed-in user");
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function signOut() {
    setBusy(true);
    setError("");
    try {
      await post({ ...connection, token: "" }, "/auth/logout");
      dialog.current?.close();
      onSignedOut();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="identity-menu">
      <button
        className="secondary identity-trigger"
        aria-label={`Signed in as ${displayName}`}
        onClick={() => dialog.current?.showModal()}
      >
        <UserRound size={18} />
        <span>{displayName}</span>
      </button>
      <dialog
        className="identity-dialog"
        ref={dialog}
        aria-label="Your account"
      >
        <header>
          <h2>Your account</h2>
          <button
            className="quiet"
            aria-label="Close account"
            onClick={() => dialog.current?.close()}
          >
            <X size={20} />
          </button>
        </header>
        <strong>{displayName}</strong>
        {session.user.email && <p>{session.user.email}</p>}
        <p className="subtle">
          {session.user.authentication_method === "local_bootstrap"
            ? "Local developer session"
            : "Organization session"}
          {session.global_admin
            ? " · factory administrator"
            : " · project access assigned individually"}
        </p>
        <p>
          Signing in does not grant access to another project. Its owner or a
          factory administrator assigns membership.
        </p>
        <details>
          <summary>Registered user identity</summary>
          <code>{session.user.id}</code>
        </details>
        {error && (
          <div className="alert" role="alert">
            {error}
          </div>
        )}
        <div className="identity-account-actions">
          <button
            className="secondary"
            onClick={() => {
              dialog.current?.close();
              onAccess();
            }}
          >
            <ShieldCheck size={18} />
            Project access
          </button>
          <button
            className="primary"
            disabled={busy}
            onClick={() => void signOut()}
          >
            <LogOut size={18} />
            {busy ? "Signing out…" : "Sign out"}
          </button>
        </div>
      </dialog>
    </div>
  );
}
