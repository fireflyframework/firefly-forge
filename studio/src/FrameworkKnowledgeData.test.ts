/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it, vi } from "vitest";
import { request } from "./api";
import {
  corpusSelectionKey,
  refreshBody,
  submitFrameworkRefresh,
  readFrameworkOperation,
  frameworkSearch,
  frameworkSources,
  frameworkCorpora,
  FrameworkRefreshUncertain,
  submitRetainedFrameworkRefresh,
  readPendingFrameworkRefresh,
  submitFrameworkControl,
  type FrameworkOperation,
} from "./FrameworkKnowledgeData";
import {
  corpus,
  counts,
  discovery,
} from "../tests/framework-knowledge-fixture";

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  request: vi.fn(),
}));
const connection = { base: "/api/v1", token: "" };

it("binds refresh to discovered release, current revision and policy", () => {
  expect(refreshBody(discovery, discovery.status)).toEqual({
    version: 1,
    discovery_digest: "1".repeat(64),
    expected_status_revision: "2".repeat(64),
    policy_digest: "e".repeat(64),
    make_current: true,
  });
});

it("refuses stale discovery, another framework and unadvertised refresh", () => {
  for (const status of [
    { ...discovery.status, revision: "3".repeat(64) },
    { ...discovery.status, framework: "larafly" as const },
    { ...discovery.status, allowed_actions: [] },
  ])
    expect(() => refreshBody(discovery, status)).toThrow();
});

it("retains a lost-response request without retrying", async () => {
  vi.mocked(request)
    .mockReset()
    .mockRejectedValue(new Error("connection lost"));
  const attempt = {
    framework: "pyfly" as const,
    request_id: "4".repeat(32),
    body: refreshBody(discovery, discovery.status),
  };
  const error = await submitFrameworkRefresh(connection, attempt).catch(
    (value: unknown) => value,
  );
  expect(error).toBeInstanceOf(FrameworkRefreshUncertain);
  expect((error as FrameworkRefreshUncertain).attempt).toEqual(attempt);
  expect(request).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledWith(
    connection,
    `/knowledge/frameworks/pyfly/refreshes/${attempt.request_id}`,
    { method: "POST", body: JSON.stringify(attempt.body) },
  );
});

it("reads the original request and rejects a mismatched receipt", async () => {
  vi.mocked(request)
    .mockReset()
    .mockResolvedValue({ framework: "pyfly", request_id: "5".repeat(32) });
  await expect(
    readFrameworkOperation(connection, "pyfly", "4".repeat(32)),
  ).rejects.toThrow();
  expect(request).toHaveBeenCalledTimes(1);
  expect(vi.mocked(request).mock.calls[0][1]).toBe(
    `/knowledge/frameworks/pyfly/refreshes/${"4".repeat(32)}`,
  );
});

it("keys results by factory, user and immutable corpus", () => {
  const key = corpusSelectionKey(connection, "user-a", corpus);
  expect(key).not.toBe(
    corpusSelectionKey(
      { ...connection, base: "https://other.example/api/v1" },
      "user-a",
      corpus,
    ),
  );
  expect(key).not.toBe(corpusSelectionKey(connection, "user-b", corpus));
  expect(key).not.toBe(
    corpusSelectionKey(connection, "user-a", {
      ...corpus,
      corpus_ref: "6".repeat(32),
    }),
  );
});

it("persists one request before POST and refuses replacement submission after reload", async () => {
  const saved = new Map<string, string>();
  const storage = {
    getItem: (key: string) => saved.get(key) ?? null,
    setItem: (key: string, value: string) => {
      saved.set(key, value);
    },
    removeItem: (key: string) => {
      saved.delete(key);
    },
  };
  const attempt = {
    framework: "pyfly" as const,
    request_id: "4".repeat(32),
    body: refreshBody(discovery, discovery.status),
  };
  vi.mocked(request)
    .mockReset()
    .mockImplementation(async () => {
      expect(saved.has("pending")).toBe(true);
      throw new Error("lost");
    });
  await expect(
    submitRetainedFrameworkRefresh(connection, attempt, storage, "pending"),
  ).rejects.toBeInstanceOf(FrameworkRefreshUncertain);
  expect(readPendingFrameworkRefresh(storage, "pending", "pyfly")).toEqual(
    attempt,
  );
  await expect(
    submitRetainedFrameworkRefresh(
      connection,
      { ...attempt, request_id: "5".repeat(32) },
      storage,
      "pending",
    ),
  ).rejects.toThrow();
  expect(request).toHaveBeenCalledTimes(1);
  expect(
    readPendingFrameworkRefresh(storage, "pending", "pyfly")?.request_id,
  ).toBe(attempt.request_id);
});

