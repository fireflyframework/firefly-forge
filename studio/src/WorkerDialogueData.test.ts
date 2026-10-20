/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import delegation from "../tests/scm-delegation-contract-fixture.json";
import { attemptDigest } from "./LearningObservationAttemptData";
import contract from "../tests/worker-dialogue-contract-fixture.json";
import {
  confirmReplyMarkers,
  normalizeWorkerText,
  readReplyMarkers,
  replyMarkerBinding,
  retainReplyMarker,
  workerAttributionSummary,
  workerReply,
  workerThreads,
  type ReplyMarker,
  type ReplyLockManager,
} from "./WorkerDialogueData";
const run = "2".repeat(32);
async function seal<T extends Record<string, unknown>>(body: T) {
  return { ...body, digest: await attemptDigest(body) };
}
async function question() {
  return seal({
    run_id: run,
    sequence: 2,
    task_id: "implement",
    idempotency_key: "worker-feedback:q1",
    kind: "worker_feedback",
    created_at: "2026-10-15T12:00:00+00:00",
    previous_hash: "a".repeat(64),
    payload: {
      id: "q1",
      role_id: "implementer",
      task_id: "implement",
      attempt: 1,
      disposition: "clarification",
      summary: "Confirm the upper bound.",
      evidence_paths: [],
      request: {
        id: "limit",
        kind: "product_definition",
        question: "What is the limit?",
        context: "A reservation needs a limit.",
        choices: ["14 items"],
      },
    },
  });
}
async function response() {
  return seal({
    run_id: run,
    sequence: 5,
    task_id: "implement",
    idempotency_key: "worker-response:a1",
    kind: "worker_response" as const,
    created_at: "2026-10-15T12:01:00+00:00",
    previous_hash: "b".repeat(64),
    payload: {
      id: "a1",
      actor: "Operator",
      answer: "14 items",
      request_id: "limit",
    },
  });
}
function storage() {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    key(index: number) {
      return [...map.keys()][index] ?? null;
    },
    getItem(key: string) {
      return map.get(key) ?? null;
    },
    setItem(key: string, value: string) {
      map.set(key, value);
    },
  };
}
const immediateLocks: ReplyLockManager = {
  request: async (_name, _options, callback) => callback(),
};
describe("agent conversation boundaries", () => {
  async function factoryConversation() {
    const original = await question();
    const { digest: _digest, ...body } = original;
    void _digest;
    const q = await seal({
      ...body,
      payload: {
        ...body.payload,
        request: { ...body.payload.request, kind: "verification" },
      },
    });
    const prior = await response();
    const { digest: _answerDigest, ...answerBody } = prior;
    void _answerDigest;
    const handoffId = "f".repeat(32);
    const a = await seal({
      ...answerBody,
      idempotency_key: `worker-response:${handoffId}`,
      payload: {
        ...answerBody.payload,
        id: handoffId,
        actor: "Forge verification",
        answer: "Two approved commands completed.",
        source: "factory_verification",
        verification_handoff_id: handoffId,
        responding_actor: {
          version: 4,
          authentication_method: "factory_verification",
          handoff_id: handoffId,
          source_digest: "1".repeat(64),
          commands_digest: "2".repeat(64),
          policy_digest: "3".repeat(64),
          provider_digest: "4".repeat(64),
        },
      },
    });
    return { q, a };
  }

  it("attributes a protected factory verification answer separately from a person", async () => {
    const { q, a } = await factoryConversation();
    const threads = await workerThreads([q, a], run);
    expect(threads[0].response?.payload.source).toBe("factory_verification");
    expect(workerAttributionSummary(threads[0].response!)).toContain(
      "approved verification runner",
    );
  });

  it("rejects mismatched factory handoff identities", async () => {
    const { q, a } = await factoryConversation();
    const { digest: _digest, ...body } = a;
    void _digest;
    const changed = await seal({
      ...body,
      payload: { ...body.payload, verification_handoff_id: "e".repeat(32) },
    });
    await expect(workerThreads([q, changed], run)).rejects.toThrow();
  });

  it("does not accept a factory verification answer for a product decision", async () => {
    const { a } = await factoryConversation();
    await expect(workerThreads([await question(), a], run)).rejects.toThrow();
  });

  const delegatedScope = {
    project: delegation.receipt.project_id,
    repository: delegation.receipt.repository_id,
    component: "api",
    readBinding: async () => structuredClone(delegation.receipt),
  };
  async function delegatedResponse(changes: Record<string, unknown> = {}) {
    const original = await response();
    const { digest: _digest, ...body } = original;
    void _digest;
    const reference = delegation.receipt.verified_reference;
    return seal({
      ...body,
      payload: {
        ...body.payload,
        responding_actor: {
          version: 2,
          user_id: delegation.request.native_user_id,
          authentication_method: "scm_mapping",
          provenance: {
            binding_request_id: delegation.request.request_id,
            binding_digest: delegation.receipt.digest,
            // The signed answer delivery is distinct from the mapping's original candidate delivery.
            verified_receipt_id: "3".repeat(32),
            provider: reference.provider,
            api_origin: reference.api_origin,
            connection_revision: reference.connection_revision,
            installation_id: reference.installation_id,
            remote_repository_id: reference.remote_repository_id,
            external_actor_id: reference.external_actor_id,
          },
          ...changes,
        },
      },
    });
  }
  it("reads a strict delegated historical answer without treating it as a manual marker confirmation", async () => {
    const q = await question(),
      a = await delegatedResponse();
    const threads = await workerThreads([q, a], run, delegatedScope);
    expect(threads[0].response?.payload.responding_actor).toEqual(
      a.payload.responding_actor,
    );
    const { responding_actor: _attribution, ...input } = a.payload;
    void _attribution;
    const marker: ReplyMarker = {
      version: 1,
      base: "http://127.0.0.1:8787/api/v1",
      actor: delegation.request.native_user_id,
      project: delegatedScope.project,
      repository: delegatedScope.repository,
      run,
      requestId: "limit",
      responseId: "a1",
      questionDigest: q.digest,
      inputDigest: await attemptDigest(input),
    };
    await expect(confirmReplyMarkers([marker], threads)).rejects.toThrow();
  });
  it.each([
    { version: true },
    { version: 3 },
    { global_admin: true },
    { authentication_method: "oidc_bearer" },
    { user_id: "label" },
    { provenance: { provider: "github" } },
  ])(
    "refuses malformed or privileged delegated attribution %j",
    async (changes) => {
      await expect(
        workerThreads(
          [await question(), await delegatedResponse(changes)],
          run,
          delegatedScope,
        ),
      ).rejects.toThrow();
    },
  );
  it("requires an exact authorized mapping read only for delegated replies", async () => {
    let reads = 0;
    const scope = {
      ...delegatedScope,
      readBinding: async (requestId: string) => {
        reads++;
        expect(requestId).toBe(delegation.request.request_id);
        return structuredClone(delegation.receipt);
      },
    };
    await workerThreads([await question(), await response()], run, scope);
    expect(reads).toBe(0);
    await expect(
      workerThreads([await question(), await delegatedResponse()], run),
    ).rejects.toThrow();
    await workerThreads(
      [await question(), await delegatedResponse()],
      run,
      scope,
    );
    expect(reads).toBe(1);
  });
  it.each([
    { native_user_id: "9".repeat(32) },
    { component_ids: ["web"] },
    { allowed_actions: ["cancel"] },
    { active: false },
  ])(
    "refuses a valid immutable mapping for another user, component or action %j",
    async (changes) => {
      const request = { ...delegation.request, ...changes };
      const { digest: _digest, ...prior } = delegation.receipt;
      void _digest;
      const receipt = await seal({
        ...prior,
        request,
        request_digest: await attemptDigest(request),
      });
      const a = await delegatedResponse();
      a.payload.responding_actor.provenance.binding_digest = receipt.digest;
      const { digest: _eventDigest, ...event } = a;
      void _eventDigest;
      await expect(
        workerThreads([await question(), await seal(event)], run, {
          ...delegatedScope,
          readBinding: async () => receipt,
        }),
      ).rejects.toThrow();
    },
  );
  it("keeps an active historical admission readable after a later revocation", async () => {
    const original = structuredClone(delegation.receipt);
    const request = {
      ...original.request,
      request_id: "9".repeat(32),
      expected_previous_digest: original.digest,
      active: false,
    };
    const { digest: _digest, ...body } = original;
    void _digest;
    const revoked = await seal({
      ...body,
      event_id: "8".repeat(32),
      revision: 2,
      previous_digest: original.digest,
      request,
      request_digest: await attemptDigest(request),
    });
    const receipts = new Map<string, unknown>([
      [original.request.request_id, original],
      [revoked.request.request_id, revoked],
    ]);
    const reads: string[] = [];
    const threads = await workerThreads(
      [await question(), await delegatedResponse()],
      run,
      {
        ...delegatedScope,
        readBinding: async (requestId) => {
          reads.push(requestId);
          return receipts.get(requestId);
        },
      },
    );
    expect(threads[0].response).not.toBeNull();
    expect(reads).toEqual([original.request.request_id]);
  });
  it.each([
    { api_origin: "not-an-origin" },
    { installation_id: null },
    { installation_id: "999" },
    { external_actor_id: "label" },
    { remote_repository_id: "999" },
    { connection_revision: "9".repeat(32) },
    { additional: true },
  ])("refuses malformed or swapped signed provenance %j", async (changes) => {
    const a = await delegatedResponse();
    const attribution = a.payload.responding_actor;
    const forged = await delegatedResponse({
      ...attribution,
      provenance: { ...attribution.provenance, ...changes },
    });
    await expect(
      workerThreads([await question(), forged], run, delegatedScope),
    ).rejects.toThrow();
  });
  it.each([
    ["github", "GitHub"],
    ["gitlab", "GitLab"],
    ["bitbucket", "Bitbucket"],
  ])(
    "labels recorded %s delegation without claiming a personal login",
    async (provider, label) => {
      const event = await delegatedResponse();
      event.payload.responding_actor.provenance.provider = provider;
      expect(workerAttributionSummary(event)).toBe(
        `Delegated via ${label} to a mapped Forge user.`,
      );
    },
  );
  it("accepts the actual Python model and UTF-8 journal digest vectors", async () => {
    const threads = await workerThreads(
      [contract.feedback, contract.legacy_response],
      run,
    );
    expect(threads[0].question.id).toBe("reservation-limit");
    expect(threads[0].response?.payload.answer).toBe("14 items");
  });
  it.each(contract.normalization)(
    "matches the actual backend normalization for $input",
    (vector) => {
      expect(normalizeWorkerText(vector.input)).toBe(vector.answer);
    },
  );
  it("preserves FEFF, emoji and real newlines within the model bounds", () => {
    expect(normalizeWorkerText("\u0085 A\nB \u0085")).toBe("A\nB");
    expect(normalizeWorkerText("\ufeffA\ufeff")).toBe("\ufeffA\ufeff");
    expect(
      workerReply("a1", "Operator", "😀".repeat(8000)).answer,
    ).toHaveLength(16000);
    expect(() => workerReply("a1", "Operator", "a".repeat(8001))).toThrow();
    expect(() => workerReply("invalid:id", "Operator", "answer")).toThrow();
  });
  it("checks individual non-contiguous event digests and matches request and task", async () => {
    const threads = await workerThreads(
      [await question(), await response()],
      run,
    );
    expect(threads[0].response?.payload.answer).toBe("14 items");
    const tampered = await response();
    tampered.payload = {
      id: "a1",
      actor: "Operator",
      answer: "20 items",
      request_id: "limit",
    };
    await expect(
      workerThreads([await question(), tampered], run),
    ).rejects.toThrow();
    const foreign = await seal({
      ...(await response()),
      run_id: "3".repeat(32),
      digest: undefined,
    });
    await expect(
      workerThreads([await question(), foreign], run),
    ).rejects.toThrow();
  });
  it("rejects re-signed cross-request answers and unrecognized authenticated attribution", async () => {
    const original = await response();
    const body: Record<string, unknown> = { ...original };
    delete body.digest;
    await expect(
      workerThreads(
        [
          await question(),
          await seal({
            ...body,
            payload: { ...original.payload, request_id: "other" },
          }),
        ],
        run,
      ),
    ).rejects.toThrow();
    await expect(
      workerThreads(
        [
          await question(),
          await seal({
            ...body,
            payload: {
              ...original.payload,
              responding_actor: {
                version: 1,
                user_id: "f".repeat(32),
                authentication_method: "forged",
              },
            },
          }),
        ],
        run,
      ),
    ).rejects.toThrow();
  });
  it("retains only bound digests before delivery and confirms the exact normalized body", async () => {
    const q = await question(),
      a = await response();
    const store = storage();
    const marker: ReplyMarker = {
      base: "http://127.0.0.1:8787/api/v1",
      actor: "f".repeat(32),
      project: null,
      repository: "3".repeat(32),
      run,
      version: 1,
      requestId: "limit",
      responseId: "a1",
      questionDigest: q.digest,
      inputDigest: await attemptDigest(a.payload),
    };
    await retainReplyMarker(
      store,
      marker,
      new Set(),
      immediateLocks,
      () => true,
    );
    expect(store.getItem(store.key(0)!)).not.toContain("14 items");
    expect(readReplyMarkers(store, marker)).toEqual([marker]);
    const pending = await confirmReplyMarkers(
      [marker],
      await workerThreads([q], run),
    );
    expect(pending.size).toBe(0);
    await expect(
      retainReplyMarker(
        store,
        { ...marker, responseId: "a2" },
        pending,
        immediateLocks,
        () => true,
      ),
    ).rejects.toThrow();
    expect(
      await confirmReplyMarkers([marker], await workerThreads([q, a], run)),
    ).toEqual(new Set([replyMarkerBinding(marker)]));
    await expect(
      confirmReplyMarkers(
        [{ ...marker, inputDigest: "0".repeat(64) }],
        await workerThreads([q, a], run),
      ),
    ).rejects.toThrow();
    expect(
      readReplyMarkers(store, { ...marker, actor: "e".repeat(32) }),
    ).toEqual([]);
  });
  it.each(["inputDigest", "questionDigest", "requestId"] as const)(
    "holds a changed confirmed %s before retaining another answer",
    async (field) => {
      const q = await question(),
        a = await response(),
        store = storage();
      const marker: ReplyMarker = {
        base: "http://127.0.0.1:8787/api/v1",
        actor: "f".repeat(32),
        project: null,
        repository: "3".repeat(32),
        run,
        version: 1,
        requestId: "limit",
        responseId: "a1",
        questionDigest: q.digest,
        inputDigest: await attemptDigest(a.payload),
      };
      await retainReplyMarker(
        store,
        marker,
        new Set(),
        immediateLocks,
        () => true,
      );
      const confirmed = await confirmReplyMarkers(
        [marker],
        await workerThreads([q, a], run),
      );
      const changed = {
        ...marker,
        [field]: field === "requestId" ? "other-question" : "0".repeat(64),
      };
      store.setItem(store.key(0)!, JSON.stringify(changed));
      await expect(
        retainReplyMarker(
          store,
          { ...marker, responseId: "a2", requestId: "next-question" },
          confirmed,
          immediateLocks,
          () => true,
        ),
      ).rejects.toThrow();
      expect(readReplyMarkers(store, marker)).toEqual([changed]);
    },
  );
  it("refuses storage loss before dispatch and malformed saved marker bindings", async () => {
    const broken = {
      ...storage(),
      setItem() {
        throw new Error("unavailable");
      },
    };
    const marker: ReplyMarker = {
      base: "http://127.0.0.1:8787/api/v1",
      actor: "f".repeat(32),
      project: null,
      repository: "3".repeat(32),
      run,
      version: 1,
      requestId: "limit",
      responseId: "a1",
      questionDigest: "a".repeat(64),
      inputDigest: "b".repeat(64),
    };
    await expect(
      retainReplyMarker(broken, marker, new Set(), immediateLocks, () => true),
    ).rejects.toThrow();
    expect(() =>
      readReplyMarkers(storage(), {
        ...marker,
        base: "https://user:secret@example.test/api/v1",
      }),
    ).toThrow();
  });
  it("does not confirm another authenticated user as the retained answer author", async () => {
    const q = await question(),
      a = await response();
    const body: Record<string, unknown> = { ...a };
    delete body.digest;
    const foreign = await seal({
      ...body,
      payload: {
        ...a.payload,
        responding_actor: {
          version: 1,
          user_id: "e".repeat(32),
          authentication_method: "oidc_session",
        },
      },
    });
    const marker: ReplyMarker = {
      base: "http://127.0.0.1:8787/api/v1",
      actor: "f".repeat(32),
      project: null,
      repository: "3".repeat(32),
      run,
      version: 1,
      requestId: "limit",
      responseId: "a1",
      questionDigest: q.digest,
      inputDigest: await attemptDigest(a.payload),
    };
    await expect(
      confirmReplyMarkers([marker], await workerThreads([q, foreign], run)),
    ).rejects.toThrow();
  });
});

