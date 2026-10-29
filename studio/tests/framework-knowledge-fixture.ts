/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type {
  FrameworkCorpus,
  FrameworkDiscovery,
  FrameworkSource,
} from "../src/FrameworkKnowledgeData";
export const corpus: FrameworkCorpus = {
  corpus_ref: "a".repeat(32),
  framework: "pyfly",
  framework_version: "26.10.11",
  release_tag: "v26.10.11",
  commit: "b".repeat(40),
  manifest_sha256: "c".repeat(64),
  license_sha256: "d".repeat(64),
  workspace_id: "framework-pyfly-26-10-11",
  state: "ready",
};
export const counts = {
  files: 0,
  total_files: 0,
  documents: 0,
  examples: 0,
  api: 0,
  parts: 0,
  ready_parts: 0,
  bytes: 0,
  excluded_files: 0,
};
export const completeCounts = {
  files: 5,
  total_files: 5,
  documents: 1,
  examples: 1,
  api: 1,
  parts: 5,
  ready_parts: 5,
  bytes: 176,
  excluded_files: 0,
};
const completeInventory = [
  { path: "LICENSE", kind: "license", bytes: 28 },
  { path: "pyproject.toml", kind: "configuration", bytes: 67 },
  { path: "docs/guide.md", kind: "document", bytes: 24 },
  { path: "src/pyfly/service.py", kind: "api", bytes: 31 },
  { path: "examples/demo.py", kind: "example", bytes: 26 },
] as const;
export const completeSources: FrameworkSource[] = completeInventory.map(
  (source, index) => ({
    ...source,
    source_ref: String(index + 1).repeat(32),
    corpus_ref: corpus.corpus_ref,
    blob_oid: String(index + 1).repeat(40),
    sha256:
      source.kind === "license"
        ? corpus.license_sha256
        : String(index + 1).repeat(64),
    parts: 1,
    ready_parts: 1,
    state: "ready",
    source_uri: `https://github.com/fireflyframework/fireflyframework-pyfly/blob/${corpus.commit}/${source.path}`,
    license_sha256: corpus.license_sha256,
    exclusion_reason: null,
  }),
);
export const generationCandidate = {
  validation_run_id: "3".repeat(32),
  repository_name: "inventory-service",
  project_id: "1".repeat(32),
  component_id: "api",
  created_at: "2026-10-26T09:00:00Z",
  source_tree: "4".repeat(40),
  runtime: { runtime: "python", version: "3.13.13" },
  validation_digest: "5".repeat(64),
};
export const discovery: FrameworkDiscovery = {
  version: 1,
  framework: "pyfly",
  policy_digest: "e".repeat(64),
  release: {
    framework: "pyfly",
    framework_version: "26.10.11",
    release_tag: "v26.10.11",
    release_id: 1,
    repository: "fireflyframework/fireflyframework-pyfly",
    commit: corpus.commit,
    tree: "f".repeat(40),
    license_path: "LICENSE",
    license_sha256: corpus.license_sha256,
    release_url:
      "https://github.com/fireflyframework/fireflyframework-pyfly/releases/tag/v26.10.11",
    published_at: "2026-10-24T00:00:00Z",
    checked_at: "2026-10-26T00:00:00Z",
    digest: "1".repeat(64),
  },
  status: {
    framework: "pyfly",
    official_repository: "fireflyframework/fireflyframework-pyfly",
    current_corpus: null,
    latest_stable: null,
    checked_at: null,
    state: "missing",
    counts,
    active_operation: null,
    allowed_actions: ["refresh"],
    revision: "2".repeat(64),
  },
};
