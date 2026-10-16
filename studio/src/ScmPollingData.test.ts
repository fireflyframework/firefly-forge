/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import fixture from "../tests/scm-polling-contract-fixture.json";
import { attemptDigest } from "./LearningObservationAttemptData";
import { readPollingBindingReceipt } from "./ScmPollingData";
import { workerAttributionSummary, workerThreads } from "./WorkerDialogueData";
const scope = {
  project: fixture.receipt.project_id,
  repository: fixture.receipt.repository_id,
  requestId: fixture.receipt.request.request_id,
  receiptDigest: fixture.receipt.digest,
};
async function seal<T extends Record<string, unknown>>(body: T) {
  return { ...body, digest: await attemptDigest(body) };
}
async function events(): Promise<Record<string, unknown>[]> {
  const run = "9".repeat(32);
  const common = {
    run_id: run,
    task_id: "implement",
    created_at: "2026-10-16T12:00:00+00:00",
    previous_hash: "a".repeat(64),
  };
  return [
    await seal({
      ...common,
      sequence: 1,
      idempotency_key: "worker-feedback:q1",
      kind: "worker_feedback",
      payload: {
        id: "q1",
        role_id: "implementer",
        task_id: "implement",
        attempt: 1,
        disposition: "clarification",
        summary: "Confirm scope",
        evidence_paths: [],
        request: {
          id: "question1",
          kind: "product_definition",
          question: "Preserve contacts?",
          context: "Existing source",
          choices: ["Yes"],
        },
      },
    }),
    await seal({
      ...common,
      sequence: 2,
      idempotency_key: "worker-response:a1",
      kind: "worker_response",
      payload: {
        id: "a1",
        actor: "7",
        answer: "Yes",
        request_id: "question1",
        responding_actor: fixture.responding_actor,
      },
    }),
  ];
}
it("validates the exact model-built REST polling receipt", async () => {
  expect(await readPollingBindingReceipt(fixture.receipt, scope)).toEqual(
    fixture.receipt,
  );
});
it("renders historical REST attribution with an exact historical read and no current-head requirement", async () => {
  const history = await events();
  const reads: string[] = [];
  const parsed = await workerThreads(history, "9".repeat(32), {
    ...scope,
    component: "api",
    readBinding: async () => {
      throw Error("V1 must not be read");
    },
    readPollingBinding: async (id) => {
      reads.push(id);
      return fixture.receipt;
    },
  });
  expect(reads).toEqual([scope.requestId]);
  expect(workerAttributionSummary(parsed[0].response!)).toBe(
    "Delegated through an API-observed GitHub account to a mapped forge user.",
  );
});
it("rejects a fully resigned inactive original rather than certifying historical attribution", async () => {
  const value = structuredClone(fixture.receipt);
  value.request.active = false;
  value.request_digest = await attemptDigest(value.request);
  const { digest: _digest, ...body } = value;
  void _digest;
  const changed = await seal(body);
  const history = await events();
  const answer = history[1].payload as Record<string, unknown>;
  const actor = structuredClone(fixture.responding_actor);
  actor.provenance.binding_digest = changed.digest;
  history[1] = await seal({
    ...history[1],
    payload: { ...answer, responding_actor: actor },
  });
  await expect(
    workerThreads(history, "9".repeat(32), {
      ...scope,
      component: "api",
      readBinding: async () => fixture.receipt,
      readPollingBinding: async () => changed,
    }),
  ).rejects.toThrow();
});
it("rejects another receipt scope, source proof or extra field", async () => {
  await expect(
    readPollingBindingReceipt(fixture.receipt, {
      ...scope,
      project: "a".repeat(32),
    }),
  ).rejects.toThrow();
  await expect(
    readPollingBindingReceipt({ ...fixture.receipt, current: true }, scope),
  ).rejects.toThrow();
  const value = structuredClone(fixture.receipt);
  value.verified_reference.external_actor_id = "8";
  const { digest: _digest, ...body } = value;
  void _digest;
  await expect(
    readPollingBindingReceipt(await seal(body), {
      ...scope,
      receiptDigest: undefined,
    }),
  ).rejects.toThrow();
});
