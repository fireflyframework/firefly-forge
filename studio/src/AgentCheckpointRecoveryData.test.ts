/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it, vi } from "vitest";
import {
  candidatePage,
  discoveredReceipt,
  markerKey,
  readMarker,
  sendOnce,
  observationReceipt,
  restoreReceipt,
  type RecoveryMarker,
} from "./AgentCheckpointRecoveryData";
export const context = {
  base: "https://factory.example/api/v1",
  actor: "1".repeat(32),
  project: "2".repeat(32),
  repository: "3".repeat(32),
  run: "4".repeat(32),
};
export const candidate = {
  completion_id: "fbr1_" + "5".repeat(32),
  checkpoint_digest: "a".repeat(64),
  intent_digest: "b".repeat(64),
  task: "implement",
  attempt: 1,
  ordinal: 2,
  deadline_ms: 1893456000000,
  created_at: "2026-10-17T00:00:00Z",
  state: "ready",
  result: {
    kind: "http_response",
    record_digest: "c".repeat(64),
    observation_id: null,
    terminal_digest: null,
  },
  observation: null,
  ticket: null,
};
export const page = {
  version: 1,
  actor_id: context.actor,
  run_id: context.run,
  repository_id: context.repository,
  project_id: context.project,
  state: "available",
  items: [candidate],
};
export const marker: RecoveryMarker = {
  version: 1,
  context,
  completion: candidate.completion_id,
  checkpoint: candidate.checkpoint_digest,
  intent: candidate.intent_digest,
  kind: "observation",
  request: "6".repeat(32),
  source: null,
};
function storage() {
  const rows = new Map<string, string>();
  return {
    getItem: (key: string) => rows.get(key) ?? null,
    setItem: (key: string, value: string) => {
      rows.set(key, value);
    },
  };
}
it("accepts only bounded allowlisted candidates bound to the current identity", () => {
  expect(candidatePage(page, context).items).toHaveLength(1);
  for (const change of [
    { actor_id: "9".repeat(32) },
    { state: "eligible" },
    { items: Array(17).fill(candidate) },
    { items: [{ ...candidate, response: "private" }] },
    { items: [{ ...candidate, attempt: true }] },
    { items: [{ ...candidate, state: "ready", result: null }] },
    { items: [{ ...candidate, ticket: { id: "bad", status: "queued" } }] },
  ])
    expect(() => candidatePage({ ...page, ...change }, context)).toThrow();
});
it("accepts an open work deadline only under the explicit owned checkpoint protocol", () => {
  const owned = {
    ...candidate,
    deadline_ms: null,
    invocation_protocol: "forge-owned-checkpoint-v3",
    work_duration: {
      protocol: "forge-work-duration-v2",
      timeout_seconds: null,
    },
  };
  expect(candidatePage({ ...page, items: [owned] }, context).items[0]).toEqual(
    owned,
  );
  expect(candidatePage(page, context).items[0]).toEqual(candidate);
  for (const change of [
    { ...candidate, deadline_ms: null },
    { ...owned, invocation_protocol: "unknown" },
    {
      ...owned,
      work_duration: {
        protocol: "forge-work-duration-v2",
        timeout_seconds: 60,
      },
    },
    {
      ...owned,
      work_duration: {
        protocol: "forge-work-duration-v2",
        timeout_seconds: null,
        unexpected: true,
      },
    },
  ])
    expect(() =>
      candidatePage({ ...page, items: [change] }, context),
    ).toThrow();
});
it("stores and reads the exact original ID before POST; lost responses cannot create another admission", async () => {
  const saved = storage();
  const send = vi.fn(async () => {
    expect(readMarker(saved.getItem(markerKey(context)), context)).toEqual(
      marker,
    );
    throw new Error("lost response");
  });
  await expect(sendOnce(saved, marker, send)).rejects.toThrow("lost response");
  expect(send).toHaveBeenCalledTimes(1);
  expect(readMarker(saved.getItem(markerKey(context)), context)).toEqual(
    marker,
  );
  await expect(
    sendOnce(saved, { ...marker, request: "7".repeat(32) }, send),
  ).rejects.toThrow();
  expect(send).toHaveBeenCalledTimes(1);
});
it("refuses broken storage, malformed markers and context changes before any POST", async () => {
  const send = vi.fn();
  await expect(
    sendOnce({ getItem: () => null, setItem: () => {} }, marker, send),
  ).rejects.toThrow();
  expect(send).not.toHaveBeenCalled();
  for (const change of [
    { token: "secret" },
    { request: "bad" },
    { source: candidate.result },
  ])
    expect(() =>
      readMarker(JSON.stringify({ ...marker, ...change }), context),
    ).toThrow();
  expect(() =>
    readMarker(JSON.stringify(marker), { ...context, actor: "9".repeat(32) }),
  ).toThrow();
});
it("only replaces a previous exact confirmed observation marker", async () => {
  const saved = storage(),
    send = vi.fn(async () => ({}));
  await sendOnce(saved, marker, send);
  await expect(
    sendOnce(saved, { ...marker, request: "7".repeat(32) }, send, {
      marker: { ...marker, request: "8".repeat(32) },
      state: "pending",
    }),
  ).rejects.toThrow();
  await sendOnce(saved, { ...marker, request: "7".repeat(32) }, send, {
    marker,
    state: "pending",
  });
  expect(send).toHaveBeenCalledTimes(2);
});
it("validates observation identity without exposing native response text", () => {
  const receipt = {
    run_id: context.run,
    idempotency_key: marker.request,
    kind: "bridge_request_observation",
    digest: "d".repeat(64),
    task_id: "implement",
    payload: {
      request_id: marker.completion,
      intent_digest: marker.intent,
      observed_by: context.actor,
      input: {
        version: 1,
        observation_id: marker.request,
        expected_intent_digest: marker.intent,
      },
      snapshot: { state: "pending" },
    },
  };
  expect(observationReceipt(receipt, marker)).toBe("pending");
  expect(() =>
    observationReceipt({ ...receipt, idempotency_key: "9".repeat(32) }, marker),
  ).toThrow();
});
it("requires exact original restore request/source and reports receipt status separately", () => {
  const m: RecoveryMarker = {
    ...marker,
    kind: "restore",
    source: candidate.result as RecoveryMarker["source"],
  };
  const input = {
    request_id: m.request,
    expected_checkpoint_digest: m.checkpoint,
    expected_intent_digest: m.intent,
    source: m.source,
  };
  const receipt = {
    id: m.request,
    run_id: context.run,
    kind: "agent_restore",
    status: "completed",
    request: {
      run_id: context.run,
      completion_request_id: m.completion,
      actor: { user_id: context.actor },
      input,
    },
  };
  expect(restoreReceipt(receipt, m)).toBe("completed");
  for (const change of [
    { status: "succeeded" },
    {
      request: {
        ...receipt.request,
        input: { ...input, request_id: "9".repeat(32) },
      },
    },
  ])
    expect(() => restoreReceipt({ ...receipt, ...change }, m)).toThrow();
});
it("a completed original ticket can be retained while a different boundary gets its own request", async () => {
  const saved = storage(),
    send = vi.fn(async () => ({}));
  const original: RecoveryMarker = {
    ...marker,
    kind: "restore",
    source: candidate.result as RecoveryMarker["source"],
  };
  await sendOnce(saved, original, send);
  await expect(
    sendOnce(saved, { ...original, request: "7".repeat(32) }, send, {
      marker: original,
      state: "completed",
    }),
  ).rejects.toThrow();
  await sendOnce(
    saved,
    {
      ...original,
      completion: "fbr1_" + "8".repeat(32),
      request: "7".repeat(32),
    },
    send,
    { marker: original, state: "completed" },
  );
  expect(send).toHaveBeenCalledTimes(2);
});

it("reads a server-discovered ticket from another operator only when its original candidate bindings match", () => {
  const c = { ...candidate, ticket: { id: "9".repeat(32), status: "queued" } };
  const receipt = {
    id: c.ticket.id,
    run_id: context.run,
    kind: "agent_restore",
    status: "completed",
    request: {
      run_id: context.run,
      completion_request_id: c.completion_id,
      actor: { user_id: "8".repeat(32) },
      input: {
        request_id: c.ticket.id,
        expected_checkpoint_digest: c.checkpoint_digest,
        expected_intent_digest: c.intent_digest,
        source: c.result,
      },
    },
  };
  expect(
    discoveredReceipt(
      receipt,
      c as Parameters<typeof discoveredReceipt>[1],
      context,
    ),
  ).toBe("completed");
  expect(() =>
    discoveredReceipt(
      { ...receipt, id: "7".repeat(32) },
      c as Parameters<typeof discoveredReceipt>[1],
      context,
    ),
  ).toThrow();
});