describe("exclusive worker reply claims", () => {
  const original: ReplyMarker = {
    version: 1,
    base: "https://forge.example.test/api/v1",
    actor: "f".repeat(32),
    project: null,
    repository: "3".repeat(32),
    run,
    requestId: "limit",
    responseId: "answer-1",
    questionDigest: "a".repeat(64),
    inputDigest: "b".repeat(64),
  };
  function controlledLocks() {
    const names: string[] = [];
    const waiting: (() => void)[] = [];
    const manager: ReplyLockManager = {
      request<T>(
        name: string,
        options: { mode: "exclusive" },
        callback: () => T,
      ) {
        expect(options).toEqual({ mode: "exclusive" });
        names.push(name);
        return new Promise<T>((resolve, reject) => {
          waiting.push(() => {
            try {
              resolve(callback());
            } catch (cause) {
              reject(cause);
            }
          });
        });
      },
    };
    return { manager, names, grant: () => waiting.shift()!() };
  }
  it("does not read or retain an identity before the exclusive lock is granted", async () => {
    const locks = controlledLocks(),
      store = storage();
    let reads = 0;
    const guarded = {
      ...store,
      get length() {
        reads++;
        return store.length;
      },
    };
    const retained = retainReplyMarker(
      guarded,
      original,
      new Set(),
      locks.manager,
      () => true,
    );
    expect(reads).toBe(0);
    expect(store.length).toBe(0);
    expect(locks.names).toEqual([
      "forge.worker-reply.v1:https%3A%2F%2Fforge.example.test%2Fapi%2Fv1:" +
        "f".repeat(32) +
        ":unassigned:" +
        "3".repeat(32) +
        ":" +
        run +
        ":",
    ]);
    locks.grant();
    await expect(retained).resolves.toEqual(original);
    expect(readReplyMarkers(store, original)).toEqual([original]);
  });
  it("holds a second identity after the first claim releases the same scope", async () => {
    const locks = controlledLocks(),
      store = storage();
    const first = retainReplyMarker(
      store,
      original,
      new Set(),
      locks.manager,
      () => true,
    );
    const second = retainReplyMarker(
      store,
      { ...original, responseId: "answer-2" },
      new Set(),
      locks.manager,
      () => true,
    );
    const result = Promise.allSettled([first, second]);
    expect(store.length).toBe(0);
    expect(locks.names[0]).toBe(locks.names[1]);
    locks.grant();
    await first;
    locks.grant();
    expect((await result).map((item) => item.status)).toEqual([
      "fulfilled",
      "rejected",
    ]);
    expect(readReplyMarkers(store, original)).toEqual([original]);
  });
  it("rechecks current context after waiting without retaining an obsolete identity", async () => {
    const locks = controlledLocks(),
      store = storage();
    let current = true;
    const retained = retainReplyMarker(
      store,
      original,
      new Set(),
      locks.manager,
      () => current,
    );
    current = false;
    const refused = expect(retained).rejects.toThrow();
    locks.grant();
    await refused;
    expect(store.length).toBe(0);
  });
  it("refuses missing coordination instead of writing with an unprotected storage fallback", async () => {
    const store = storage();
    await expect(
      retainReplyMarker(store, original, new Set(), undefined, () => true),
    ).rejects.toThrow();
    expect(store.length).toBe(0);
  });
  it("refuses a denied lock request without touching storage", async () => {
    const store = storage();
    const locks: ReplyLockManager = {
      request: async () => {
        throw new Error("denied");
      },
    };
    await expect(
      retainReplyMarker(store, original, new Set(), locks, () => true),
    ).rejects.toThrow();
    expect(store.length).toBe(0);
  });
  it.each([
    { base: "https://other.example.test/api/v1" },
    { actor: "e".repeat(32) },
    { project: "7".repeat(32) },
    { repository: "8".repeat(32) },
    { run: "9".repeat(32) },
  ])(
    "separates unrelated scopes without using per-answer lock names %j",
    async (changes) => {
      const locks = controlledLocks(),
        store = storage();
      const first = retainReplyMarker(
        store,
        original,
        new Set(),
        locks.manager,
        () => true,
      );
      const second = retainReplyMarker(
        store,
        { ...original, ...changes, responseId: "answer-2" },
        new Set(),
        locks.manager,
        () => true,
      );
      expect(locks.names[0]).not.toBe(locks.names[1]);
      locks.grant();
      locks.grant();
      await Promise.all([first, second]);
      expect(store.length).toBe(2);
    },
  );
});

it("reads a distinct native final allowance feedback without widening legacy attempts", async () => {
  const original = await question();
  const { digest: _digest, ...body } = original;
  void _digest;
  const payload = {
    ...body.payload,
    attempt: 5,
    protocol: "native-provider-recovery-allowance-v1",
    allowance_id: "c".repeat(32),
    allowance_digest: "d".repeat(64),
  };
  const final = await seal({ ...body, payload });
  const threads = await workerThreads([final], run);
  expect(threads).toHaveLength(1);
  expect(threads[0].event.payload.attempt).toBe(5);
  for (const change of [
    { protocol: "legacy" },
    { attempt: 6 },
    { allowance_digest: "bad" },
    { allowance_id: "bad" },
    { task_id: "verify" },
    { role_id: "architect" },
  ]) {
    await expect(
      workerThreads(
        [await seal({ ...body, payload: { ...payload, ...change } })],
        run,
      ),
    ).rejects.toThrow();
  }
  await expect(
    workerThreads(
      [await seal({ ...body, payload: { ...body.payload, attempt: 5 } })],
      run,
    ),
  ).rejects.toThrow();
});
