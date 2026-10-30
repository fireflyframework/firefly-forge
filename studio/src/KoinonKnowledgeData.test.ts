/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  koinonConfiguration,
  type KoinonSettings,
} from "./KoinonKnowledgeData";
const repository = "a".repeat(32);
const current = {
  provider: "koinon",
  version: 2,
  enabled: true,
  required: true,
  auto_memory: true,
  endpoint: "https://koinon.example/api/v1",
  tenant_id: "tenant",
  top_k: 10,
  ca_certificate: "",
  access_token_configured: true,
  project_scopes: {
    ["b".repeat(32)]: {
      workspace_id: "other",
      project_id: "other-project",
      collection_id: null,
      dataset_ids: [],
    },
  },
  framework_scope: null,
  readiness: { state: "configured", detail: "Check mapped access" },
} as KoinonSettings;
function form() {
  const value = new FormData();
  for (const [key, item] of Object.entries({
    enabled: "on",
    required: "on",
    endpoint: current.endpoint,
    tenant_id: "tenant",
    repository_id: repository,
    workspace_id: "workspace",
    project_id: "project",
    collection_id: "collection",
    dataset_ids: "dataset-1, dataset-2",
    top_k: "25",
    access_token: "",
  }))
    value.set(key, item);
  return value;
}
it("saves explicit catalog identities and preserves other repository mappings", () => {
  const request = koinonConfiguration(form(), current);
  expect(request.settings.project_scopes[repository]).toEqual({
    workspace_id: "workspace",
    project_id: "project",
    collection_id: "collection",
    dataset_ids: ["dataset-1", "dataset-2"],
  });
  expect(request.settings.project_scopes["b".repeat(32)]).toEqual(
    current.project_scopes["b".repeat(32)],
  );
  expect(request.settings.auto_memory).toBe(true);
  expect(request.settings.access_token).toBe("");
  expect(request.clear_access_token).toBe(false);
  expect(request.settings).not.toHaveProperty("api_key");
});
it("refuses an incomplete mapping or a repeated dataset rather than guessing scope", () => {
  const value = form();
  value.delete("workspace_id");
  expect(() => koinonConfiguration(value, current)).toThrow("workspace");
  value.set("workspace_id", "workspace");
  value.set("dataset_ids", "dataset, dataset");
  expect(() => koinonConfiguration(value, current)).toThrow("unique");
});
it("clears a stored machine token only by explicit choice", () => {
  const value = form();
  value.set("clear_access_token", "on");
  expect(koinonConfiguration(value, current).clear_access_token).toBe(true);
});
