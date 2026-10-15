/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";

export type WorkerQuestion = {
  id: string;
  kind: string;
  question: string;
  context: string;
  choices: string[];
};
export type WorkerEvent = {
  run_id: string;
  sequence: number;
  task_id: string;
  idempotency_key: string;
  kind: "worker_feedback" | "worker_response";
  created_at: string;
  previous_hash: string;
  digest: string;
  payload: Record<string, unknown>;
};
export type WorkerReply = { id: string; actor: string; answer: string };
export type WorkerThread = {
  event: WorkerEvent;
  question: WorkerQuestion;
  response: WorkerEvent | null;
};
export type ReplyContext = {
  base: string;
  actor: string;
  project: string | null;
  repository: string;
  run: string;
};
export type ReplyMarker = ReplyContext & {
  version: 1;
  requestId: string;
  responseId: string;
  questionDigest: string;
  inputDigest: string;
};
type MarkerStorage = Pick<Storage, "length" | "key" | "getItem" | "setItem">;
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/,
  identifier = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/;
const kinds = [
  "product_definition",
  "architecture",
  "design",
  "provider",
  "verification",
  "approval",
  "other",
];
const tasks = [
  "prepare",
  "implement",
  "verify",
  "checkpoint",
  "review",
  "publication",
];
const problem = () =>
  new Error(
    "The recorded agent conversation could not be verified. No answer was sent.",
  );
