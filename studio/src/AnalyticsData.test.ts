/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import {
  analyticsConfiguration,
  analyticsPayload,
  catalogObservation,
  loadAnalyticsCatalog,
  loadAnalyticsObject,
  loadAnalyticsTable,
  saveAnalyticsConfiguration,
} from "./AnalyticsData";

const project = "a".repeat(32);
const workspace = "00000000-0000-0000-0000-000000000002";
const dataset = "00000000-0000-0000-0000-000000000003";
const table = "00000000-0000-0000-0000-000000000004";
const object = "00000000-0000-0000-0000-000000000005";
const connection = { base: "/api/v1", token: "operator" };
const scope = {
  endpoint: "https://analytics.example",
  tenant_id: "fictional",
  workspace_id: workspace,
  dataset_id: dataset,
  enabled: true,
};
const capabilities = {
  catalog: true,
  table: true,
  exact_schema_object: true,
  full_table_schema: false,
  prepared_results: false,
  query_execution: false,
};
const settings = {
  project_id: project,
  revision: 1,
  status: "configured",
  agent_token_configured: true,
  configuration: scope,
  capabilities,
};
const tableRecord = {
  id: table,
  tenant_id: "fictional",
  workspace_id: workspace,
  dataset_id: dataset,
  name: "Stock",
  qualified_name: "Stock",
  kind: "table",
  is_active: true,
  n_columns: 2,
  description: "Fictional stock",
  current_snapshot_id: null,
};
const observation = {
  project_id: project,
  configuration_revision: 1,
  tenant_id: "fictional",
  workspace_id: workspace,
  dataset_id: dataset,
  observed_at: "2026-10-17T00:00:00Z",
  response_digest: "b".repeat(64),
  snapshot_assurance: "reported_identity_only",
  data: { items: [tableRecord], total: 1, offset: 0, has_more: false },
};
afterEach(() => vi.unstubAllGlobals());

it("saves one exact full scope under the observed revision and never reuses a hidden token", () => {
  const saved = analyticsConfiguration(settings, project);
  expect(
    analyticsPayload(saved, { ...scope, agent_token: "fresh-scoped-token" }),
  ).toEqual({
    ...scope,
    agent_token: "fresh-scoped-token",
    expected_revision: 1,
  });
  expect(() => analyticsPayload(saved, { ...scope, agent_token: "" })).toThrow(
    /token/i,
  );
  expect(() =>
    analyticsPayload(saved, { ...scope, enabled: false, agent_token: "" }),
  ).toThrow(/token/i);
  expect(
    analyticsPayload(saved, {
      ...scope,
      enabled: false,
      agent_token: "fresh-scoped-token",
    }).enabled,
  ).toBe(false);
});

for (const endpoint of [
  "http://analytics.example",
  "https://user:secret@analytics.example",
  "https://analytics.example:8443",
  "https://analytics.example/path",
  "https://analytics.example?tenant=other",
])
  it(`refuses unsupported endpoint ${endpoint} before any save`, () => {
    expect(() =>
      analyticsPayload(analyticsConfiguration(settings, project), {
        ...scope,
        endpoint,
        agent_token: "fresh-scoped-token",
      }),
    ).toThrow(/HTTPS/);
  });

it("refuses foreign, incomplete or duplicate catalogs rather than displaying a partial result", () => {
  expect(catalogObservation(observation, project).data.items[0].id).toBe(table);
  for (const data of [
    { ...observation.data, total: 2 },
    { ...observation.data, offset: 1 },
    { ...observation.data, has_more: true },
    { items: [tableRecord] },
    { ...observation.data, items: [{ ...tableRecord, workspace_id: object }] },
    { ...observation.data, items: [tableRecord, tableRecord], total: 2 },
  ])
    expect(() =>
      catalogObservation({ ...observation, data }, project),
    ).toThrow();
  expect(() =>
    catalogObservation({ ...observation, project_id: "f".repeat(32) }, project),
  ).toThrow();
});

it("uses only exact native GET routes and refuses changed scope before exposing table data", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(observation)))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          ...observation,
          configuration_revision: 2,
          data: tableRecord,
        }),
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const page = await loadAnalyticsCatalog(connection, project);
  await expect(
    loadAnalyticsTable(connection, project, table, page),
  ).rejects.toThrow(/changed/i);
  expect(fetch.mock.calls.map(([path]) => path)).toEqual([
    `/api/v1/projects/${project}/analytics/catalog`,
    `/api/v1/projects/${project}/analytics/tables/${table}`,
  ]);
  expect(
    fetch.mock.calls.every(
      ([, options]) => !options.method || options.method === "GET",
    ),
  ).toBe(true);
});

it("does not fetch an object without a known parent snapshot or accept a path as an ID", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const page = catalogObservation(observation, project);
  await expect(
    loadAnalyticsObject(connection, project, tableRecord, object, page),
  ).rejects.toThrow(/snapshot/i);
  await expect(
    loadAnalyticsTable(connection, project, "../queries", page),
  ).rejects.toThrow(/UUID/);
  expect(fetch).not.toHaveBeenCalled();
});

for (const status of [403, 404, 409, 500])
  it(`makes one PUT on ${status}, with no fallback, automatic read, retry or token reflection`, async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "fresh-scoped-token" }), {
        status,
      }),
    );
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveAnalyticsConfiguration(
        connection,
        project,
        analyticsConfiguration(settings, project),
        { ...scope, agent_token: "fresh-scoped-token" },
      ),
    ).rejects.toThrow("Save not confirmed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].method).toBe("PUT");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      ...scope,
      expected_revision: 1,
      agent_token: "fresh-scoped-token",
    });
  });

