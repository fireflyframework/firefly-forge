/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { request, type Connection } from "./api";
import type { FrameworkCorpus, FrameworkName } from "./FrameworkKnowledgeData";

export type FrameworkGenerationCandidate = {
  validation_run_id: string;
  repository_name: string;
  project_id: string;
  component_id: string;
  created_at: string;
  source_tree: string;
  runtime: { runtime: "python" | "php"; version: string };
  validation_digest: string;
};
export type FrameworkGenerationCandidatePage = {
  version: 1;
  framework: FrameworkName;
  corpus_ref: string;
  items: FrameworkGenerationCandidate[];
  next_cursor: string | null;
};
const nativeId = /^[a-f0-9]{32}$/;
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function text(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}
function candidate(value: unknown, framework: FrameworkName) {
  return (
    record(value) &&
    typeof value.validation_run_id === "string" &&
    nativeId.test(value.validation_run_id) &&
    text(value.repository_name) &&
    text(value.project_id) &&
    text(value.component_id) &&
    text(value.created_at) &&
    Number.isFinite(Date.parse(value.created_at)) &&
    typeof value.source_tree === "string" &&
    /^[a-f0-9]{40}$/.test(value.source_tree) &&
    typeof value.validation_digest === "string" &&
    /^[a-f0-9]{64}$/.test(value.validation_digest) &&
    record(value.runtime) &&
    value.runtime.runtime === (framework === "pyfly" ? "python" : "php") &&
    typeof value.runtime.version === "string" &&
    /^\d+\.\d+\.\d+$/.test(value.runtime.version)
  );
}
export async function frameworkGenerationCandidates(
  connection: Connection,
  corpus: FrameworkCorpus,
  cursor: string | null = null,
  signal?: AbortSignal,
): Promise<FrameworkGenerationCandidatePage> {
  if (
    !["pyfly", "larafly"].includes(corpus.framework) ||
    corpus.state !== "ready" ||
    !nativeId.test(corpus.corpus_ref) ||
    (cursor !== null && !nativeId.test(cursor))
  )
    throw new Error("Choose a ready retained framework corpus.");
  const query = new URLSearchParams({
    corpus_ref: corpus.corpus_ref,
    page_size: "50",
  });
  if (cursor !== null) query.set("cursor", cursor);
  const page = await request<unknown>(
    connection,
    `/knowledge/frameworks/${corpus.framework}/generation-candidates?${query}`,
    { signal },
  );
  if (
    !record(page) ||
    page.version !== 1 ||
    page.framework !== corpus.framework ||
    page.corpus_ref !== corpus.corpus_ref ||
    !Array.isArray(page.items) ||
    !page.items.every((value) => candidate(value, corpus.framework)) ||
    !(
      page.next_cursor === null ||
      (typeof page.next_cursor === "string" &&
        nativeId.test(page.next_cursor) &&
        page.next_cursor !== cursor)
    )
  )
    throw new Error(
      "Eligible validation runs could not be verified. Reload the list.",
    );
  return page as FrameworkGenerationCandidatePage;
}