function assert(value: unknown): asserts value {
  if (!value) throw problem();
}
function object(value: unknown): Record<string, unknown> {
  assert(value && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum: number): string {
  assert(
    typeof value === "string" &&
      Array.from(value).length > 0 &&
      Array.from(value).length <= maximum,
  );
  assert(
    !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(
      value,
    ),
  );
  return value;
}
function matches(value: unknown, pattern: RegExp): string {
  const result = text(value, 256);
  assert(pattern.test(result));
  return result;
}
export function normalizeWorkerText(value: string): string {
  // Match the Pydantic model's Unicode whitespace; U+001C–U+001F and U+FEFF are retained.
  return value.replace(
    // eslint-disable-next-line no-control-regex
    /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g,
    "",
  );
}
export function workerReply(
  responseId: string,
  actor: string,
  answer: string,
): WorkerReply {
  return {
    id: matches(responseId, identifier),
    actor: text(normalizeWorkerText(actor), 200),
    answer: text(normalizeWorkerText(answer), 8000),
  };
}
export async function workerThreads(
  value: unknown,
  run: string,
): Promise<WorkerThread[]> {
  assert(id.test(run) && Array.isArray(value) && value.length <= 512);
  assert(
    new TextEncoder().encode(JSON.stringify(value)).length <= 8 * 1024 * 1024,
  );
  const events: WorkerEvent[] = [];
  let sequence = 0;
  for (const raw of value) {
    const row = object(raw);
    assert(
      Object.keys(row).sort().join(",") ===
        "created_at,digest,idempotency_key,kind,payload,previous_hash,run_id,sequence,task_id",
    );
    assert(
      row.run_id === run &&
        Number.isSafeInteger(row.sequence) &&
        (row.sequence as number) > sequence,
    );
    sequence = row.sequence as number;
    assert(row.kind === "worker_feedback" || row.kind === "worker_response");
    assert(tasks.includes(text(row.task_id, 80)));
    text(row.created_at, 80);
    assert(Number.isFinite(Date.parse(row.created_at as string)));
    matches(row.previous_hash, hash);
    matches(row.digest, hash);
    const { digest, ...body } = row;
    assert(digest === (await attemptDigest(body)));
    const payload = object(row.payload);
    matches(payload.id, identifier);
    assert(
      row.idempotency_key ===
        `${row.kind === "worker_feedback" ? "worker-feedback" : "worker-response"}:${payload.id}`,
    );
    events.push(row as WorkerEvent);
  }
  const threads: WorkerThread[] = [];
  const responseIds = new Set<string>();
  for (const event of events) {
    const payload = event.payload;
    if (event.kind === "worker_feedback") {
      assert(
        Object.keys(payload).sort().join(",") ===
          "attempt,disposition,evidence_paths,id,request,role_id,summary,task_id",
      );
      assert(payload.task_id === event.task_id);
      assert(
        [
          "product-manager",
          "tech-lead",
          "architect",
          "implementer",
          "qa",
          "security",
          "docs",
        ].includes(payload.role_id as string),
      );
      text(payload.summary, 8000);
      assert(
        ["completed", "blocked", "clarification", "failed"].includes(
          payload.disposition as string,
        ),
      );
      assert(
        Array.isArray(payload.evidence_paths) &&
          payload.evidence_paths.length <= 100,
      );
      payload.evidence_paths.forEach((path) => text(path, 300));
      assert(
        Number.isInteger(payload.attempt) &&
          (payload.attempt as number) >= 1 &&
          (payload.attempt as number) <= 4,
      );
      if (payload.request == null) continue;
      assert(
        ["blocked", "clarification"].includes(payload.disposition as string),
      );
      const request = object(payload.request);
      assert(
        Object.keys(request).sort().join(",") ===
          "choices,context,id,kind,question",
      );
      matches(request.id, identifier);
      assert(kinds.includes(request.kind as string));
      text(request.question, 4000);
      text(request.context, 4000);
      assert(Array.isArray(request.choices) && request.choices.length <= 6);
      request.choices.forEach((choice) => text(choice, 1000));
      assert(!threads.some((thread) => thread.question.id === request.id));
      assert(!threads.some((thread) => !thread.response));
      threads.push({
        event,
        question: request as WorkerQuestion,
        response: null,
      });
    } else {
      const { responding_actor: attribution, ...answer } = payload;
      assert(
        Object.keys(answer).sort().join(",") === "actor,answer,id,request_id",
      );
      const reply = workerReply(
        answer.id as string,
        answer.actor as string,
        answer.answer as string,
      );
      assert(
        reply.actor === answer.actor &&
          reply.answer === answer.answer &&
          !responseIds.has(reply.id),
      );
      responseIds.add(reply.id);
      matches(answer.request_id, identifier);
      if (attribution !== undefined) {
        const identity = object(attribution);
        assert(
          Object.keys(identity).sort().join(",") ===
            "authentication_method,user_id,version",
        );
        assert(
          identity.version === 1 &&
            ["oidc_session", "oidc_bearer", "local_bootstrap"].includes(
              identity.authentication_method as string,
            ),
        );
        matches(identity.user_id, id);
      }
      const thread = threads.find(
        (item) => item.question.id === answer.request_id,
      );
      assert(
        thread && !thread.response && thread.event.task_id === event.task_id,
      );
      thread.response = event;
    }
  }
  return threads;
}
function prefix(scope: ReplyContext): string {
  const url = new URL(scope.base);
  assert(
    ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash,
  );
  assert(url.href.replace(/\/$/, "") === scope.base);
  assert(
    [scope.actor, scope.repository, scope.run].every(
      (value) => typeof value === "string" && id.test(value),
    ),
  );
  assert(
    scope.project === null ||
      (typeof scope.project === "string" && id.test(scope.project)),
  );
  return `forge.worker-reply.v1:${encodeURIComponent(scope.base)}:${scope.actor}:${scope.project ?? "unassigned"}:${scope.repository}:${scope.run}:`;
}
function marker(value: unknown): ReplyMarker {
  const row = object(value);
  assert(
    Object.keys(row).sort().join(",") ===
      "actor,base,inputDigest,project,questionDigest,repository,requestId,responseId,run,version",
  );
  assert(row.version === 1);
  matches(row.requestId, identifier);
  matches(row.responseId, identifier);
  matches(row.inputDigest, hash);
  matches(row.questionDigest, hash);
  prefix(row as ReplyMarker);
  return row as ReplyMarker;
}
export function replyMarkerBinding(value: ReplyMarker): string {
  marker(value);
  return (
    prefix(value) +
    value.responseId +
    ":" +
    JSON.stringify([value.requestId, value.questionDigest, value.inputDigest])
  );
}
export function readReplyMarkers(
  storage: MarkerStorage,
  scope: ReplyContext,
): ReplyMarker[] {
  const scopePrefix = prefix(scope);
  assert(storage.length <= 4096);
  const result: ReplyMarker[] = [];
  for (let i = 0; i < storage.length; i++) {
    const key = storage.key(i);
    if (!key?.startsWith(scopePrefix)) continue;
    const value = marker(JSON.parse(storage.getItem(key) ?? "null"));
    assert(
      prefix(value) === scopePrefix && key === scopePrefix + value.responseId,
    );
    result.push(value);
    assert(result.length <= 100);
  }
  return result;
}
export function retainReplyMarker(
  storage: MarkerStorage,
  input: ReplyMarker,
  confirmed: ReadonlySet<string>,
): ReplyMarker {
  const value = marker(input);
  const prior = readReplyMarkers(storage, value);
  assert(
    prior.length < 100 &&
      prior.every((item) => confirmed.has(replyMarkerBinding(item))),
  );
  const key = prefix(value) + value.responseId;
  assert(storage.getItem(key) === null);
  const encoded = JSON.stringify(value);
  storage.setItem(key, encoded);
  assert(storage.getItem(key) === encoded);
  return marker(JSON.parse(encoded));
}
export async function confirmReplyMarkers(
  markers: ReplyMarker[],
  threads: WorkerThread[],
): Promise<Set<string>> {
  const confirmed = new Set<string>();
  for (const value of markers) {
    const thread = threads.find((item) => item.question.id === value.requestId);
    if (!thread) continue;
    assert(
      thread.event.run_id === value.run &&
        thread.event.digest === value.questionDigest,
    );
    if (!thread.response || thread.response.payload.id !== value.responseId)
      continue;
    const attribution = thread.response.payload.responding_actor;
    if (attribution !== undefined)
      assert(object(attribution).user_id === value.actor);
    const { id, actor, answer, request_id } = thread.response.payload;
    const input = { id, actor, answer, request_id };
    assert((await attemptDigest(input)) === value.inputDigest);
    confirmed.add(replyMarkerBinding(value));
  }
  return confirmed;
}
