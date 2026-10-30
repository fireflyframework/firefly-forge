/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { KoinonKnowledgeSettings } from "./KoinonKnowledgeSettings";
import type { KoinonSettings } from "./KoinonKnowledgeData";
const settings = {
  provider: "koinon",
  version: 2,
  enabled: true,
  required: true,
  auto_memory: true,
  endpoint: "https://knowledge.example/api/v1",
  tenant_id: "tenant",
  top_k: 25,
  ca_certificate: "",
  access_token_configured: true,
  project_scopes: {},
  framework_scope: null,
  readiness: { state: "configured", detail: "Check mapped catalog access" },
} as KoinonSettings;
it("offers explicit native scope mapping and keeps stored tokens out of the document", () => {
  const html = renderToStaticMarkup(
    <KoinonKnowledgeSettings
      settings={settings}
      connection={{
        base: "https://forge.example",
        token: "private-factory-token",
      }}
      repositories={[]}
      onSaved={() => undefined}
    />,
  );
  for (const label of [
    "koinon knowledge connection",
    "Workspace catalog ID",
    "Project catalog ID",
    "Dataset catalog IDs",
    "Stored; leave blank to keep",
    "Check mapped access",
  ])
    expect(html).toContain(label);
  expect(html).not.toContain("private-factory-token");
  expect(html).not.toContain("Platform API key");
  expect(html).toContain('type="password"');
  expect(html).toContain(
    "Source ingestion, memory publication, framework refresh",
  );
  expect(html).toContain("Choose a repository");
});
