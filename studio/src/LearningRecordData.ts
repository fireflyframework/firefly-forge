/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type LearningRecord = {
  id: string;
  state: "candidate" | "verified" | "invalidated" | "superseded";
  kind: "local_outcome" | "observed_failure" | "web_reference";
  observed_at?: string;
  verified_at?: string;
  expires_at: string;
  envelope: {
    run_id: string;
    task_id: string;
    role: string;
    purpose: string;
    question: string;
    statement: string;
    context: {
      repository_id: string;
      project_id: string | null;
      component_id: string | null;
      framework: string;
      version: string;
      dependency_digest: string;
      source_tree: string;
      query_digest: string;
    };
    source: { base_revision: string; head: string; source_tree: string };
    citations: string[];
    receipts: { kind: string; key: string; sequence: number; digest: string }[];
  };
};
const invalid = () =>
  new Error(
    "Learning records could not be read. The response does not match this repository's evidence contract.",
  );
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/,
  revision = /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/;
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum: number, pattern?: RegExp): string {
  if (
    typeof value !== "string" ||
    !value.length ||
    value.length > maximum ||
    (pattern && !pattern.test(value))
  )
    throw invalid();
  return value;
}
function choice<T extends string>(value: unknown, options: readonly T[]): T {
  if (typeof value !== "string" || !options.includes(value as T))
    throw invalid();
  return value as T;
}
function timestamp(value: unknown): string {
  const result = text(value, 100);
  if (
    !/(Z|[+-]\d{2}:\d{2})$/.test(result) ||
    !Number.isFinite(Date.parse(result))
  )
    throw invalid();
  return result;
}
function list(value: unknown, maximum: number): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw invalid();
  return value;
}
export function readLearningRecords(
  value: unknown,
  repository: string,
): LearningRecord[] {
  if (!id.test(repository)) throw invalid();
  const seen = new Set<string>();
  return list(value, 256).map((entry) => {
    const row = object(entry),
      envelope = object(row.envelope),
      context = object(envelope.context),
      source = object(envelope.source);
    const recordId = text(row.id, 32, id),
      sourceTree = text(context.source_tree, 64, revision);
    if (
      seen.has(recordId) ||
      context.repository_id !== repository ||
      source.source_tree !== sourceTree ||
      envelope.version !== 1
    )
      throw invalid();
    seen.add(recordId);
    const kind = choice(row.kind, [
      "local_outcome",
      "observed_failure",
      "web_reference",
    ] as const);
    if (
      kind === "local_outcome"
        ? row.verified_at == null
        : row.observed_at == null
    )
      throw invalid();
    return {
      id: recordId,
      state: choice(row.state, [
        "candidate",
        "verified",
        "invalidated",
        "superseded",
      ] as const),
      kind,
      ...(row.observed_at != null
        ? { observed_at: timestamp(row.observed_at) }
        : {}),
      ...(row.verified_at != null
        ? { verified_at: timestamp(row.verified_at) }
        : {}),
      expires_at: timestamp(row.expires_at),
      envelope: {
        run_id: text(envelope.run_id, 32, id),
        task_id: text(envelope.task_id, 80),
        role: choice(envelope.role, [
          "implementer",
          "docs",
          "qa",
          "tech-lead",
          "security",
          "architect",
          "product-manager",
        ]),
        purpose: choice(envelope.purpose, [
          "verification",
          "regression",
          "diagnostic",
          "intentional_red",
          "repair_generation",
        ]),
        question: text(envelope.question, 4000),
        statement: text(envelope.statement, 6000),
        context: {
          repository_id: repository,
          project_id:
            context.project_id == null
              ? null
              : text(context.project_id, 32, id),
          component_id:
            context.component_id == null
              ? null
              : text(context.component_id, 64),
          framework: choice(context.framework, ["pyfly", "larafly", "none"]),
          version: text(context.version, 100),
          dependency_digest: text(context.dependency_digest, 64, hash),
          source_tree: sourceTree,
          query_digest: text(context.query_digest, 64, hash),
        },
        source: {
          base_revision: text(source.base_revision, 64, revision),
          head: text(source.head, 64, revision),
          source_tree: sourceTree,
        },
        citations: list(envelope.citations, 16).map((citation) =>
          text(citation, 200),
        ),
        receipts: list(envelope.receipts, 40).map((entry) => {
          const receipt = object(entry);
          if (
            typeof receipt.sequence !== "number" ||
            !Number.isSafeInteger(receipt.sequence) ||
            receipt.sequence < 1
          )
            throw invalid();
          return {
            kind: choice(receipt.kind, [
              "receipt",
              "tool_receipt",
              "sandbox_receipt",
              "review_receipt",
              "checkpoint",
              "error",
            ]),
            key: text(receipt.key, 200),
            sequence: receipt.sequence,
            digest: text(receipt.digest, 64, hash),
          };
        }),
      },
    };
  });
}
export type LearningProjection = {
  id: string;
  record_id: string;
  state: "pending" | "executing" | "source_admitted" | "verified" | "unknown";
  workspace_id?: string;
  source_id?: string;
  source_status?: string;
  content_sha256?: string;
  target_revision?: string;
  citations: { source_id: string; chunk_id: string }[];
};
export function readLearningProjection(
  value: unknown,
  record: string,
): LearningProjection {
  const row = object(value);
  if (row.record_id !== record || !id.test(record)) throw invalid();
  const result: LearningProjection = {
    id: text(row.id, 32, id),
    record_id: record,
    state: choice(row.state, [
      "pending",
      "executing",
      "source_admitted",
      "verified",
      "unknown",
    ] as const),
    citations:
      row.citations == null
        ? []
        : list(row.citations, 16).map((entry) => {
            const citation = object(entry);
            return {
              source_id: text(citation.source_id, 200),
              chunk_id: text(citation.chunk_id, 200),
            };
          }),
  };
  for (const field of ["workspace_id", "source_id", "source_status"] as const)
    if (row[field] != null) result[field] = text(row[field], 200);
  if (row.content_sha256 != null)
    result.content_sha256 = text(row.content_sha256, 64, hash);
  if (row.target != null) {
    const target = object(row.target);
    if (target.revision != null)
      result.target_revision = text(target.revision, 32, id);
  }
  if (
    result.state === "verified" &&
    (!result.workspace_id ||
      !result.source_id ||
      !result.content_sha256 ||
      !result.citations.length)
  )
    throw invalid();
  if (
    result.citations.some((citation) => citation.source_id !== result.source_id)
  )
    throw invalid();
  return result;
}
