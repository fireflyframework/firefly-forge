/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { request, type Connection, type Repository } from "./api";
import { repositoryLabels } from "./RepositoryLabels";
import "./AgentLifecycle.css";
type Skill = {
  id: string;
  name: string;
  application?: string;
  provenance?: { license: string; revision: string; source_url: string };
};
type Tool = { id: string; availability: string; reason: string };
type Role = {
  id: string;
  name: string;
  enabled: boolean;
  execution_mode: string;
  stages: string[];
  skills: string[];
  allowed_tools: string[];
  instructions: string;
};
type Overrides = {
  enabled: string[];
  disabled: string[];
  skills: Record<string, string[]>;
  allowed_tools: Record<string, string[]>;
};
export type LifecycleCatalog = {
  document: { version: string; max_requests: number; roles: Role[] };
  effective_roles: Role[];
  overrides: Overrides;
  permissions: { edit_registry: boolean; edit_project: boolean };
  knowledge: { status: string; framework_scope: string; verification: string };
  skills: Skill[];
  tools: Tool[];
  limits: string;
  phases: {
    id: string;
    name: string;
    description: string;
    handoff: string;
    participants: {
      id: string;
      role_id?: string;
      name: string;
      execution_kind: string;
      enabled: boolean;
      optional: boolean;
      responsibilities: string[];
      inputs: string[];
      outputs: string[];
      skills: Skill[];
      tools: Tool[];
      knowledge_scope: string;
      selection: { status: string; profile_id?: string; provider?: string };
      condition?: string;
      policy_skills?: Skill[];
    }[];
  }[];
};
const human = (value: string) => value.replaceAll("_", " ");
const failure = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The request could not be completed.";
export function AgentLifecycle({
  connection,
  repositories,
}: {
  connection: Connection;
  repositories: Repository[];
}) {
  const [scope, setScope] = useState("");
  const [catalog, setCatalog] = useState<LifecycleCatalog | null>(null);
  const [phase, setPhase] = useState("product_definition");
  const [draft, setDraft] = useState<Role | null>(null);
  const [registryEdit, setRegistryEdit] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const path = scope
    ? `/repositories/${encodeURIComponent(scope)}/agent-roles/lifecycle`
    : "/agent-roles/lifecycle";
  useEffect(() => {
    const controller = new AbortController();
    request<LifecycleCatalog>(connection, path, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        if (!Array.isArray(value.phases))
          throw new Error(
            "The lifecycle catalog is unavailable on this server.",
          );
        setCatalog(value);
        setError("");
      })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setCatalog(null);
          setError(failure(cause));
        }
      });
    return () => controller.abort();
  }, [connection, path]);
  const selected =
    catalog?.phases.find((item) => item.id === phase) ?? catalog?.phases[0];
  const labels = repositoryLabels(repositories);
  function edit(roleId: string) {
    if (!catalog) return;
    setRegistryEdit(!scope);
    setDraft(
      structuredClone(
        (scope ? catalog.effective_roles : catalog.document.roles).find(
          (role) => role.id === roleId,
        )!,
      ),
    );
    setNotice("");
  }
  function toggle(
    field: "skills" | "allowed_tools",
    id: string,
    enabled: boolean,
  ) {
    setDraft(
      (current) =>
        current && {
          ...current,
          [field]: enabled
            ? [...current[field], id]
            : current[field].filter((value) => value !== id),
        },
    );
  }
  async function save() {
    if (!catalog || !draft) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (registryEdit)
        await request(connection, "/agent-roles", {
          method: "PUT",
          body: JSON.stringify({
            ...catalog.document,
            roles: catalog.document.roles.map((role) =>
              role.id === draft.id ? draft : role,
            ),
          }),
        });
      else {
        const overrides = structuredClone(catalog.overrides);
        overrides.skills[draft.id] = draft.skills;
        overrides.allowed_tools[draft.id] = draft.allowed_tools;
        if (draft.id === "docs") {
          overrides.enabled = overrides.enabled.filter((id) => id !== draft.id);
          overrides.disabled = overrides.disabled.filter(
            (id) => id !== draft.id,
          );
          (draft.enabled ? overrides.enabled : overrides.disabled).push(
            draft.id,
          );
        }
        await request(
          connection,
          `/repositories/${encodeURIComponent(scope)}/agent-roles`,
          { method: "PUT", body: JSON.stringify(overrides) },
        );
      }
      setCatalog(await request<LifecycleCatalog>(connection, path));
      setDraft(null);
      setNotice(
        "Role settings saved. Existing runs retain their frozen instructions.",
      );
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel agent-lifecycle" aria-label="Agent lifecycle">
      <div className="section-heading">
        <div>
          <h2>From product decisions to operation</h2>
          <p>
            Inspect participants, applied instructions and the evidence that
            moves work forward.
          </p>
        </div>
        <span className="badge">Current configuration</span>
      </div>
      <div className="field">
        <label htmlFor="lifecycle-project">Lifecycle project</label>
        <select
          id="lifecycle-project"
          value={scope}
          disabled={!!draft || busy}
          onChange={(event) => {
            setScope(event.target.value);
            setNotice("");
            setCatalog(null);
          }}
        >
          <option value="">Factory defaults</option>
          {repositories.map((repo) => (
            <option key={repo.id} value={repo.id}>
              {labels.get(repo.id)}
            </option>
          ))}
        </select>
        {draft && (
          <small>Save or close your draft before changing its project.</small>
        )}
      </div>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {!catalog && !error && <p role="status">Loading lifecycle…</p>}
      {catalog && selected && (
        <>
          <div className="lifecycle-layout">
            <details className="lifecycle-process" open>
              <summary>Lifecycle phases</summary>
              <nav className="lifecycle-phases" aria-label="Lifecycle phases">
                {catalog.phases.map((item, index) => (
                  <button
                    key={item.id}
                    className={item.id === selected.id ? "active" : ""}
                    aria-current={item.id === selected.id ? "step" : undefined}
                    onClick={() => setPhase(item.id)}
                  >
                    <span aria-hidden="true">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    {item.name}
                  </button>
                ))}
              </nav>
            </details>
            <div className="lifecycle-detail">
              <h3>{selected.name}</h3>
              <p>{selected.description}</p>
              {selected.participants.map((participant) => (
                <article className="lifecycle-participant" key={participant.id}>
                  <div className="section-heading">
                    <h4>{participant.name}</h4>
                    <span className="badge">
                      {participant.execution_kind === "deterministic"
                        ? "Deterministic service"
                        : participant.execution_kind === "configured_model"
                          ? "AI dialogue"
                          : "Run-selected AI"}
                    </span>
                  </div>
                  <p>{participant.responsibilities.join(". ")}</p>
                  <p className="subtle">
                    {participant.enabled
                      ? participant.optional
                        ? "Optional participant"
                        : "Required participant"
                      : "Disabled guidance"}
                    {participant.condition ? ` · ${participant.condition}` : ""}
                  </p>
                  <dl className="lifecycle-evidence">
                    <div>
                      <dt>Inputs</dt>
                      <dd>{participant.inputs.join("; ")}</dd>
                    </div>
                    <div>
                      <dt>Retained outputs</dt>
                      <dd>{participant.outputs.join("; ")}</dd>
                    </div>
                    <div>
                      <dt>Model or service</dt>
                      <dd>
                        {human(participant.selection.status)}
                        {participant.selection.profile_id
                          ? ` · ${participant.selection.profile_id} (${participant.selection.provider})`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>Knowledge scope</dt>
                      <dd>{human(participant.knowledge_scope)}</dd>
                    </div>
                  </dl>
                  <details>
                    <summary>Skills and tool authority</summary>
                    {participant.policy_skills?.length ? (
                      <p>
                        Project policy instructions also apply:{" "}
                        {participant.policy_skills
                          .map((skill) => skill.name)
                          .join(", ")}
                        .
                      </p>
                    ) : null}
                    {participant.skills.length ? (
                      participant.skills.map((skill) => (
                        <div className="lifecycle-skill" key={skill.id}>
                          <strong>{skill.name}</strong>
                          <small>
                            {human(skill.application ?? "instruction")}
                          </small>
                          {skill.provenance && (
                            <a
                              href={skill.provenance.source_url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              {skill.provenance.license} ·{" "}
                              {skill.provenance.revision.slice(0, 8)}
                            </a>
                          )}
                        </div>
                      ))
                    ) : (
                      <p>
                        No separate instruction skills are injected into this
                        participant.
                      </p>
                    )}
                    {participant.tools.map((tool) => (
                      <p key={tool.id}>
                        <strong>{human(tool.id)}</strong>:{" "}
                        {human(tool.availability)}. {tool.reason}
                      </p>
                    ))}
                    <small>
                      Configured grants require worker validation. Official CLI
                      tools follow their own permissions.
                    </small>
                  </details>
                  {participant.role_id &&
                    (scope
                      ? catalog.permissions.edit_project
                      : catalog.permissions.edit_registry) && (
                      <button
                        className="secondary"
                        onClick={() => edit(participant.role_id!)}
                        disabled={busy || !!draft}
                      >
                        Edit {participant.name}
                      </button>
                    )}
                </article>
              ))}
              <div className="lifecycle-handoff">
                <strong>Next handoff</strong>
                <p>{selected.handoff}</p>
              </div>
            </div>
          </div>
          {draft && (
            <form
              className="lifecycle-editor"
              aria-label={`Edit ${draft.name}`}
              onSubmit={(event) => {
                event.preventDefault();
                void save();
              }}
            >
              <h3>Edit {draft.name}</h3>
              <p>
                Execution binding stays {human(draft.execution_mode)} at{" "}
                {draft.stages.join(", ")}.{" "}
                {registryEdit
                  ? "Registry instructions apply to new runs."
                  : "Project overrides select skills and tools; instructions come from the factory registry."}
              </p>
              {draft.execution_mode === "deterministic" && (
                <p className="subtle">
                  These role instructions are retained metadata. Executable
                  checks and security guards remain fixed by the factory.
                </p>
              )}
              {registryEdit && (
                <div className="field">
                  <label htmlFor="role-instructions">Role instructions</label>
                  <textarea
                    id="role-instructions"
                    autoFocus
                    disabled={busy}
                    value={draft.instructions}
                    required
                    maxLength={8000}
                    onChange={(event) =>
                      setDraft({ ...draft, instructions: event.target.value })
                    }
                  />
                </div>
              )}
              {draft.id === "docs" && (
                <label className="check-choice">
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(event) =>
                      setDraft({ ...draft, enabled: event.target.checked })
                    }
                  />
                  Enable documentation guidance in the same coding worker
                </label>
              )}
              <div className="lifecycle-editor-options">
                <fieldset disabled={busy}>
                  <legend>Instruction skills</legend>
                  <div className="lifecycle-choices">
                    {catalog.skills.map((skill) => (
                      <label className="check-choice" key={skill.id}>
                        <input
                          type="checkbox"
                          checked={draft.skills.includes(skill.id)}
                          onChange={(event) =>
                            toggle("skills", skill.id, event.target.checked)
                          }
                        />
                        {skill.name}
                      </label>
                    ))}
                  </div>
                </fieldset>
                {draft.execution_mode === "coding_provider" && (
                  <fieldset disabled={busy}>
                    <legend>Requested native tools</legend>
                    {catalog.tools.map((tool) => (
                      <label className="check-choice" key={tool.id}>
                        <input
                          type="checkbox"
                          checked={draft.allowed_tools.includes(tool.id)}
                          onChange={(event) =>
                            toggle(
                              "allowed_tools",
                              tool.id,
                              event.target.checked,
                            )
                          }
                        />
                        {human(tool.id)} · {human(tool.availability)}
                      </label>
                    ))}
                  </fieldset>
                )}
              </div>
              <div className="card-actions">
                <button className="primary" disabled={busy}>
                  {busy
                    ? "Saving…"
                    : registryEdit
                      ? "Save factory role"
                      : "Save project role"}
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy}
                  onClick={() => setDraft(null)}
                >
                  Close draft
                </button>
              </div>
            </form>
          )}
          <details className="lifecycle-knowledge" open>
            <summary>Knowledge availability and evidence</summary>
            <p>
              {catalog.knowledge.status === "configured_unverified"
                ? "Configured, retrieval unverified"
                : "Knowledge is not configured"}
            </p>
            <p>
              Framework workspace: {human(catalog.knowledge.framework_scope)}.
              Project knowledge is scoped to the repository at invocation.
            </p>
            <p>{catalog.knowledge.verification}</p>
          </details>
          <p className="subtle lifecycle-limit">{catalog.limits}</p>
        </>
      )}
    </section>
  );
}