it("does not dispatch when durable browser storage fails", async () => {
  vi.mocked(request).mockReset();
  const storage = {
    getItem: () => null,
    setItem: () => {
      throw new Error("storage full");
    },
    removeItem: () => {},
  };
  const attempt = {
    framework: "pyfly" as const,
    request_id: "4".repeat(32),
    body: refreshBody(discovery, discovery.status),
  };
  await expect(
    submitRetainedFrameworkRefresh(connection, attempt, storage, "pending"),
  ).rejects.toThrow("storage full");
  expect(request).not.toHaveBeenCalled();
});

function controlFixture() {
  const saved = new Map<string, string>();
  const storage = {
    getItem: (key: string) => saved.get(key) ?? null,
    setItem: (key: string, value: string) => {
      saved.set(key, value);
    },
    removeItem: (key: string) => {
      saved.delete(key);
    },
  };
  const operation: FrameworkOperation = {
    request_id: "4".repeat(32),
    framework: "pyfly",
    corpus,
    state: "needs_reconciliation",
    stage: "fetching",
    counts,
    spent_budget: { requests: 10, bytes: 1000, seconds: 5.5 },
    remaining_budget: { requests: 40, bytes: 9000, seconds: 94.5 },
    sanitized_error: null,
    uncertain_sources: 0,
    allowed_actions: ["renew_budget", "cancel", "resume"],
    revision: "5".repeat(64),
  };
  return { storage, operation };
}

it("renews new totals on the same operation with its exact displayed revision and retained spend", async () => {
  const { storage, operation } = controlFixture();
  const total = { requests: 100, bytes: 20000, seconds: 200 };
  vi.mocked(request).mockReset().mockResolvedValue(operation);
  await submitFrameworkControl(
    connection,
    operation,
    "renew_budget",
    storage,
    "pending",
    total,
  );
  expect(request).toHaveBeenCalledWith(
    connection,
    `/knowledge/frameworks/pyfly/refreshes/${operation.request_id}/budget`,
    {
      method: "POST",
      body: JSON.stringify({
        version: 1,
        expected_revision: operation.revision,
        total_budget: total,
      }),
    },
  );
});

it("refuses decreased or unchanged totals, unadvertised actions and uncertain resume before POST", async () => {
  const { storage, operation } = controlFixture();
  vi.mocked(request).mockReset();
  for (const total of [
    { requests: 50, bytes: 10000, seconds: 100 },
    { requests: 100, bytes: 500, seconds: 200 },
  ]) {
    await expect(
      submitFrameworkControl(
        connection,
        operation,
        "renew_budget",
        storage,
        "pending",
        total,
      ),
    ).rejects.toThrow();
  }
  await expect(
    submitFrameworkControl(
      connection,
      { ...operation, allowed_actions: [] },
      "cancel",
      storage,
      "pending",
    ),
  ).rejects.toThrow();
  await expect(
    submitFrameworkControl(
      connection,
      { ...operation, uncertain_sources: 1 },
      "resume",
      storage,
      "pending",
    ),
  ).rejects.toThrow();
  expect(request).not.toHaveBeenCalled();
  expect(storage.getItem("pending")).toBeNull();
});

it("retains an uncertain control body across reload and does not replace it with cancellation", async () => {
  const { storage, operation } = controlFixture();
  vi.mocked(request).mockReset().mockRejectedValue(new Error("lost"));
  await expect(
    submitFrameworkControl(
      connection,
      operation,
      "renew_budget",
      storage,
      "pending",
      { requests: 100, bytes: 20000, seconds: 200 },
    ),
  ).rejects.toBeInstanceOf(FrameworkRefreshUncertain);
  const retained = readPendingFrameworkRefresh(storage, "pending", "pyfly");
  expect(retained).toMatchObject({
    framework: "pyfly",
    request_id: operation.request_id,
    control: "renew_budget",
    body: { expected_revision: operation.revision },
  });
  await expect(
    submitFrameworkControl(connection, operation, "cancel", storage, "pending"),
  ).rejects.toThrow();
  expect(request).toHaveBeenCalledTimes(1);
});

