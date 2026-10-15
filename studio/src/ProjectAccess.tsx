/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Plus,
  ShieldCheck,
  Users,
  X,
} from "lucide-react";
import { request, type Connection, type ProjectSnapshot } from "./api";
import { type IdentitySession, type ProjectRole } from "./Identity";
import { Wizard } from "./Wizard";
import "./ProjectAccess.css";
import { ScmDelegation } from "./ScmDelegation";

type Member = {
  user_id: string;
  display_name: string | null;
  email: string | null;
  role: ProjectRole;
  active: boolean;
  revision: number;
};
type RegisteredUser = {
  contact_email?: string | null;
  email_verified?: boolean;
  id: string;
  display_name: string | null;
  email: string | null;
  enabled: boolean;
};
const label = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : "Project access could not be read.";
const roles: ProjectRole[] = [
  "owner",
  "maintainer",
  "developer",
  "reviewer",
  "viewer",
];

export function ProjectAccess({
  connection,
  session,
  onConfigure,
  onUpdated,
  initialUser,
}: {
  connection: Connection;
  session: IdentitySession | null;
  onConfigure: () => void;
  onUpdated: () => void;
  initialUser?: string;
}) {
  const [projects, setProjects] = useState<ProjectSnapshot[]>([]),
    [selected, setSelected] = useState("");
  const [members, setMembers] = useState<Member[]>([]),
    [users, setUsers] = useState<RegisteredUser[]>([]);
  const [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [revision, setRevision] = useState(0),
    [adding, setAdding] = useState(false);
  const current =
    initialUser && !selected
      ? undefined
      : (projects.find((item) => item.id === selected) ?? projects[0]);
  const projectId = current?.id;
  const canManage = Boolean(
    session &&
    (session.global_admin ||
      session.memberships.some(
        (item) =>
          item.project_id === current?.id &&
          item.permissions.includes("members"),
      )),
  );
  useEffect(() => {
    if (!session) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    request<ProjectSnapshot[]>(connection, "/projects", {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setProjects(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, session]);
  useEffect(() => {
    if (!session?.global_admin) {
      setUsers([]);
      return;
    }
    const controller = new AbortController();
    request<RegisteredUser[]>(connection, "/identity/users", {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setUsers(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      });
    return () => controller.abort();
  }, [connection, session, revision]);
  useEffect(() => {
    setMembers([]);
    setAdding(false);
    if (!projectId || !canManage) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    request<Member[]>(connection, `/projects/${projectId}/members`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setMembers(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(errorText(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, projectId, canManage, revision]);
  const changed = () => {
    setRevision((value) => value + 1);
    onUpdated();
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Project access</h1>
          <p>Choose who can work on each project and what they can do.</p>
        </div>
        {current && canManage && (
          <button className="primary" onClick={() => setAdding(true)}>
            <Plus size={18} />
            Add registered user
          </button>
        )}
      </div>
      {!session ? (
        <div className="empty">
          <ShieldCheck size={28} />
          <h2>Sign in to see your access</h2>
          <p>
            Your organization session identifies you. It does not automatically
            grant access to other projects.
          </p>
          <button className="primary" onClick={onConfigure}>
            Sign in
            <ArrowRight size={16} />
          </button>
        </div>
      ) : (
        <>
          <div className="access-toolbar">
            <div className="field">
              <label htmlFor="access-project">Product project</label>
              <select
                id="access-project"
                value={current?.id ?? ""}
                disabled={!projects.length}
                onChange={(event) => setSelected(event.target.value)}
              >
                {initialUser && !selected && (
                  <option value="">Choose the product project</option>
                )}
                {projects.length ? (
                  projects.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.definition.name}
                    </option>
                  ))
                ) : (
                  <option value="">No accessible product projects</option>
                )}
              </select>
            </div>
            <p>
              {session.global_admin
                ? "Factory administrator · project membership can be managed here."
                : current
                  ? `Your role: ${session.memberships.find((item) => item.project_id === current.id)?.role ?? "not assigned"}.`
                  : "Only projects you are allowed to read appear here."}
            </p>
          </div>
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          {loading && <p role="status">Loading project access…</p>}
          {current && !canManage && (
            <div className="access-notice">
              <ShieldCheck size={20} />
              <p>
                Your role does not allow membership changes. Ask this project’s
                owner or a factory administrator to assign access.
              </p>
            </div>
          )}
          {current && canManage && !loading && !members.length && (
            <div className="access-notice">
              <Users size={20} />
              <p>
                No memberships are recorded for this project. Add an existing
                registered user; an email address alone never grants access.
              </p>
            </div>
          )}
          {canManage && (
            <section
              className="access-members"
              aria-label="Recorded project members"
            >
              {members.map((member) => (
                <MemberEditor
                  key={`${current?.id}:${member.user_id}:${member.revision}`}
                  connection={connection}
                  project={current!.id}
                  member={member}
                  session={session}
                  onSaved={changed}
                />
              ))}
            </section>
          )}
          {current && Array.isArray(current.definition.components) && (
            <ScmDelegation
              key={JSON.stringify([
                connection.base,
                connection.token,
                session.user.id,
                session.global_admin,
                session.memberships,
                current.id,
                current.revision,
                current.digest,
              ])}
              connection={connection}
              session={session}
              project={current}
              people={users}
              onConfigure={onConfigure}
              onProjectRefresh={onUpdated}
            />
          )}
          <section className="access-role-guide">
            <h2>What each role can do</h2>
            <p>
              These permissions are returned by this server. Membership changes
              affect future requests; they do not change a run’s recorded
              history.
            </p>
            <div>
              {roles
                .filter((role) => Array.isArray(session.roles[role]))
                .map((role) => (
                  <article key={role}>
                    <h3>{label(role)}</h3>
                    <p>
                      {session.roles[role]
                        .map((permission) =>
                          permission === "members"
                            ? "manage membership"
                            : permission,
                        )
                        .join(" · ")}
                    </p>
                  </article>
                ))}
            </div>
          </section>
          {adding && current && (
            <MemberWizard
              key={current.id}
              connection={connection}
              project={current}
              session={session}
              users={users}
              initialUser={initialUser}
              onCancel={() => setAdding(false)}
              onSaved={() => {
                setAdding(false);
                changed();
              }}
            />
          )}
        </>
      )}
    </>
  );
}

function MemberEditor({
  connection,
  project,
  member,
  session,
  onSaved,
}: {
  connection: Connection;
  project: string;
  member: Member;
  session: IdentitySession;
  onSaved: () => void;
}) {
  const [role, setRole] = useState<ProjectRole | "">(
      member.active ? member.role : "",
    ),
    [confirm, setConfirm] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const changed = role !== (member.active ? member.role : "");
  async function save() {
    setBusy(true);
    setError("");
    try {
      await request(
        connection,
        `/projects/${project}/members/${member.user_id}`,
        { method: "PUT", body: JSON.stringify({ role: role || null }) },
      );
      onSaved();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className="access-member">
      <div>
        <h2>{member.display_name || member.user_id}</h2>
        {member.email && <p>{member.email}</p>}
        <small>
          {member.active ? "Active membership" : "Access removed"} · revision{" "}
          {member.revision}
        </small>
        <details>
          <summary>Registered user ID</summary>
          <code>{member.user_id}</code>
        </details>
      </div>
      <div className="field">
        <label htmlFor={`member-role-${member.user_id}`}>Project role</label>
        <select
          id={`member-role-${member.user_id}`}
          value={role}
          disabled={busy}
          onChange={(event) => {
            setRole(event.target.value as ProjectRole | "");
            setConfirm(false);
          }}
        >
          <option value="">No access</option>
          {roles
            .filter((value) => session.roles[value])
            .map((value) => (
              <option key={value} value={value}>
                {label(value)}
              </option>
            ))}
        </select>
      </div>
      <button
        className="secondary"
        disabled={!changed || busy}
        onClick={() => setConfirm(true)}
      >
        Review change
      </button>
      {confirm && (
        <div className="access-change-review">
          <p>
            {role
              ? `Assign ${member.display_name || member.user_id} the ${role} role.`
              : `Remove ${member.display_name || member.user_id} from this project.`}{" "}
            This changes access to future project requests.
          </p>
          <button
            className="primary"
            disabled={busy}
            onClick={() => void save()}
          >
            {busy ? "Saving…" : "Confirm access change"}
          </button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() => setConfirm(false)}
          >
            Keep current access
          </button>
        </div>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
    </article>
  );
}

function MemberWizard({
  connection,
  project,
  session,
  users,
  onCancel,
  onSaved,
  initialUser,
}: {
  connection: Connection;
  project: ProjectSnapshot;
  session: IdentitySession;
  users: RegisteredUser[];
  initialUser?: string;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [step, setStep] = useState(0),
    [user, setUser] = useState(initialUser ?? ""),
    [role, setRole] = useState<ProjectRole>("viewer"),
    [confirmed, setConfirmed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);
  const [query, setQuery] = useState(""),
    [matches, setMatches] = useState<RegisteredUser[]>([]),
    [searching, setSearching] = useState(false),
    [searchError, setSearchError] = useState("");
  useEffect(() => {
    if (session.global_admin || query.trim().length < 3) {
      setMatches([]);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    setSearchError("");
    const timer = window.setTimeout(() => {
      request<RegisteredUser[]>(
        connection,
        `/projects/${project.id}/eligible-users?query=${encodeURIComponent(query.trim())}`,
        { signal: controller.signal },
      )
        .then((value) => {
          if (!controller.signal.aborted) setMatches(value);
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setSearchError(errorText(cause));
        })
        .finally(() => {
          if (!controller.signal.aborted) setSearching(false);
        });
    }, 300);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [connection, project.id, query, session.global_admin]);
  const available = session.global_admin ? users : matches;
  const person = available.find((item) => item.id === user);
  const personName =
    person?.display_name ||
    person?.contact_email ||
    person?.email ||
    (user === session.user.id &&
    session.user.authentication_method === "local_bootstrap"
      ? "Local operator"
      : "Registered user");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (step < 3) {
      setStep((value) => value + 1);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await request(connection, `/projects/${project.id}/members/${user}`, {
        method: "PUT",
        body: JSON.stringify({ role }),
      });
      onSaved();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      className="access-member-dialog"
      ref={dialog}
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onCancel();
      }}
      aria-label="Assign project access"
    >
      <div className="access-dialog-heading">
        <h2>Add a registered user</h2>
        <button
          className="quiet"
          aria-label="Close membership setup"
          disabled={busy}
          onClick={onCancel}
        >
          <X size={20} />
        </button>
      </div>
      <Wizard
        steps={["Person", "Role", "Review", "Assign"]}
        active={step}
        onStep={setStep}
        locked={busy}
        title={
          [
            "Choose the person",
            "Choose their project role",
            "Review the access you will grant",
            "Assign the reviewed membership",
          ][step]
        }
        description={
          [
            "Find an existing account, then choose the person to add.",
            "Permissions apply only to this logical product project and its component repositories.",
            "Check the person, product project and allowed actions before changing access.",
            "Confirm to give this person the role shown below.",
          ][step]
        }
      >
        <form onSubmit={(event) => void submit(event)}>
          {step === 0 && (
            <>
              {!session.global_admin && (
                <div className="field">
                  <label htmlFor="access-find">Find a person</label>
                  <input
                    id="access-find"
                    value={query}
                    maxLength={120}
                    autoComplete="off"
                    placeholder="Name or email address"
                    onChange={(event) => {
                      setQuery(event.target.value);
                      setUser("");
                      setConfirmed(false);
                    }}
                  />
                  <small>
                    Enter at least three characters to find an existing account.
                  </small>
                  {searching && <p role="status">Finding people…</p>}
                  {searchError && (
                    <p className="alert" role="alert">
                      {searchError}
                    </p>
                  )}
                </div>
              )}
              <div className="field">
                <label htmlFor="access-person">Registered user</label>
                <select
                  id="access-person"
                  required
                  value={user}
                  disabled={
                    searching || (!session.global_admin && !matches.length)
                  }
                  onChange={(event) => {
                    setUser(event.target.value);
                    setConfirmed(false);
                  }}
                >
                  <option value="">Choose an existing user</option>
                  {available
                    .filter((item) => item.enabled)
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.display_name ||
                          item.contact_email ||
                          item.email ||
                          (item.id === session.user.id &&
                          session.user.authentication_method ===
                            "local_bootstrap"
                            ? "Local operator"
                            : "Registered user")}
                        {item.display_name && (item.contact_email || item.email)
                          ? ` · ${item.contact_email || item.email}`
                          : ""}
                      </option>
                    ))}
                </select>
                {!searching &&
                  !session.global_admin &&
                  query.trim().length >= 3 &&
                  !matches.length &&
                  !searchError && (
                    <small>
                      No matching accounts. Ask the person to sign in first.
                    </small>
                  )}
              </div>
            </>
          )}
          {step === 1 && (
            <div className="field">
              <label htmlFor="access-new-role">Project role</label>
              <select
                id="access-new-role"
                value={role}
                onChange={(event) => {
                  setRole(event.target.value as ProjectRole);
                  setConfirmed(false);
                }}
              >
                {roles
                  .filter((value) => session.roles[value])
                  .map((value) => (
                    <option key={value} value={value}>
                      {label(value)}
                    </option>
                  ))}
              </select>
              <p>
                {session.roles[role]
                  ?.map((value) =>
                    value === "members" ? "manage membership" : value,
                  )
                  .join(" · ")}
              </p>
            </div>
          )}
          {step >= 2 && (
            <section className="access-membership-review">
              <h3>{project.definition.name}</h3>
              <dl>
                <div>
                  <dt>Person</dt>
                  <dd>
                    {personName}
                    <details className="access-review-user-id">
                      <summary>Account details</summary>
                      <code>{user}</code>
                    </details>
                  </dd>
                </div>
                <div>
                  <dt>Role</dt>
                  <dd>{label(role)}</dd>
                </div>
                <div>
                  <dt>Allowed actions</dt>
                  <dd>{session.roles[role]?.join(", ")}</dd>
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
                  I reviewed this person’s access to this project.
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
                disabled={busy}
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
              disabled={
                busy ||
                (step === 0 && (!user || searching)) ||
                (step === 3 && !confirmed)
              }
            >
              {busy
                ? "Assigning…"
                : step === 3
                  ? "Assign project access"
                  : "Continue"}
              <ArrowRight size={16} />
            </button>
          </div>
        </form>
      </Wizard>
    </dialog>
  );
}
