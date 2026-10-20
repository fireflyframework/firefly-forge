/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import {
  Analytics,
  AnalyticsExplorer,
  AnalyticsWizard,
  ScopeDetails,
} from "./Analytics";
import type {
  AnalyticsCatalog,
  AnalyticsConfiguration,
  AnalyticsDraft,
  AnalyticsTable,
} from "./AnalyticsData";

const workspace = "00000000-0000-0000-0000-000000000002";
const dataset = "00000000-0000-0000-0000-000000000003";
const scope = {
  enabled: true,
  endpoint: "https://analytics.example",
  tenant_id: "fictional",
  workspace_id: workspace,
  dataset_id: dataset,
};
const current: AnalyticsConfiguration = {
  project_id: "a".repeat(32),
  revision: 7,
  status: "configured",
  agent_token_configured: true,
  configuration: scope,
};
const draft: AnalyticsDraft = {
  ...scope,
  agent_token: "transient-token-must-not-appear-in-review",
};
const noop = () => {};
const wizard = {
  current,
  draft,
  onChange: noop,
  step: 0,
  onStep: noop,
  reviewed: true,
  onReviewed: noop,
  busy: false,
  onCancel: noop,
  onSubmit: noop,
};
const table: AnalyticsTable = {
  id: "00000000-0000-0000-0000-000000000004",
  name: "<script>untrusted()</script>",
  qualified_name: "warehouse.stock",
  description: "Fictional stock",
  n_columns: 2,
  current_snapshot_id: null,
};
const catalog: AnalyticsCatalog = {
  project_id: current.project_id,
  configuration_revision: 7,
  tenant_id: scope.tenant_id,
  workspace_id: workspace,
  dataset_id: dataset,
  observed_at: "2026-10-17T00:00:00Z",
  response_digest: "f".repeat(64),
  snapshot_assurance: "reported_identity_only",
  data: { items: [table] },
};
const explorer = {
  catalog: null,
  table: null,
  object: null,
  objectId: "",
  onObjectId: noop,
  busy: false,
  disabled: false,
  onCatalog: noop,
  onTable: noop,
  onObject: noop,
};

it("requires sign-in before presenting project analytics controls", () => {
  const html = renderToStaticMarkup(
    <Analytics connection={{ base: "/api/v1", token: "" }} session={null} />,
  );
  expect(html).toContain("Sign in to read project analytics");
  expect(html).not.toContain("Read catalog");
  expect(html).not.toContain("Set up Flyquery");
});

it("states finite prerequisites without claiming they test the connection", () => {
  const html = renderToStaticMarkup(<AnalyticsWizard {...wizard} />);
  expect(html).toContain("Flyquery 26.10.4");
  expect(html).toContain("flyquery.schema:read");
  expect(html).toContain("factory administrator");
  expect(html).toContain("does not test the remote service");
  expect(html).toContain("port 443");
  expect(html).toContain("50 tables");
  expect(html).not.toContain(draft.agent_token);
});

it("uses a password field only while editing a replacement token and never preloads a saved secret", () => {
  const html = renderToStaticMarkup(
    <AnalyticsWizard
      {...wizard}
      draft={{ ...draft, agent_token: "" }}
      step={1}
    />,
  );
  expect(html).toMatch(
    /id="analytics-token" type="password" autoComplete="new-password"[^>]*value=""/,
  );
  expect(html).toContain("A token is saved; its value is never returned.");
  expect(html).toContain("including disabling");
  expect(html).not.toContain(draft.agent_token);
});

it("reviews the exact scope and CAS revision without serializing the transient token", () => {
  const html = renderToStaticMarkup(<AnalyticsWizard {...wizard} step={2} />);
  expect(html).toContain(workspace);
  expect(html).toContain(dataset);
  expect(html).toContain("Expected saved revision: 7");
  expect(html).toContain("Replacement entered; value hidden");
  expect(html).toContain(
    "Saving does not create a dataset or verify a connection",
  );
  expect(html).not.toContain(draft.agent_token);
  expect(html).not.toContain('id="analytics-token"');
});

