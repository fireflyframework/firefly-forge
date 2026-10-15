/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  candidateRead,
  continuationKey,
  retainContinuation,
  readContinuationMarker,
  continuationReceipt,
} from "./RunnerContinuationData";
const context = {
  base: "https://factory.example/api/v1",
  actor: "1".repeat(32),
  run: "2".repeat(32),
  repository: "3".repeat(32),
  project: null,
};
const command = {
  execution_id: "4".repeat(32),
  task_id: "verify" as const,
  attempt: 1,
  command_label: "Git command",
  argument_count: 1,
  source_digest: "a".repeat(64),
  invocation_digest: "b".repeat(64),
  wal_digest: "c".repeat(64),
  binding_digest: "d".repeat(64),
};
const packet = {
  version: 1,
  run_id: context.run,
  repository_id: context.repository,
  project_id: null,
  actor_id: context.actor,
  state: "requires_leader_confirmation",
  reason: "Leader confirmation required.",
  commands: [command],
};
function storage() {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
  };
}
it("accepts only bound local candidates and fixed safe command labels", () => {
  expect(candidateRead(packet, context).commands).toEqual([command]);
  for (const change of [
    { actor_id: "9".repeat(32) },
    { run_id: "9".repeat(32) },
    { state: "eligible" },
    { commands: [{ ...command, argv: ["secret"] }] },
    { commands: [{ ...command, command_label: "/private/secret" }] },
    { commands: [{ ...command, attempt: true }] },
  ])
    expect(() => candidateRead({ ...packet, ...change }, context)).toThrow();
});
it("persists only exact identity digests before dispatch and refuses overwrites or failed storage", () => {
  const saved = storage(),
    marker = retainContinuation(saved, context, command, "5".repeat(32));
  expect(
    readContinuationMarker(saved.getItem(continuationKey(context)), context),
  ).toEqual(marker);
  expect(Object.keys(marker).sort()).toEqual(
    [
      "version",
      "base",
      "actor",
      "run",
      "repository",
      "project",
      "execution",
      "request",
      "task",
      "attempt",
      "sourceDigest",
      "invocationDigest",
      "walDigest",
      "bindingDigest",
    ].sort(),
  );
  expect(() =>
    retainContinuation(saved, context, command, "6".repeat(32)),
  ).toThrow();
  expect(() =>
    retainContinuation(
      { getItem: () => null, setItem: () => {} },
      context,
      command,
      "5".repeat(32),
    ),
  ).toThrow();
  expect(() =>
    readContinuationMarker(
      JSON.stringify({ ...marker, token: "secret" }),
      context,
    ),
  ).toThrow();
});
it("binds every exact receipt anchor without storing private run binding or controller output", () => {
  const marker = retainContinuation(
    storage(),
    context,
    command,
    "5".repeat(32),
  );
  const receipt = {
    id: marker.request,
    run_id: marker.run,
    kind: "runner_continue",
    status: "completed",
    request_digest: marker.bindingDigest,
    request: {
      run_id: marker.run,
      execution_id: marker.execution,
      task_id: marker.task,
      attempt: marker.attempt,
      wal_digest: marker.walDigest,
      invocation_digest: marker.invocationDigest,
      actor: { user_id: marker.actor },
      run_binding: { repository_id: marker.repository, project_id: null },
    },
    result: { disposition: "accepted", private: "untrusted" },
  };
  expect(continuationReceipt(receipt, marker)).toEqual({
    status: "completed",
    accepted: true,
  });
  for (const field of [
    "execution_id",
    "task_id",
    "attempt",
    "wal_digest",
    "invocation_digest",
  ])
    expect(() =>
      continuationReceipt(
        { ...receipt, request: { ...receipt.request, [field]: "wrong" } },
        marker,
      ),
    ).toThrow();
  for (const change of [
    { id: "6".repeat(32) },
    { request_digest: "e".repeat(64) },
    { kind: "other" },
    { run_id: "6".repeat(32) },
  ])
    expect(() =>
      continuationReceipt({ ...receipt, ...change }, marker),
    ).toThrow();
  expect(
    continuationReceipt(
      { ...receipt, status: "needs_reconciliation", result: null },
      marker,
    ),
  ).toEqual({ status: "needs_reconciliation", accepted: false });
});
