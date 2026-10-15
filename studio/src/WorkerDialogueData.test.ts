/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { attemptDigest } from "./LearningObservationAttemptData";
import contract from "../tests/worker-dialogue-contract-fixture.json";
import {
  confirmReplyMarkers,
  normalizeWorkerText,
  readReplyMarkers,
  replyMarkerBinding,
  retainReplyMarker,
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
    kind: "worker_response",
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
  it("accepts the actual Python model and UTF-8 journal digest vectors", async () => {
    const threads = await workerThreads(
      [contract.feedback, contract.legacy_response],
      run,
    );
    expect(threads[0].question.id).toBe("reservation-limit");
    expect(threads[0].response?.payload.answer).toBe("14 items");
  });
  it("normalizes exactly like Python strip while preserving FEFF, emoji and real newlines", () => {
    expect(normalizeWorkerText("\u001c\u0085 A\nB \u001f")).toBe("A\nB");
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
