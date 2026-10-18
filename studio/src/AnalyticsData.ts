/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { ApiError, request, type Connection } from "./api";

export type AnalyticsSettings = {
  enabled: boolean;
  endpoint: string;
  tenant_id: string;
  workspace_id: string;
  dataset_id: string;
};
export type AnalyticsDraft = AnalyticsSettings & { agent_token: string };
export type AnalyticsConfiguration = {
  project_id: string;
  revision: number;
  status: "configured" | "disabled";
  agent_token_configured: boolean;
  configuration: AnalyticsSettings | null;
};
export type AnalyticsScope = {
  project_id: string;
  configuration_revision: number;
  tenant_id: string;
  workspace_id: string;
  dataset_id: string;
};
export type AnalyticsObservation<T> = AnalyticsScope & {
  observed_at: string;
  response_digest: string;
  snapshot_assurance: "reported_identity_only";
  data: T;
};
export type AnalyticsTable = {
  id: string;
  name: string;
  qualified_name: string;
  description: string | null;
  current_snapshot_id: string | null;
  n_columns: number | null;
};
export type AnalyticsObject = {
  id: string;
  table_id: string;
  snapshot_id: string;
  qualified_name: string;
  kind: string;
  description: string | null;
  data_type: string | null;
  is_nullable: boolean | null;
};
export type AnalyticsCatalog = AnalyticsObservation<{
  items: AnalyticsTable[];
}>;
const unsupported = () =>
  new Error("Analytics response is unsupported or outside the selected scope.");
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw unsupported();
  return value as Record<string, unknown>;
}
function text(value: unknown, max = 2000): string {
  if (typeof value !== "string" || value.length > max) throw unsupported();
  return value;
}
function nullableText(value: unknown): string | null {
  return value == null ? null : text(value);
}
function integer(value: unknown, min: number, max: number): number {
  if (
    !Number.isInteger(value) ||
    (value as number) < min ||
    (value as number) > max
  )
    throw unsupported();
  return value as number;
}
function projectId(value: string): string {
  if (!/^[a-f0-9]{32}$/.test(value))
    throw new Error("Choose an existing project.");
  return value;
}
export function analyticsUuid(value: unknown): string {
  if (
    typeof value !== "string" ||
    !/^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(
      value,
    )
  )
    throw new Error("Enter an exact UUID.");
  const compact = value.replaceAll("-", "").toLowerCase();
  return `${compact.slice(0, 8)}-${compact.slice(8, 12)}-${compact.slice(12, 16)}-${compact.slice(16, 20)}-${compact.slice(20)}`;
}
function settings(value: unknown): AnalyticsSettings {
  const r = record(value);
  if (typeof r.enabled !== "boolean") throw unsupported();
  const endpoint = text(r.endpoint, 2048).trim();
  try {
    const url = new URL(endpoint);
    if (
      url.protocol !== "https:" ||
      !url.hostname ||
      url.username ||
      url.password ||
      url.port ||
      url.pathname !== "/" ||
      url.search ||
      url.hash ||
      /\s/.test(endpoint)
    )
      throw Error();
  } catch {
    throw new Error(
      "Use an HTTPS origin on port 443 without credentials, path, query or fragment.",
    );
  }
  const tenant = text(r.tenant_id, 128);
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(tenant))
    throw new Error(
      "Enter a tenant identifier using letters, numbers, dot, underscore, colon or dash.",
    );
  return {
    enabled: r.enabled,
    endpoint: endpoint.replace(/\/$/, ""),
    tenant_id: tenant,
    workspace_id: analyticsUuid(r.workspace_id),
    dataset_id: analyticsUuid(r.dataset_id),
  };
}
export function analyticsConfiguration(
  value: unknown,
  project: string,
): AnalyticsConfiguration {
  const r = record(value),
    capabilities = record(r.capabilities);
  if (
    r.project_id !== projectId(project) ||
    typeof r.agent_token_configured !== "boolean" ||
    !["configured", "disabled"].includes(String(r.status)) ||
    ["agent_token", "secret_revision", "secret_digest"].some((key) =>
      Object.hasOwn(r, key),
    ) ||
    capabilities.catalog !== true ||
    capabilities.table !== true ||
    capabilities.exact_schema_object !== true ||
    capabilities.full_table_schema !== false ||
    capabilities.prepared_results !== false ||
    capabilities.query_execution !== false
  )
    throw unsupported();
  const revision = integer(r.revision, 0, 33),
    c = record(r.configuration);
  if (
    ["agent_token", "secret_revision", "secret_digest"].some((key) =>
      Object.hasOwn(c, key),
    )
  )
    throw unsupported();
  const configured = revision > 0 ? settings(c) : null;
  if (
    r.agent_token_configured !== revision > 0 ||
    r.status !== (configured?.enabled ? "configured" : "disabled") ||
    (!revision && Object.keys(c).length) ||
    (revision === 33 && configured?.enabled)
  )
    throw unsupported();
  return {
    project_id: project,
    revision,
    status: r.status as AnalyticsConfiguration["status"],
    agent_token_configured: r.agent_token_configured,
    configuration: configured,
  };
}
export function analyticsPayload(
  current: AnalyticsConfiguration,
  draft: AnalyticsDraft,
) {
  if (
    current.revision >= 33 ||
    (current.revision === 32 && !current.configuration?.enabled)
  )
    throw new Error(
      "The connection is disabled at its history limit. Terminal bindings cannot be edited or re-enabled.",
    );
  const scope = settings(draft),
    token = draft.agent_token;
  if (current.revision === 32) {
    if (scope.enabled)
      throw new Error(
        "Only disable is available at the configuration history limit.",
      );
    if (
      (["endpoint", "tenant_id", "workspace_id", "dataset_id"] as const).some(
        (key) => scope[key] !== current.configuration?.[key],
      )
    )
      throw new Error(
        "Keep the existing scope unchanged for the terminal disable.",
      );
  }
  if (!/^[!-~]{1,4096}$/.test(token))
    throw new Error(
      "Enter a nonempty scoped agent token without spaces or newlines for every save.",
    );
  return { ...scope, agent_token: token, expected_revision: current.revision };
}
function envelope(
  value: unknown,
  project: string,
): AnalyticsObservation<unknown> {
  const r = record(value);
  if (
    r.project_id !== projectId(project) ||
    r.snapshot_assurance !== "reported_identity_only" ||
    !/^[a-f0-9]{64}$/.test(String(r.response_digest)) ||
    new TextEncoder().encode(JSON.stringify(r)).length > 1048576
  )
    throw unsupported();
  const observed = text(r.observed_at, 80),
    tenant = text(r.tenant_id, 128);
  if (
    !Number.isFinite(Date.parse(observed)) ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(tenant)
  )
    throw unsupported();
  return {
    project_id: project,
    configuration_revision: integer(r.configuration_revision, 1, 32),
    tenant_id: tenant,
    workspace_id: analyticsUuid(r.workspace_id),
    dataset_id: analyticsUuid(r.dataset_id),
    observed_at: observed,
    response_digest: r.response_digest as string,
    snapshot_assurance: "reported_identity_only",
    data: r.data,
  };
}
function sameScope(actual: AnalyticsScope, expected: AnalyticsScope) {
  for (const key of [
    "project_id",
    "configuration_revision",
    "tenant_id",
    "workspace_id",
    "dataset_id",
  ] as const)
    if (actual[key] !== expected[key])
      throw new Error(
        "Analytics scope or revision changed. Read the catalog again before continuing.",
      );
}
function tableRecord(value: unknown, scope: AnalyticsScope): AnalyticsTable {
  const r = record(value);
  if (
    r.tenant_id !== scope.tenant_id ||
    analyticsUuid(r.workspace_id) !== scope.workspace_id ||
    analyticsUuid(r.dataset_id) !== scope.dataset_id
  )
    throw unsupported();
  return {
    id: analyticsUuid(r.id),
    name: text(r.name, 512),
    qualified_name: text(r.qualified_name, 1000),
    description: nullableText(r.description),
    current_snapshot_id:
      r.current_snapshot_id == null
        ? null
        : analyticsUuid(r.current_snapshot_id),
    n_columns: r.n_columns == null ? null : integer(r.n_columns, 0, 1000000),
  };
}
export function catalogObservation(
  value: unknown,
  project: string,
): AnalyticsCatalog {
  const e = envelope(value, project),
    data = record(e.data);
  if (
    !Array.isArray(data.items) ||
    data.items.length > 50 ||
    data.total !== data.items.length ||
    data.offset !== 0 ||
    data.has_more === true
  )
    throw unsupported();
  const items = data.items.map((item) => tableRecord(item, e));
  if (new Set(items.map((item) => item.id)).size !== items.length)
    throw unsupported();
  return { ...e, data: { items } };
}
const path = (project: string) => `/projects/${projectId(project)}/analytics`;
function boundedSignal(signal?: AbortSignal): AbortSignal {
  const deadline = AbortSignal.timeout(15000);
  return signal ? AbortSignal.any([signal, deadline]) : deadline;
}
export async function loadAnalyticsConfiguration(
  connection: Connection,
  project: string,
  signal?: AbortSignal,
) {
  return analyticsConfiguration(
    await request(connection, path(project) + "/configuration", {
      signal: boundedSignal(signal),
    }),
    project,
  );
}
export async function saveAnalyticsConfiguration(
  connection: Connection,
  project: string,
  current: AnalyticsConfiguration,
  draft: AnalyticsDraft,
) {
  const payload = analyticsPayload(current, draft);
  try {
    const result = analyticsConfiguration(
      await request(connection, path(project) + "/configuration", {
        method: "PUT",
        body: JSON.stringify(payload),
        signal: boundedSignal(),
      }),
      project,
    );
    if (
      result.revision !== current.revision + 1 ||
      !result.configuration ||
      (
        Object.keys(result.configuration) as Array<keyof AnalyticsSettings>
      ).some((key) => result.configuration![key] !== payload[key])
    )
      throw unsupported();
    return result;
  } catch {
    throw new Error(
      "Save not confirmed. Reload the saved configuration before another save; an earlier save may still settle.",
    );
  }
}
export async function loadAnalyticsCatalog(
  connection: Connection,
  project: string,
  signal?: AbortSignal,
) {
  return catalogObservation(
    await request(connection, path(project) + "/catalog", {
      signal: boundedSignal(signal),
    }),
    project,
  );
}
export async function loadAnalyticsTable(
  connection: Connection,
  project: string,
  table: string,
  expected: AnalyticsScope,
  signal?: AbortSignal,
) {
  const id = analyticsUuid(table),
    e = envelope(
      await request(connection, path(project) + "/tables/" + id, {
        signal: boundedSignal(signal),
      }),
      project,
    );
  sameScope(e, expected);
  const data = tableRecord(e.data, e);
  if (data.id !== id) throw unsupported();
  return { ...e, data };
}
export async function loadAnalyticsObject(
  connection: Connection,
  project: string,
  table: AnalyticsTable,
  object: string,
  expected: AnalyticsScope,
  signal?: AbortSignal,
) {
  const id = analyticsUuid(object),
    parent = analyticsUuid(table.id);
  if (!table.current_snapshot_id)
    throw new Error(
      "Read a table with a reported current snapshot before looking up a schema object.",
    );
  const e = envelope(
    await request(
      connection,
      path(project) + `/tables/${parent}/objects/${id}`,
      { signal: boundedSignal(signal) },
    ),
    project,
  );
  sameScope(e, expected);
  const r = record(e.data);
  if (
    analyticsUuid(r.id) !== id ||
    analyticsUuid(r.table_id) !== parent ||
    analyticsUuid(r.snapshot_id) !== table.current_snapshot_id ||
    r.tenant_id !== e.tenant_id ||
    analyticsUuid(r.workspace_id) !== e.workspace_id ||
    (r.is_nullable !== null && typeof r.is_nullable !== "boolean")
  )
    throw unsupported();
  const data: AnalyticsObject = {
    id,
    table_id: parent,
    snapshot_id: table.current_snapshot_id,
    qualified_name: text(r.qualified_name, 1000),
    kind: text(r.kind, 100),
    description: nullableText(r.description),
    data_type: nullableText(r.data_type),
    is_nullable: r.is_nullable as boolean | null,
  };
  return { ...e, data };
}
export function analyticsReadError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401)
    return "Your session is unavailable. Sign in again to read analytics.";
  if (error instanceof ApiError && error.status === 403)
    return "Current project access does not allow this read. Ask the administrator to review access.";
  return "Analytics could not be read. Check the saved scope, current access and Flyquery prerequisites, then choose a read again. No query or retry was started.";
}
