/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { normalizeBase, request, type Connection } from "./api";

export type FrameworkName = "pyfly" | "larafly";
export type FrameworkAction =
  | "check_updates"
  | "refresh"
  | "observe"
  | "search"
  | "browse"
  | "renew_budget"
  | "cancel"
  | "resume"
  | "activate_generation_target";
export type FrameworkCounts = {
  files: number;
  total_files: number;
  documents: number;
  examples: number;
  api: number;
  parts: number;
  ready_parts: number;
  bytes: number;
  excluded_files: number;
};
export type FrameworkRelease = {
  framework: FrameworkName;
  framework_version: string;
  release_tag: string;
  release_id: number;
  repository: string;
  commit: string;
  tree: string;
  license_path: string;
  license_sha256: string;
  release_url: string;
  published_at: string;
  checked_at: string;
  digest: string;
};
export type FrameworkCorpus = {
  corpus_ref: string;
  framework: FrameworkName;
  framework_version: string;
  release_tag: string;
  commit: string;
  manifest_sha256: string | null;
  license_sha256: string;
  workspace_id: string;
  state:
    | "queued"
    | "fetching"
    | "indexing"
    | "verifying"
    | "ready"
    | "needs_reconciliation"
    | "failed";
};
export type FrameworkOperation = {
  request_id: string;
  framework: FrameworkName;
  corpus: FrameworkCorpus | null;
  state:
    | "queued"
    | "running"
    | "needs_reconciliation"
    | "ready"
    | "failed"
    | "cancelled";
  stage:
    | "discovery"
    | "inventory"
    | "fetching"
    | "submitting"
    | "verifying"
    | "complete";
  counts: FrameworkCounts;
  spent_budget: { requests: number; bytes: number; seconds: number };
  remaining_budget: { requests: number; bytes: number; seconds: number };
  sanitized_error: string | null;
  uncertain_sources: number;
  allowed_actions: FrameworkAction[];
  revision: string;
};
export type FrameworkStatus = {
  framework: FrameworkName;
  official_repository: string;
  current_corpus: FrameworkCorpus | null;
  latest_stable: FrameworkRelease | null;
  generation_target?: FrameworkGenerationTarget | null;
  discovery_retry_at?: string | null;
  discovery_error?: string | null;
  checked_at: string | null;
  state:
    | "missing"
    | "indexed"
    | "checking"
    | "update_available"
    | "current"
    | "refreshing"
    | "needs_reconciliation"
    | "failed";
  counts: FrameworkCounts;
  active_operation: FrameworkOperation | null;
  allowed_actions: FrameworkAction[];
  revision: string;
};
export type FrameworkDiscovery = {
  version: 1;
  framework: FrameworkName;
  release: FrameworkRelease;
  status: FrameworkStatus;
  policy_digest: string;
};
export type FrameworkRefreshBody = {
  version: 1;
  discovery_digest: string;
  expected_status_revision: string;
  policy_digest: string;
  make_current: true;
};
export type FrameworkRefreshAttempt = {
  framework: FrameworkName;
  request_id: string;
  body: FrameworkRefreshBody;
};
export type FrameworkBudget = {
  requests: number;
  bytes: number;
  seconds: number;
};
export type FrameworkControl = "renew_budget" | "cancel" | "resume";
export type FrameworkControlAttempt = {
  framework: FrameworkName;
  request_id: string;
  control: FrameworkControl;
  body: {
    version: 1;
    expected_revision: string;
    total_budget?: FrameworkBudget;
  };
};
export type FrameworkGenerationBody = {
  version: 1;
  corpus_ref: string;
  expected_status_revision: string;
  validation_run_id: string;
};
export type FrameworkGenerationTarget = {
  version: 1;
  request_id: string;
  framework: FrameworkName;
  corpus: FrameworkCorpus;
  validation_run_id: string;
  validation_source_tree: string;
  validation_digest: string;
  activation_actor_digest: string;
  activated_at: string;
  revision: string;
};
export type FrameworkActivationAttempt = {
  activation: true;
  framework: FrameworkName;
  request_id: string;
  corpus: FrameworkCorpus;
  body: FrameworkGenerationBody;
};
export type FrameworkPendingAttempt =
  | FrameworkRefreshAttempt
  | FrameworkControlAttempt
  | FrameworkActivationAttempt;
