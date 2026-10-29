/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it, vi } from "vitest";
import { request } from "./api";
import {
  generationTargetBody,
  submitRetainedGenerationTarget,
  readGenerationTarget,
  readPendingFrameworkRefresh,
  FrameworkRefreshUncertain,
  type FrameworkStatus,
  type FrameworkCorpus,
} from "./FrameworkKnowledgeData";
import {
  corpus,
  completeCounts,
  discovery,
} from "../tests/framework-knowledge-fixture";
vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  request: vi.fn(),
}));
const connection = { base: "/api/v1", token: "" };
const status: FrameworkStatus = {
  ...discovery.status,
  current_corpus: corpus,
  counts: completeCounts,
  state: "indexed",
  allowed_actions: ["activate_generation_target"],
};
const runId = "6".repeat(32);
const attempt = {
  activation: true as const,
  framework: corpus.framework,
  request_id: "7".repeat(32),
  corpus,
  body: {
    version: 1 as const,
    corpus_ref: corpus.corpus_ref,
    expected_status_revision: status.revision,
    validation_run_id: runId,
  },
};
const receipt = {
  version: 1,
  request_id: attempt.request_id,
  framework: corpus.framework,
  corpus,
  validation_run_id: runId,
  validation_source_tree: "8".repeat(40),
  validation_digest: "9".repeat(64),
  activation_actor_digest: "a".repeat(64),
  activated_at: "2026-10-26T12:00:00Z",
  revision: "b".repeat(64),
};
function retainedStorage() {
  const saved = new Map<string, string>();
  return {
    getItem: (key: string) => saved.get(key) ?? null,
    setItem: (key: string, value: string) => {
      saved.set(key, value);
    },
    removeItem: (key: string) => {
      saved.delete(key);
    },
  };
}
it("binds explicit generation review to a ready exact corpus, native validation run and current status", () => {
  expect(generationTargetBody(status, corpus, runId)).toEqual(attempt.body);
  const invalid: [FrameworkStatus, FrameworkCorpus, string][] = [
    [{ ...status, allowed_actions: [] }, corpus, runId],
    [status, { ...corpus, state: "indexing" }, runId],
    [status, { ...corpus, framework: "larafly" }, runId],
    [status, corpus, "not-a-native-run"],
    [{ ...status, revision: "stale" }, corpus, runId],
  ];
  for (const [value, selected, run] of invalid)
    expect(() => generationTargetBody(value, selected, run)).toThrow();
});
it("persists an activation before POST and reads only its original identity after a lost response", async () => {
  const storage = retainedStorage();
  vi.mocked(request)
    .mockReset()
    .mockImplementation(async () => {
      expect(readPendingFrameworkRefresh(storage, "pending", "pyfly")).toEqual(
        attempt,
      );
      throw new Error("lost response");
    });
  await expect(
    submitRetainedGenerationTarget(connection, attempt, storage, "pending"),
  ).rejects.toBeInstanceOf(FrameworkRefreshUncertain);
  await expect(
    submitRetainedGenerationTarget(
      connection,
      { ...attempt, request_id: "c".repeat(32) },
      storage,
      "pending",
    ),
  ).rejects.toThrow();
  expect(request).toHaveBeenCalledTimes(1);
  expect(vi.mocked(request).mock.calls[0][1]).toBe(
    `/knowledge/frameworks/pyfly/generation-targets/${attempt.request_id}`,
  );
  vi.mocked(request).mockResolvedValue(receipt);
  expect(await readGenerationTarget(connection, attempt)).toEqual(receipt);
  expect(vi.mocked(request).mock.calls[1]).toEqual([
    connection,
    `/knowledge/frameworks/pyfly/generation-targets/${attempt.request_id}`,
    {},
  ]);
});
it("rejects cross-run, cross-corpus and changed immutable activation receipts", async () => {
  for (const changed of [
    { ...receipt, validation_run_id: "d".repeat(32) },
    { ...receipt, corpus: { ...corpus, corpus_ref: "d".repeat(32) } },
    { ...receipt, corpus: { ...corpus, commit: "d".repeat(40) } },
    { ...receipt, corpus: { ...corpus, manifest_sha256: "d".repeat(64) } },
    { ...receipt, validation_digest: "invalid" },
  ]) {
    vi.mocked(request).mockReset().mockResolvedValue(changed);
    await expect(readGenerationTarget(connection, attempt)).rejects.toThrow();
  }
});
it("does not dispatch activation if durable storage cannot retain the original body", async () => {
  vi.mocked(request).mockReset();
  const storage = {
    ...retainedStorage(),
    setItem: () => {
      throw new Error("storage unavailable");
    },
  };
  await expect(
    submitRetainedGenerationTarget(connection, attempt, storage, "pending"),
  ).rejects.toThrow();
  expect(request).not.toHaveBeenCalled();
});
