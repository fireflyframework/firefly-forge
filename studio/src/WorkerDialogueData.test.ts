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
describe("agent conversation boundaries", () => {
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
    retainReplyMarker(store, marker, new Set());
    expect(store.getItem(store.key(0)!)).not.toContain("14 items");
    expect(readReplyMarkers(store, marker)).toEqual([marker]);
    const pending = await confirmReplyMarkers(
      [marker],
      await workerThreads([q], run),
    );
    expect(pending.size).toBe(0);
    expect(() =>
      retainReplyMarker(store, { ...marker, responseId: "a2" }, pending),
    ).toThrow();
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
      retainReplyMarker(store, marker, new Set());
      const confirmed = await confirmReplyMarkers(
        [marker],
        await workerThreads([q, a], run),
      );
      const changed = {
        ...marker,
        [field]: field === "requestId" ? "other-question" : "0".repeat(64),
      };
      store.setItem(store.key(0)!, JSON.stringify(changed));
      expect(() =>
        retainReplyMarker(
          store,
          { ...marker, responseId: "a2", requestId: "next-question" },
          confirmed,
        ),
      ).toThrow();
      expect(readReplyMarkers(store, marker)).toEqual([changed]);
    },
  );
  it("refuses storage loss before dispatch and malformed saved marker bindings", () => {
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
    expect(() => retainReplyMarker(broken, marker, new Set())).toThrow();
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