export type FrameworkSource = {
  source_ref: string;
  corpus_ref: string;
  path: string;
  kind: "document" | "example" | "api" | "license" | "configuration";
  blob_oid: string;
  sha256: string | null;
  bytes: number;
  parts: number;
  ready_parts: number;
  state:
    | "pending"
    | "fetched"
    | "submitting"
    | "indexing"
    | "ready"
    | "needs_reconciliation"
    | "failed"
    | "excluded";
  source_uri: string;
  license_sha256: string;
  exclusion_reason: string | null;
};
export type FrameworkCitation = {
  source_ref: string;
  corpus_ref: string;
  framework: FrameworkName;
  framework_version: string;
  release_tag: string;
  commit: string;
  source_id: string;
  chunk_id: string | null;
  path: string;
  source_uri: string;
  source_sha256: string;
  license_sha256: string;
  knowledge_version: number | null;
  score: number;
};
export type FrameworkHit = FrameworkCitation & { content: string };
export type FrameworkSearchResult = {
  corpus: FrameworkCorpus;
  hits: FrameworkHit[];
  citations: FrameworkCitation[];
  warnings: string[];
  context: string;
};
export type FrameworkPage<T> = {
  items: T[];
  next_cursor: string | null;
  total: number;
};

const prefix = "/knowledge/frameworks";
const nativeId = /^[a-f0-9]{32}$/;
const digest = /^[a-f0-9]{64}$/;
export function formatKnowledgeTimestamp(value: string) {
  const time = new Date(value);
  return Number.isNaN(time.getTime())
    ? "Time unavailable"
    : new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
      }).format(time);
}
function requireId(value: string) {
  if (!nativeId.test(value))
    throw new Error("The framework operation reference is invalid.");
}
function target(framework: FrameworkName) {
  if (framework !== "pyfly" && framework !== "larafly")
    throw new Error("Choose a supported framework.");
  return `${prefix}/${framework}`;
}
function sameCorpus(
  actual: Pick<
    FrameworkCorpus,
    "corpus_ref" | "framework" | "framework_version" | "commit" | "release_tag"
  >,
  expected: FrameworkCorpus,
) {
  return (
    actual.corpus_ref === expected.corpus_ref &&
    actual.framework === expected.framework &&
    actual.framework_version === expected.framework_version &&
    actual.commit === expected.commit &&
    actual.release_tag === expected.release_tag
  );
}
export function corpusSelectionKey(
  connection: Connection,
  user: string,
  corpus: FrameworkCorpus,
) {
  return JSON.stringify([
    normalizeBase(connection.base),
    user,
    corpus.framework,
    corpus.corpus_ref,
    corpus.framework_version,
    corpus.commit,
  ]);
}
export function pendingFrameworkKey(
  connection: Connection,
  user: string,
  framework: FrameworkName,
) {
  return JSON.stringify([
    "forge-framework-refresh-v1",
    normalizeBase(connection.base),
    user,
    framework,
  ]);
}
export function refreshBody(
  discovery: FrameworkDiscovery,
  current: FrameworkStatus,
): FrameworkRefreshBody {
  if (
    discovery.version !== 1 ||
    discovery.framework !== current.framework ||
    discovery.release.framework !== current.framework ||
    discovery.status.revision !== current.revision ||
    !current.allowed_actions.includes("refresh") ||
    !digest.test(discovery.release.digest) ||
    !digest.test(current.revision) ||
    !digest.test(discovery.policy_digest) ||
    (current.active_operation &&
      ["queued", "running", "needs_reconciliation"].includes(
        current.active_operation.state,
      ))
  ) {
    throw new Error(
      "Check the current release and refresh permissions before starting another operation.",
    );
  }
  return {
    version: 1,
    discovery_digest: discovery.release.digest,
    expected_status_revision: current.revision,
    policy_digest: discovery.policy_digest,
    make_current: true,
  };
}
export function loadFrameworkStatus(
  connection: Connection,
  signal?: AbortSignal,
) {
  return request<{ version: 1; frameworks: FrameworkStatus[] }>(
    connection,
    `${prefix}/status`,
    { signal },
  );
}
export function checkFrameworkUpdates(
  connection: Connection,
  framework: FrameworkName,
) {
  return request<FrameworkDiscovery>(
    connection,
    `${target(framework)}/check-updates`,
    {
      method: "POST",
      body: JSON.stringify({ version: 1, framework_version: "latest" }),
    },
  );
}
export class FrameworkRefreshUncertain extends Error {
  constructor(public attempt: FrameworkPendingAttempt) {
    super(
      "The operation response is unconfirmed. Read the original operation before taking another action.",
    );
    this.name = "FrameworkRefreshUncertain";
  }
}
type PendingStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export function readPendingFrameworkRefresh(
  storage: PendingStorage,
  key: string,
  framework: FrameworkName,
): FrameworkPendingAttempt | null {
  const raw = storage.getItem(key);
  if (raw === null) return null;
  const value = JSON.parse(raw) as FrameworkPendingAttempt;
  if (
    !value ||
    value.framework !== framework ||
    !nativeId.test(value.request_id) ||
    !value.body ||
    value.body.version !== 1
  ) {
    throw new Error("The retained refresh request could not be verified.");
  }
  if ("activation" in value) {
    assertActivationAttempt(value);
  } else if ("control" in value) {
    if (
      !["renew_budget", "cancel", "resume"].includes(value.control) ||
      !digest.test(value.body.expected_revision) ||
      (value.control === "renew_budget" &&
        !validWorkBudget(value.body.total_budget))
    )
      throw new Error("The retained operation change could not be verified.");
  } else if (
    value.body.make_current !== true ||
    !digest.test(value.body.discovery_digest) ||
    !digest.test(value.body.policy_digest) ||
    !digest.test(value.body.expected_status_revision)
  ) {
    throw new Error("The retained refresh request could not be verified.");
  }
  return value;
}
function retainAttempt(
  storage: PendingStorage,
  key: string,
  attempt: FrameworkPendingAttempt,
  onRetained?: (value: FrameworkPendingAttempt) => void,
) {
  const prior = readPendingFrameworkRefresh(storage, key, attempt.framework);
  if (prior) {
    onRetained?.(prior);
    throw new FrameworkRefreshUncertain(prior);
  }
  storage.setItem(key, JSON.stringify(attempt));
  onRetained?.(attempt);
}
function assertActivationAttempt(value: FrameworkActivationAttempt) {
  if (
    value.activation !== true ||
    !nativeId.test(value.request_id) ||
    !value.corpus ||
    value.corpus.framework !== value.framework ||
    value.corpus.state !== "ready" ||
    !nativeId.test(value.body.corpus_ref) ||
    value.body.corpus_ref !== value.corpus.corpus_ref ||
    !nativeId.test(value.body.validation_run_id) ||
    !digest.test(value.body.expected_status_revision) ||
    !digest.test(value.corpus.manifest_sha256 ?? "")
  )
    throw new Error(
      "The retained generation activation could not be verified.",
    );
}
export function generationTargetBody(
  status: FrameworkStatus,
  corpus: FrameworkCorpus,
  validationRunId: string,
): FrameworkGenerationBody {
  if (
    !status.allowed_actions.includes("activate_generation_target") ||
    status.framework !== corpus.framework ||
    corpus.state !== "ready" ||
    !nativeId.test(corpus.corpus_ref) ||
    !nativeId.test(validationRunId) ||
    !digest.test(status.revision) ||
    !digest.test(corpus.manifest_sha256 ?? "")
  )
    throw new Error(
      "Review a ready corpus, current activation permission and a native validation run.",
    );
  return {
    version: 1,
    corpus_ref: corpus.corpus_ref,
    expected_status_revision: status.revision,
    validation_run_id: validationRunId,
  };
}
function checkedGenerationTarget(
  value: FrameworkGenerationTarget,
  attempt: FrameworkActivationAttempt,
) {
  if (
    value.version !== 1 ||
    value.request_id !== attempt.request_id ||
    value.framework !== attempt.framework ||
    value.validation_run_id !== attempt.body.validation_run_id ||
    !value.corpus ||
    !sameCorpus(value.corpus, attempt.corpus) ||
    value.corpus.manifest_sha256 !== attempt.corpus.manifest_sha256 ||
    value.corpus.license_sha256 !== attempt.corpus.license_sha256 ||
    value.corpus.workspace_id !== attempt.corpus.workspace_id ||
    value.corpus.state !== "ready" ||
    !/^[a-f0-9]{40}$/.test(value.validation_source_tree) ||
    !digest.test(value.validation_digest) ||
    !digest.test(value.activation_actor_digest) ||
    !digest.test(value.revision) ||
    !Number.isFinite(Date.parse(value.activated_at))
  )
    throw new Error(
      "The generation activation receipt does not match the retained review.",
    );
  return value;
}
export async function submitRetainedGenerationTarget(
  connection: Connection,
  attempt: FrameworkActivationAttempt,
  storage: PendingStorage,
  key: string,
  onRetained?: (value: FrameworkPendingAttempt) => void,
) {
  assertActivationAttempt(attempt);
  retainAttempt(storage, key, attempt, onRetained);
  try {
    return checkedGenerationTarget(
      await request<FrameworkGenerationTarget>(
        connection,
        `${target(attempt.framework)}/generation-targets/${attempt.request_id}`,
        { method: "POST", body: JSON.stringify(attempt.body) },
      ),
      attempt,
    );
  } catch {
    throw new FrameworkRefreshUncertain(attempt);
  }
}
export async function readGenerationTarget(
  connection: Connection,
  attempt: FrameworkActivationAttempt,
) {
  assertActivationAttempt(attempt);
  return checkedGenerationTarget(
    await request<FrameworkGenerationTarget>(
      connection,
      `${target(attempt.framework)}/generation-targets/${attempt.request_id}`,
      {},
    ),
    attempt,
  );
}
export async function submitRetainedFrameworkRefresh(
  connection: Connection,
  attempt: FrameworkRefreshAttempt,
  storage: PendingStorage,
  key: string,
  onRetained?: (value: FrameworkPendingAttempt) => void,
) {
  retainAttempt(storage, key, attempt, onRetained);
  return submitFrameworkRefresh(connection, attempt);
}
function validWorkBudget(value: unknown): value is FrameworkBudget {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return (
    Object.keys(record).length === 3 &&
    ["requests", "bytes", "seconds"].every(
      (name) =>
        typeof record[name] === "number" &&
        Number.isSafeInteger(record[name]) &&
        record[name] > 0,
    )
  );
}
export async function submitFrameworkControl(
  connection: Connection,
  operation: FrameworkOperation,
  control: FrameworkControl,
  storage: PendingStorage,
  key: string,
  totalBudget?: FrameworkBudget,
  onRetained?: (value: FrameworkPendingAttempt) => void,
) {
  requireId(operation.request_id);
  if (
    !operation.allowed_actions.includes(control) ||
    !digest.test(operation.revision) ||
    (control === "resume" && operation.uncertain_sources !== 0)
  )
    throw new Error(
      "Read the current operation and its permitted actions before continuing.",
    );
  const keys = ["requests", "bytes", "seconds"] as const;
  if (control === "renew_budget") {
    if (
      !validWorkBudget(totalBudget) ||
      keys.some(
        (name) =>
          totalBudget[name] + 1e-8 <
          operation.spent_budget[name] + operation.remaining_budget[name],
      ) ||
      !keys.some(
        (name) =>
          totalBudget[name] >
          operation.spent_budget[name] +
            operation.remaining_budget[name] +
            1e-8,
      )
    ) {
      throw new Error(
        "Increase the reviewed total allowance and preserve every spent request, byte and second.",
      );
    }
  }
  const attempt: FrameworkControlAttempt = {
    framework: operation.framework,
    request_id: operation.request_id,
    control,
    body: {
      version: 1,
      expected_revision: operation.revision,
      ...(control === "renew_budget" ? { total_budget: totalBudget } : {}),
    },
  };
  retainAttempt(storage, key, attempt, onRetained);
  try {
    const result = checkedOperation(
      await request<FrameworkOperation>(
        connection,
        `${target(operation.framework)}/refreshes/${operation.request_id}/${control === "renew_budget" ? "budget" : control}`,
        { method: "POST", body: JSON.stringify(attempt.body) },
      ),
      operation.framework,
      operation.request_id,
    );
    if (
      keys.some(
        (name) => result.spent_budget[name] < operation.spent_budget[name],
      )
    )
      throw new Error("Retained spent usage changed.");
    return result;
  } catch {
    throw new FrameworkRefreshUncertain(attempt);
  }
}
function checkedOperation(
  value: FrameworkOperation,
  framework: FrameworkName,
  requestId: string,
) {
  if (
    value.framework !== framework ||
    value.request_id !== requestId ||
    (value.corpus && value.corpus.framework !== framework)
  ) {
    throw new Error(
      "The framework operation readback does not match the retained request.",
    );
  }
  return value;
}
export async function submitFrameworkRefresh(
  connection: Connection,
  attempt: FrameworkRefreshAttempt,
) {
  requireId(attempt.request_id);
  try {
    return checkedOperation(
      await request<FrameworkOperation>(
        connection,
        `${target(attempt.framework)}/refreshes/${attempt.request_id}`,
        {
          method: "POST",
          body: JSON.stringify(attempt.body),
        },
      ),
      attempt.framework,
      attempt.request_id,
    );
  } catch {
    // A lost response cannot establish whether the server admitted the write.
    throw new FrameworkRefreshUncertain(attempt);
  }
}
export async function readFrameworkOperation(
  connection: Connection,
  framework: FrameworkName,
  requestId: string,
  observe = false,
) {
  requireId(requestId);
  return checkedOperation(
    await request<FrameworkOperation>(
      connection,
      `${target(framework)}/refreshes/${requestId}${observe ? "/observe" : ""}`,
      observe ? { method: "POST", body: "{}" } : {},
    ),
    framework,
    requestId,
  );
}
export async function frameworkCorpora(
  connection: Connection,
  framework: FrameworkName,
  cursor: string | null = null,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ page_size: "50" });
  if (cursor) query.set("cursor", cursor);
  const page = await request<FrameworkPage<FrameworkCorpus>>(
    connection,
    `${target(framework)}/corpora?${query}`,
    { signal },
  );
  if (page.items.some((corpus) => corpus.framework !== framework)) {
    throw new Error("The corpus page belongs to another framework.");
  }
  return page;
}
export async function frameworkSources(
  connection: Connection,
  corpus: FrameworkCorpus,
  cursor: string | null = null,
  signal?: AbortSignal,
) {
  requireId(corpus.corpus_ref);
  const query = new URLSearchParams({ page_size: "50" });
  if (cursor) query.set("cursor", cursor);
  const page = await request<FrameworkPage<FrameworkSource>>(
    connection,
    `${target(corpus.framework)}/corpora/${corpus.corpus_ref}/sources?${query}`,
    { signal },
  );
  if (page.items.some((source) => source.corpus_ref !== corpus.corpus_ref))
    throw new Error("The source page belongs to another framework corpus.");
  return page;
}
export async function frameworkSearch(
  connection: Connection,
  corpus: FrameworkCorpus,
  query: string,
  signal?: AbortSignal,
) {
  requireId(corpus.corpus_ref);
  if (corpus.state !== "ready" || !query.trim() || query.length > 2000)
    throw new Error(
      "Choose a ready release and enter a search of up to 2,000 characters.",
    );
  const result = await request<FrameworkSearchResult>(
    connection,
    `${target(corpus.framework)}/search`,
    {
      method: "POST",
      body: JSON.stringify({
        query: query.trim(),
        corpus_ref: corpus.corpus_ref,
      }),
      signal,
    },
  );
  if (
    !sameCorpus(result.corpus, corpus) ||
    result.corpus.license_sha256 !== corpus.license_sha256 ||
    result.corpus.manifest_sha256 !== corpus.manifest_sha256 ||
    result.corpus.workspace_id !== corpus.workspace_id ||
    result.corpus.state !== "ready" ||
    [...result.hits, ...result.citations].some(
      (hit) =>
        !sameCorpus(hit, corpus) ||
        !digest.test(hit.license_sha256) ||
        !digest.test(hit.source_sha256),
    )
  ) {
    throw new Error(
      "The search citations do not match the selected immutable framework release.",
    );
  }
  return result;
}
