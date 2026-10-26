/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export const toolNames: Record<string, string> = {
  get_file_contents: "Read repository files",
  get_commit: "Read a commit",
  list_commits: "List commits",
  list_branches: "List branches",
};
export type ToolRole = "implementer" | "docs";
type RecordData = Record<string, unknown>;
export type ToolDiscovery = {
  digest: string;
  credential_revision: string;
  tools: Record<string, RecordData>;
};
export type ToolConnector = {
  id: string;
  name: string;
  endpoint: string | null;
  prerequisite: string | null;
  enabled: boolean;
  credential_configured: boolean;
  readiness: string;
  discovery: ToolDiscovery | null;
  review: { digest: string; tools: string[] } | null;
};
export type ToolProject = {
  id: string;
  definition: { name: string; components: { repository_id: string }[] };
};
export type ToolRepository = { id: string; name: string; url: string };
export type ToolGrant = {
  id: string;
  active: boolean;
  project_id: string;
  repository_id: string;
  connector: string;
  credential_revision: string;
  review_digest: string;
  tools: Record<string, RecordData>;
  roles: ToolRole[];
  stages: string[];
  scope: { owner: string; repo: string };
};
const id = /^[a-f0-9]{32}$/;
const hash = /^[a-f0-9]{64}$/;
const states = [
  "prerequisite_required",
  "disabled",
  "credential_required",
  "discovery_required",
  "schema_review_required",
  "project_grant_required",
];
export const stateNames: Record<string, string> = {
  prerequisite_required: "Prerequisite required",
  disabled: "Disabled",
  credential_required: "Key required",
  discovery_required: "Discovery required",
  schema_review_required: "Schema review required",
  project_grant_required: "Project access required",
};
function record(value: unknown): RecordData {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(
      "The server returned unsupported external tool settings. Reload before making changes.",
    );
  return value as RecordData;
}
function text(value: unknown, limit = 120): string {
  if (typeof value !== "string" || !value || value.length > limit)
    throw new Error("The server returned unsupported external tool text.");
  return value;
}
function tools(value: unknown): Record<string, RecordData> {
  const source = record(value),
    names = Object.keys(source);
  if (
    !names.length ||
    names.length > 4 ||
    names.some((name) => !Object.hasOwn(toolNames, name)) ||
    JSON.stringify(source).length > 150000
  )
    throw new Error("The server returned unsupported reviewed tools.");
  return Object.fromEntries(
    names.map((name) => [name, structuredClone(record(source[name]))]),
  );
}
export function readConnector(value: unknown): ToolConnector {
  const source = record(value);
  if (
    !["github", "sentry", "figma", "playwright", "grafana"].includes(
      String(source.id),
    ) ||
    !states.includes(String(source.readiness)) ||
    typeof source.enabled !== "boolean" ||
    typeof source.credential_configured !== "boolean"
  )
    throw new Error("The server returned unsupported external tool state.");
  let discovery: ToolDiscovery | null = null,
    review: ToolConnector["review"] = null;
  if (source.discovery != null) {
    const d = record(source.discovery);
    if (!hash.test(String(d.digest)) || !id.test(String(d.credential_revision)))
      throw new Error("The discovery digest could not be read.");
    discovery = {
      digest: d.digest as string,
      credential_revision: d.credential_revision as string,
      tools: tools(d.tools),
    };
  }
  if (source.review != null) {
    const r = record(source.review);
    if (
      !discovery ||
      r.digest !== discovery.digest ||
      !Array.isArray(r.tools) ||
      !r.tools.length ||
      new Set(r.tools).size !== r.tools.length ||
      r.tools.some(
        (name) =>
          typeof name !== "string" || !Object.hasOwn(discovery!.tools, name),
      )
    )
      throw new Error("The schema review does not match current discovery.");
    review = { digest: r.digest as string, tools: r.tools as string[] };
  }
  return {
    id: source.id as string,
    name: text(source.name),
    endpoint: source.endpoint == null ? null : text(source.endpoint, 2048),
    prerequisite:
      source.prerequisite == null ? null : text(source.prerequisite, 2000),
    enabled: source.enabled,
    credential_configured: source.credential_configured,
    readiness: source.readiness as string,
    discovery,
    review,
  };
}
export function readCatalog(value: unknown): ToolConnector[] {
  if (!Array.isArray(value) || value.length !== 5)
    throw new Error(
      "The external tool catalog could not be read. Reload before making changes.",
    );
  const entries = value.map(readConnector);
  if (new Set(entries.map((item) => item.id)).size !== entries.length)
    throw new Error("The catalog contains duplicate connections.");
  return entries;
}
export function readProjects(value: unknown): ToolProject[] {
  if (!Array.isArray(value) || value.length > 1000)
    throw new Error("Project choices could not be read.");
  return value.map((item) => {
    const p = record(item),
      d = record(p.definition);
    if (!id.test(String(p.id)) || !Array.isArray(d.components))
      throw new Error("Project repository bindings could not be read.");
    return {
      id: p.id as string,
      definition: {
        name: text(d.name),
        components: d.components.map((value) => {
          const c = record(value);
          if (!id.test(String(c.repository_id)))
            throw new Error("Project repository binding is invalid.");
          return { repository_id: c.repository_id as string };
        }),
      },
    };
  });
}
export function readRepositories(value: unknown): ToolRepository[] {
  if (!Array.isArray(value) || value.length > 1000)
    throw new Error("Repository choices could not be read.");
  return value.map((item) => {
    const r = record(item);
    if (!id.test(String(r.id)))
      throw new Error("Repository identity could not be read.");
    return { id: r.id as string, name: text(r.name), url: text(r.url, 2048) };
  });
}
export function readGrants(value: unknown, project: string): ToolGrant[] {
  if (!Array.isArray(value) || value.length > 1000)
    throw new Error("Project grants could not be read.");
  return value.map((item) => {
    const g = record(item),
      scope = record(g.scope);
    if (
      !id.test(String(g.id)) ||
      !id.test(String(g.credential_revision)) ||
      !hash.test(String(g.review_digest)) ||
      !id.test(String(g.repository_id)) ||
      g.project_id !== project ||
      g.connector !== "github" ||
      typeof g.active !== "boolean" ||
      !Array.isArray(g.roles) ||
      !g.roles.length ||
      g.roles.some((role) => !["implementer", "docs"].includes(role)) ||
      !Array.isArray(g.stages) ||
      g.stages.some((stage) => stage !== "implement")
    )
      throw new Error(
        "The server returned unsupported project grant bindings.",
      );
    return {
      id: g.id as string,
      active: g.active,
      project_id: project,
      repository_id: g.repository_id as string,
      connector: "github",
      credential_revision: g.credential_revision as string,
      review_digest: g.review_digest as string,
      tools: tools(g.tools),
      roles: g.roles as ToolRole[],
      stages: g.stages as string[],
      scope: { owner: text(scope.owner, 100), repo: text(scope.repo, 100) },
    };
  });
}
export function connectionPayload(
  current: ToolConnector,
  enabled: boolean,
  draftKey: string,
  remove: boolean,
) {
  const key = draftKey.trim();
  if (remove && (key || enabled))
    throw new Error("Remove the key separately; disable the connection first.");
  if (key.length > 4096 || /\s/.test(key))
    throw new Error(
      "Enter one provider key without spaces, using at most 4,096 characters.",
    );
  if (enabled && !key && !current.credential_configured)
    throw new Error(
      "Enter a private provider key before enabling this connection.",
    );
  return {
    enabled,
    ...(key ? { api_key: key } : {}),
    ...(remove ? { clear_secret: true } : {}),
  };
}
export function githubRepository(repository: ToolRepository) {
  try {
    const raw = repository.url.match(/^https:\/\/([^/?#]+)([^?#]*)/i);
    if (!raw || raw[1] !== "github.com") return false;
    const u = new URL(repository.url),
      parts = raw[2]
        .replace(/\.git$/, "")
        .replace(/^\/+|\/+$/g, "")
        .split("/");
    return (
      u.protocol === "https:" &&
      !u.username &&
      !u.password &&
      !u.search &&
      !u.hash &&
      parts.length === 2 &&
      parts.every(
        (p) => /^[A-Za-z0-9_.-]{1,100}$/.test(p) && ![".", ".."].includes(p),
      )
    );
  } catch {
    return false;
  }
}
export function grantPayload(
  current: ToolConnector,
  project: ToolProject,
  repository: ToolRepository,
  selected: string[],
  roles: ToolRole[],
  grants: ToolGrant[],
) {
  if (
    !current.enabled ||
    !current.review ||
    !selected.length ||
    new Set(selected).size !== selected.length ||
    selected.some((name) => !current.review!.tools.includes(name))
  )
    throw new Error(
      "Choose tools from the enabled, exact reviewed connection.",
    );
  if (
    !roles.length ||
    roles.some((role) => !["implementer", "docs"].includes(role))
  )
    throw new Error("Choose at least one supported execution role.");
  if (
    !project.definition.components.some(
      (item) => item.repository_id === repository.id,
    )
  )
    throw new Error("Choose a repository belonging to this project.");
  if (!githubRepository(repository))
    throw new Error("Choose a registered HTTPS GitHub repository.");
  if (
    grants.some(
      (g) =>
        g.active &&
        g.credential_revision === current.discovery?.credential_revision &&
        g.repository_id === repository.id &&
        selected.some((name) => Object.hasOwn(g.tools, name)),
    )
  )
    throw new Error(
      "Revoke the overlapping active grant, then create one grant with all intended roles. Separate grants for the same tool can make execution ambiguous.",
    );
  return {
    repository_id: repository.id,
    review_digest: current.review.digest,
    tools: selected,
    roles,
    stages: ["implement"],
  };
}
export function toolFields(schema: RecordData) {
  const properties =
    schema.properties &&
    typeof schema.properties === "object" &&
    !Array.isArray(schema.properties)
      ? (schema.properties as RecordData)
      : {};
  return Object.entries(properties)
    .slice(0, 64)
    .map(([name, value]) => {
      const property =
        value && typeof value === "object" && !Array.isArray(value)
          ? (value as RecordData)
          : {};
      return {
        name: name.length > 120 ? name.slice(0, 120) + "…" : name,
        type:
          typeof property.type === "string"
            ? property.type.slice(0, 40)
            : "JSON value",
        required:
          Array.isArray(schema.required) && schema.required.includes(name),
      };
    });
}
