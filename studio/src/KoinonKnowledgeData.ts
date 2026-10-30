/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type KoinonScope = {
  workspace_id: string;
  project_id: string;
  collection_id: string | null;
  dataset_ids: string[];
};
export type KoinonSettings = {
  provider: "koinon";
  version: 2;
  enabled: boolean;
  required: boolean;
  auto_memory: boolean;
  endpoint: string;
  tenant_id: string;
  access_token_configured: boolean;
  ca_certificate: string;
  project_scopes: Record<string, KoinonScope>;
  framework_scope: KoinonScope | null;
  top_k: number;
  readiness: { state: string; detail: string };
  framework_workspace?: undefined;
  api_key_configured?: false;
  agent_token_configured?: false;
};
const identifier = /^[A-Za-z0-9_-]{1,64}$/;
function text(form: FormData, key: string) {
  return String(form.get(key) ?? "").trim();
}
export function koinonConfiguration(form: FormData, current: KoinonSettings) {
  const repository = text(form, "repository_id");
  if (!/^[a-f0-9]{32}$/.test(repository))
    throw new Error("Choose the repository for this mapping.");
  const workspace = text(form, "workspace_id"),
    project = text(form, "project_id"),
    collection = text(form, "collection_id");
  if (!identifier.test(workspace) || !identifier.test(project))
    throw new Error(
      "Enter the exact Koinon workspace and project catalog IDs.",
    );
  if (collection && !identifier.test(collection))
    throw new Error("Enter the exact Koinon collection catalog ID.");
  const datasets = text(form, "dataset_ids")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (
    new Set(datasets).size !== datasets.length ||
    datasets.some((value) => !identifier.test(value))
  )
    throw new Error("Enter unique dataset catalog IDs separated by commas.");
  const topK = Number(text(form, "top_k"));
  if (!Number.isInteger(topK) || topK < 1 || topK > 100)
    throw new Error(
      "Choose between 1 and 100 results, as supported by Koinon search.",
    );
  const frameworkWorkspace = text(form, "framework_workspace_id"),
    frameworkProject = text(form, "framework_project_id"),
    frameworkCollection = text(form, "framework_collection_id");
  let framework = current.framework_scope;
  if (form.has("clear_framework_scope")) framework = null;
  else if (frameworkWorkspace || frameworkProject || frameworkCollection) {
    if (
      !identifier.test(frameworkWorkspace) ||
      !identifier.test(frameworkProject) ||
      (frameworkCollection && !identifier.test(frameworkCollection))
    )
      throw new Error(
        "Supply both shared framework workspace and project IDs.",
      );
    framework = {
      workspace_id: frameworkWorkspace,
      project_id: frameworkProject,
      collection_id: frameworkCollection || null,
      dataset_ids: [],
    };
  }
  return {
    settings: {
      provider: "koinon" as const,
      version: 2 as const,
      enabled: form.has("enabled"),
      required: form.has("required"),
      auto_memory: current.auto_memory,
      endpoint: text(form, "endpoint"),
      tenant_id: text(form, "tenant_id"),
      access_token: String(form.get("access_token") ?? ""),
      ca_certificate: form.has("ca_certificate")
        ? text(form, "ca_certificate")
        : current.ca_certificate,
      project_scopes: {
        ...current.project_scopes,
        [repository]: {
          workspace_id: workspace,
          project_id: project,
          collection_id: collection || null,
          dataset_ids: datasets,
        },
      },
      framework_scope: framework,
      top_k: topK,
    },
    clear_access_token: form.has("clear_access_token"),
  };
}