it("blocks every wizard action after an unconfirmed save without pretending a request is still running", () => {
  const html = renderToStaticMarkup(
    <AnalyticsWizard
      {...wizard}
      step={2}
      blocked
      draft={{ ...draft, agent_token: "" }}
    />,
  );
  const buttons = html.match(/<button\b[^>]*>/g) ?? [];
  expect(buttons.length).toBeGreaterThan(3);
  expect(buttons.every((button) => button.includes("disabled="))).toBe(true);
  expect(html).not.toContain("Saving…");
  expect(html).not.toContain("Replacement entered");
  expect(html).toContain("Cleared after submission");
});

it("keeps catalog reads explicit and distinguishes an unread scope from an empty catalog", () => {
  const unread = renderToStaticMarkup(<AnalyticsExplorer {...explorer} />);
  expect(unread).toContain("No catalog read yet");
  expect(unread).toContain("Read catalog");
  expect(unread).toContain("does not poll or retry automatically");
  const empty = renderToStaticMarkup(
    <AnalyticsExplorer
      {...explorer}
      catalog={{ ...catalog, data: { items: [] } }}
    />,
  );
  expect(empty).toContain("0 tables reported");
  expect(empty).toContain("No tables returned");
  expect(empty).not.toContain("No catalog read yet");
});

it("escapes reported names and refuses exact-object controls without a current parent snapshot", () => {
  const html = renderToStaticMarkup(
    <AnalyticsExplorer
      {...explorer}
      catalog={catalog}
      table={{ ...catalog, data: table }}
      objectId={dataset}
    />,
  );
  expect(html).not.toContain("<script>");
  expect(html).toContain("&lt;script&gt;");
  expect(html).toContain("Not recorded");
  expect(html).toContain("Full field discovery is unavailable");
  expect(html).toMatch(/id="analytics-object"[^>]*disabled=""/);
  expect(html).toMatch(
    /<button[^>]*type="submit" disabled="">Read schema object/,
  );
  expect(html).toContain("does not prove the underlying data content");
  expect(html).not.toContain("Run SQL");
});

it("retains complete scope IDs and response evidence in a wrapping disclosure", () => {
  const html = renderToStaticMarkup(
    <AnalyticsExplorer {...explorer} catalog={catalog} />,
  );
  expect(html).toContain(
    "<summary>Observed scope and response digest</summary>",
  );
  expect(html).toContain(catalog.response_digest);
  expect(html).toContain("Configuration revision");
  expect(renderToStaticMarkup(<ScopeDetails scope={scope} />)).toContain(
    `<dd>${workspace}</dd>`,
  );
});

it("keeps the table read's own digest available alongside the earlier catalog observation", () => {
  const digest = "e".repeat(64);
  const html = renderToStaticMarkup(
    <AnalyticsExplorer
      {...explorer}
      catalog={catalog}
      table={{ ...catalog, response_digest: digest, data: table }}
    />,
  );
  expect(html).toContain(catalog.response_digest);
  expect(html).toContain(digest);
});

it("presents only an explicit terminal disable at32, with locked scope and an editable fresh token", () => {
  const current32 = { ...current, revision: 32 };
  const disabledDraft = { ...draft, enabled: false };
  const first = renderToStaticMarkup(
    <AnalyticsWizard
      {...wizard}
      current={current32}
      draft={disabledDraft}
      reviewed={false}
    />,
  );
  expect(first).toContain("terminal revision 33");
  expect(first).toContain("Disabling does not contact Flyquery");
  expect(first).not.toMatch(/type="checkbox"[^>]*disabled=""/);
  const fields = renderToStaticMarkup(
    <AnalyticsWizard
      {...wizard}
      current={current32}
      draft={disabledDraft}
      step={1}
    />,
  );
  for (const name of ["endpoint", "tenant", "workspace", "dataset"])
    expect(fields).toMatch(
      new RegExp(`id="analytics-${name}"[^>]*disabled=""`),
    );
  expect(fields).not.toMatch(/id="analytics-token"[^>]*disabled=""/);
  expect(fields).toMatch(/type="checkbox"[^>]*disabled=""/);
  const review = renderToStaticMarkup(
    <AnalyticsWizard
      {...wizard}
      current={current32}
      draft={disabledDraft}
      step={2}
    />,
  );
  expect(review).toContain("Review terminal disable");
  expect(review).toContain("Disable connection");
  expect(review).not.toContain("Save connection");
});