it("does not retry a lost save or expose its secret in the error", async () => {
  const fetch = vi.fn().mockRejectedValue(new Error("fresh-scoped-token"));
  vi.stubGlobal("fetch", fetch);
  await expect(
    saveAnalyticsConfiguration(
      connection,
      project,
      analyticsConfiguration(settings, project),
      { ...scope, agent_token: "fresh-scoped-token" },
    ),
  ).rejects.not.toThrow("fresh-scoped-token");
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("does not accept secret-bearing public configuration or invented capabilities", () => {
  expect(() =>
    analyticsConfiguration(
      { ...settings, agent_token: "must-never-render" },
      project,
    ),
  ).toThrow();
  expect(() =>
    analyticsConfiguration(
      {
        ...settings,
        capabilities: { ...capabilities, prepared_results: true },
      },
      project,
    ),
  ).toThrow();
});

for (const response of [
  settings,
  { ...settings, revision: 2, configuration: { ...scope, dataset_id: object } },
])
  it("does not call a save confirmed when the returned revision or scope differs", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(response)));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveAnalyticsConfiguration(
        connection,
        project,
        analyticsConfiguration(settings, project),
        { ...scope, agent_token: "fresh-scoped-token" },
      ),
    ).rejects.toThrow("Save not confirmed");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

it("accepts one confirmed disabled revision without an automatic catalog read", async () => {
  const fetch = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        ...settings,
        revision: 2,
        status: "disabled",
        configuration: { ...scope, enabled: false },
      }),
    ),
  );
  vi.stubGlobal("fetch", fetch);
  const result = await saveAnalyticsConfiguration(
    connection,
    project,
    analyticsConfiguration(settings, project),
    { ...scope, enabled: false, agent_token: "replacement-token" },
  );
  expect(result.status).toBe("disabled");
  expect(result.revision).toBe(2);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
});

it("looks up only the exact known object and rejects a changed parent snapshot without fallback", async () => {
  const snapshot = "00000000-0000-0000-0000-000000000006";
  const data = {
    id: object,
    table_id: table,
    snapshot_id: snapshot,
    tenant_id: "fictional",
    workspace_id: workspace,
    qualified_name: "Stock.count",
    kind: "column",
    description: null,
    data_type: "integer",
    is_nullable: false,
  };
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ ...observation, data })),
    )
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          ...observation,
          data: { ...data, snapshot_id: dataset },
        }),
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const page = catalogObservation(observation, project),
    parent = { ...page.data.items[0], current_snapshot_id: snapshot };
  const result = await loadAnalyticsObject(
    connection,
    project,
    parent,
    object,
    page,
  );
  expect(result.data).toEqual({
    id: object,
    table_id: table,
    snapshot_id: snapshot,
    qualified_name: "Stock.count",
    kind: "column",
    description: null,
    data_type: "integer",
    is_nullable: false,
  });
  await expect(
    loadAnalyticsObject(connection, project, parent, object, page),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls.map(([url]) => url)).toEqual(
    Array(2).fill(
      `/api/v1/projects/${project}/analytics/tables/${table}/objects/${object}`,
    ),
  );
});

it("allows only unchanged-scope terminal disable from enabled32 and keeps disabled33 readable", () => {
  const full = analyticsConfiguration({ ...settings, revision: 32 }, project);
  const draft = { ...scope, agent_token: "explicit-terminal-token" };
  expect(() => analyticsPayload(full, draft)).toThrow(/disable/i);
  expect(() =>
    analyticsPayload(full, { ...draft, enabled: false, tenant_id: "other" }),
  ).toThrow(/scope/i);
  expect(analyticsPayload(full, { ...draft, enabled: false })).toEqual({
    ...draft,
    enabled: false,
    expected_revision: 32,
  });
  const terminal = analyticsConfiguration(
    {
      ...settings,
      revision: 33,
      status: "disabled",
      configuration: { ...scope, enabled: false },
    },
    project,
  );
  expect(terminal.revision).toBe(33);
  expect(() => analyticsPayload(terminal, { ...draft, enabled: true })).toThrow(
    /terminal/i,
  );
  expect(() =>
    analyticsPayload(terminal, { ...draft, enabled: false }),
  ).toThrow(/terminal/i);
  expect(() =>
    analyticsConfiguration({ ...settings, revision: 33 }, project),
  ).toThrow();
  const alreadyDisabled = analyticsConfiguration(
    {
      ...settings,
      revision: 32,
      status: "disabled",
      configuration: { ...scope, enabled: false },
    },
    project,
  );
  expect(() =>
    analyticsPayload(alreadyDisabled, { ...draft, enabled: false }),
  ).toThrow();
});

it("confirms terminal33 from one exact disable PUT and never submits another terminal change", async () => {
  const full = analyticsConfiguration({ ...settings, revision: 32 }, project);
  const fetch = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        ...settings,
        revision: 33,
        status: "disabled",
        configuration: { ...scope, enabled: false },
      }),
    ),
  );
  vi.stubGlobal("fetch", fetch);
  const disabled = await saveAnalyticsConfiguration(connection, project, full, {
    ...scope,
    enabled: false,
    agent_token: "explicit-terminal-token",
  });
  expect(disabled.revision).toBe(33);
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
    ...scope,
    enabled: false,
    expected_revision: 32,
    agent_token: "explicit-terminal-token",
  });
  await expect(
    saveAnalyticsConfiguration(connection, project, disabled, {
      ...scope,
      enabled: true,
      agent_token: "explicit-terminal-token",
    }),
  ).rejects.toThrow(/terminal/i);
  expect(fetch).toHaveBeenCalledTimes(1);
});