it("does not present a response with reset spent usage as a successful allowance change", async () => {
  const { storage, operation } = controlFixture();
  vi.mocked(request)
    .mockReset()
    .mockResolvedValue({
      ...operation,
      spent_budget: { requests: 0, bytes: 0, seconds: 0 },
    });
  await expect(
    submitFrameworkControl(connection, operation, "cancel", storage, "pending"),
  ).rejects.toBeInstanceOf(FrameworkRefreshUncertain);
  expect(
    readPendingFrameworkRefresh(storage, "pending", "pyfly")?.request_id,
  ).toBe(operation.request_id);
});

it("searches only the selected ready corpus", async () => {
  vi.mocked(request).mockReset().mockResolvedValue({
    corpus,
    hits: [],
    citations: [],
    warnings: [],
    context: "",
  });
  await frameworkSearch(connection, corpus, "transaction test");
  expect(request).toHaveBeenCalledWith(
    connection,
    "/knowledge/frameworks/pyfly/search",
    {
      method: "POST",
      body: JSON.stringify({
        query: "transaction test",
        corpus_ref: corpus.corpus_ref,
      }),
      signal: undefined,
    },
  );
  await expect(
    frameworkSearch(connection, { ...corpus, state: "indexing" }, "test"),
  ).rejects.toThrow();
  expect(request).toHaveBeenCalledTimes(1);
});

it("rejects cross-version hits and a response for another corpus", async () => {
  const hit = {
    corpus_ref: corpus.corpus_ref,
    framework: "pyfly",
    framework_version: "26.10.6",
    commit: corpus.commit,
  };
  for (const response of [
    { corpus, hits: [hit], citations: [], warnings: [], context: "" },
    {
      corpus: { ...corpus, corpus_ref: "7".repeat(32) },
      hits: [],
      citations: [],
      warnings: [],
      context: "",
    },
  ]) {
    vi.mocked(request).mockResolvedValue(response);
    await expect(frameworkSearch(connection, corpus, "test")).rejects.toThrow();
  }
});

it("retains a source's nested license separately from the corpus root license", async () => {
  const hit = {
    source_ref: "1".repeat(32),
    corpus_ref: corpus.corpus_ref,
    framework: corpus.framework,
    framework_version: corpus.framework_version,
    release_tag: corpus.release_tag,
    commit: corpus.commit,
    source_id: "source",
    chunk_id: null,
    path: "vendor/example.py",
    source_uri: "https://example.test/immutable-file",
    source_sha256: "2".repeat(64),
    license_sha256: "3".repeat(64),
    knowledge_version: null,
    score: 0.8,
    content: "Example",
  };
  vi.mocked(request).mockResolvedValue({
    corpus,
    hits: [hit],
    citations: [hit],
    warnings: [],
    context: "",
  });
  const result = await frameworkSearch(connection, corpus, "example");
  expect(result.hits[0].license_sha256).toBe("3".repeat(64));
  expect(result.corpus.license_sha256).toBe(corpus.license_sha256);
});

it("keeps opaque cursors and complete totals without capping corpus membership", async () => {
  vi.mocked(request).mockReset().mockResolvedValue({
    items: [],
    next_cursor: "next/opaque+page",
    total: 2759,
  });
  const page = await frameworkSources(connection, corpus, "cursor/opaque+page");
  expect(page.total).toBe(2759);
  expect(page.next_cursor).toBe("next/opaque+page");
  expect(vi.mocked(request).mock.calls[0][1]).toContain(
    "cursor=cursor%2Fopaque%2Bpage",
  );
});

it("rejects a foreign source page and a foreign framework corpus page", async () => {
  vi.mocked(request).mockResolvedValue({
    items: [{ corpus_ref: "7".repeat(32) }],
    next_cursor: null,
    total: 1,
  });
  await expect(frameworkSources(connection, corpus)).rejects.toThrow();
  vi.mocked(request).mockResolvedValue({
    items: [{ ...corpus, framework: "larafly" }],
    next_cursor: null,
    total: 1,
  });
  await expect(frameworkCorpora(connection, "pyfly")).rejects.toThrow();
});
