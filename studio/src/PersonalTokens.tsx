/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  Check,
  Copy,
  KeyRound,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { request, type Connection, type ProjectSnapshot } from "./api";
import type { IdentitySession } from "./Identity";
import "./PersonalTokens.css";

type Credential = {
  id: string;
  name: string;
  prefix: string;
  project_ids: string[];
  permissions: ["read"];
  status: "active" | "expired" | "revoked";
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  predecessor_id: string | null;
};
type Page = { items: Credential[]; next_cursor: string | null };
type Disclosure = {
  credential: Credential;
  secret_state: "returned_once" | "not_retained";
  token?: string;
};
type Action = { kind: "rotate" | "revoke"; token: Credential };
const date = (value: string) => new Date(value).toLocaleString();
const message = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "The request could not be completed.";

export function PersonalTokens({
  connection,
  session,
}: {
  connection: Connection;
  session: IdentitySession;
}) {
  if (session.user.authentication_method !== "oidc_session")
    return (
      <p>
        Sign in with your organization to manage personal tokens. Local
        developer sessions cannot issue them.
      </p>
    );
  return (
    <TokenManager
      key={`${connection.base}/${session.user.id}`}
      base={connection.base}
      session={session}
    />
  );
}

function TokenManager({
  base,
  session,
}: {
  base: string;
  session: IdentitySession;
}) {
  const [page, setPage] = useState<Page>({ items: [], next_cursor: null });
  const [projects, setProjects] = useState<ProjectSnapshot[]>([]);
  const [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false),
    [action, setAction] = useState<Action | null>(null);
  const [name, setName] = useState(""),
    [expiry, setExpiry] = useState("");
  const [selected, setSelected] = useState<string[]>([]),
    [secret, setSecret] = useState("");
  const [copied, setCopied] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const allowed = new Set(
    session.memberships
      .filter((membership) => membership.permissions.includes("read"))
      .map((membership) => membership.project_id),
  );
  const choices = projects.filter((project) => allowed.has(project.id));
  const labels = new Map(
    projects.map((project) => [project.id, project.definition.name]),
  );

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    const connection = { base, token: "", authenticated: true };
    void Promise.all([
      request<Page>(connection, "/identity/tokens", {
        signal: current.signal,
        cache: "no-store",
      }),
      request<ProjectSnapshot[]>(connection, "/projects", {
        signal: current.signal,
        cache: "no-store",
      }),
    ])
      .then(([records, catalog]) => {
        if (!current.signal.aborted) {
          setPage(records);
          setProjects(catalog);
        }
      })
      .catch((cause) => {
        if (!current.signal.aborted) setError(message(cause));
      })
      .finally(() => {
        if (!current.signal.aborted) setLoading(false);
      });
    return () => {
      current.abort();
      controller.current = null;
    };
  }, [base]);

  async function refresh(after?: string) {
    setError("");
    setLoading(true);
    try {
      const [records, catalog] = await Promise.all([
        request<Page>(
          { base, token: "", authenticated: true },
          "/identity/tokens" +
            (after ? `?after=${encodeURIComponent(after)}` : ""),
          { signal: controller.current?.signal, cache: "no-store" },
        ),
        after
          ? Promise.resolve(null)
          : request<ProjectSnapshot[]>(
              { base, token: "", authenticated: true },
              "/projects",
              { signal: controller.current?.signal, cache: "no-store" },
            ),
      ]);
      if (controller.current) {
        if (catalog) setProjects(catalog);
        setPage((prior) =>
          after
            ? {
                items: [...prior.items, ...records.items],
                next_cursor: records.next_cursor,
              }
            : records,
        );
      }
    } catch (cause) {
      if (controller.current) setError(message(cause));
    } finally {
      if (controller.current) setLoading(false);
    }
  }
  async function mutate(event: FormEvent) {
    event.preventDefault();
    if (busy || !controller.current) return;
    if (
      action?.kind !== "revoke" &&
      (!expiry ||
        !Number.isFinite(new Date(expiry).valueOf()) ||
        new Date(expiry) <= new Date())
    ) {
      setError("Choose an expiry in the future.");
      return;
    }
    const intent = action;
    const path = intent
      ? `/identity/tokens/${intent.token.id}/${intent.kind}`
      : "/identity/tokens";
    const requestId = crypto.randomUUID().replaceAll("-", "");
    const body =
      intent?.kind === "revoke"
        ? {}
        : {
            request_id: requestId,
            expires_at: new Date(expiry).toISOString(),
            ...(!intent
              ? {
                  name: name.trim(),
                  project_ids: selected,
                  permissions: ["read"],
                }
              : {}),
          };
    setBusy(true);
    setError("");
    setNotice("");
    setSecret("");
    setCopied(false);
    // An uncertain mutation is inspected through the list, never replayed by this form.
    setAction(null);
    setCreating(false);
    try {
      const result = await request<Disclosure | Credential>(
        { base, token: "", authenticated: true },
        path,
        {
          method: "POST",
          body: JSON.stringify(body),
          signal: controller.current.signal,
          cache: "no-store",
        },
      );
      if (!controller.current) return;
      if ("secret_state" in result) {
        if (result.secret_state === "returned_once" && result.token)
          setSecret(result.token);
        else
          setNotice(
            "The token exists, but its secret is not retained. Revoke it and create another if you did not save it.",
          );
      } else
        setNotice(
          "Token revoked. Applications using it can no longer read your projects.",
        );
      setName("");
      setExpiry("");
      setSelected([]);
      await refresh();
    } catch (cause) {
      if (controller.current)
        setError(
          `${message(cause)} Refresh the list and inspect the token before submitting another request.`,
        );
    } finally {
      if (controller.current) setBusy(false);
    }
  }
  function start(next: Action | null) {
    setAction(next);
    setCreating(!next);
    setExpiry("");
    setError("");
    setSecret("");
  }

  return (
    <section className="personal-tokens" aria-label="Personal API tokens">
      <p>
        Give a script read-only access to selected projects. Tokens cannot run
        work, publish code or manage the factory.
      </p>
      <div className="token-toolbar">
        <button
          className="primary"
          disabled={
            busy ||
            loading ||
            creating ||
            !!action ||
            !choices.length ||
            !!secret
          }
          onClick={() => start(null)}
        >
          <Plus size={16} />
          New token
        </button>
        <button
          className="secondary"
          disabled={busy || loading}
          onClick={() => void refresh()}
        >
          <RefreshCw size={16} />
          Refresh list
        </button>
      </div>
      {!loading && !choices.length && (
        <p>
          No current project memberships are available. Ask a project owner to
          grant access first.
        </p>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
      {secret && (
        <div
          className="token-disclosure"
          role="region"
          aria-label="Save your token"
        >
          <h3>
            <KeyRound size={18} />
            Save your token now
          </h3>
          <p>
            This is the only time it is shown. Store it in your secret manager
            before closing this screen.
          </p>
          <label htmlFor="personal-token-secret">New token secret</label>
          <input
            id="personal-token-secret"
            value={secret}
            readOnly
            autoComplete="off"
            spellCheck={false}
          />
          <div className="token-toolbar">
            <button
              className="secondary"
              onClick={() => {
                void navigator.clipboard
                  .writeText(secret)
                  .then(() => setCopied(true))
                  .catch(() =>
                    setError(
                      "Copy is unavailable. Select the token and copy it manually.",
                    ),
                  );
              }}
            >
              {copied ? <Check size={16} /> : <Copy size={16} />}
              {copied ? "Copied" : "Copy token"}
            </button>
            <button
              className="primary"
              onClick={() => {
                setSecret("");
                setCopied(false);
              }}
            >
              I saved this token
            </button>
          </div>
        </div>
      )}
      {(creating || action) && (
        <form className="token-editor" onSubmit={(event) => void mutate(event)}>
          <h3>
            {action
              ? `${action.kind === "revoke" ? "Revoke" : "Rotate"} ${action.token.name}`
              : "Create a personal token"}
          </h3>
          {action ? (
            <p>
              {action.kind === "revoke"
                ? "This immediately disables the token. Applications using it will lose access."
                : "This immediately revokes the old token and returns a replacement once. Update applications with the new secret."}
            </p>
          ) : (
            <>
              <div className="field">
                <label htmlFor="personal-token-name">Token name</label>
                <input
                  id="personal-token-name"
                  required
                  maxLength={160}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="For example, release dashboard"
                />
              </div>
              <fieldset>
                <legend>Projects this token can read</legend>
                {choices.map((project) => (
                  <label className="token-project" key={project.id}>
                    <input
                      type="checkbox"
                      checked={selected.includes(project.id)}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? [...selected, project.id]
                            : selected.filter((id) => id !== project.id),
                        )
                      }
                    />
                    {project.definition.name}
                  </label>
                ))}
              </fieldset>
            </>
          )}
          {action?.kind !== "revoke" && (
            <div className="field">
              <label htmlFor="personal-token-expiry">Expires on</label>
              <input
                id="personal-token-expiry"
                type="datetime-local"
                required
                value={expiry}
                onChange={(event) => setExpiry(event.target.value)}
              />
              <small>
                Your local time zone:{" "}
                {Intl.DateTimeFormat().resolvedOptions().timeZone}. No expiry is
                chosen for you.
              </small>
            </div>
          )}
          <p className="token-scope">
            <ShieldCheck size={16} />
            Read-only access. Current project membership is checked on every
            request.
          </p>
          <div className="token-toolbar">
            <button
              className="primary"
              disabled={busy || (!action && (!selected.length || !name.trim()))}
            >
              {action?.kind === "revoke"
                ? "Revoke token"
                : action
                  ? "Rotate token"
                  : "Create token"}
            </button>
            <button
              className="secondary"
              type="button"
              onClick={() => {
                setCreating(false);
                setAction(null);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
      <div aria-busy={loading} className="token-list">
        {loading && <p role="status">Loading tokens…</p>}
        {!loading && !creating && !action && !secret && !page.items.length && (
          <p>
            No personal tokens yet. Create one for a script that needs to read
            your projects.
          </p>
        )}
        {page.items.map((token) => (
          <article className="token-row" key={token.id}>
            <div>
              <h3>{token.name}</h3>
              <p>
                {token.status} · Expires {date(token.expires_at)}
              </p>
              <p>
                {token.project_ids.map((id) => labels.get(id) || id).join(", ")}
              </p>
              <code>{token.prefix}…</code>
            </div>
            <div className="token-toolbar">
              <button
                className="secondary"
                disabled={busy || token.status === "revoked" || !!secret}
                aria-label={`Rotate ${token.name}`}
                onClick={() => start({ kind: "rotate", token })}
              >
                <RefreshCw size={16} />
                Rotate
              </button>
              <button
                className="secondary"
                disabled={busy || token.status === "revoked" || !!secret}
                aria-label={`Revoke ${token.name}`}
                onClick={() => start({ kind: "revoke", token })}
              >
                <Trash2 size={16} />
                Revoke
              </button>
            </div>
          </article>
        ))}
      </div>
      {page.next_cursor && (
        <button
          className="secondary"
          disabled={busy || loading}
          onClick={() => void refresh(page.next_cursor!)}
        >
          Load more tokens
        </button>
      )}
    </section>
  );
}
