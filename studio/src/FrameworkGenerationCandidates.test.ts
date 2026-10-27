/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it, vi } from "vitest";
import { request } from "./api";
import { frameworkGenerationCandidates } from "./FrameworkGenerationCandidates";
import { corpus } from "../tests/framework-knowledge-fixture";

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  request: vi.fn(),
}));
const connection = { base: "/api/v1", token: "" };
const candidate = {
  validation_run_id: "1".repeat(32),
  repository_name: "inventory-service",
  project_id: "2".repeat(32),
  component_id: "api",
  created_at: "2026-10-26T09:00:00Z",
  source_tree: "4".repeat(40),
  runtime: { runtime: "python", version: "3.13.13" },
  validation_digest: "5".repeat(64),
};
const page = {
  version: 1,
  framework: "pyfly",
  corpus_ref: corpus.corpus_ref,
  items: [candidate],
  next_cursor: null,
};

it("scopes candidate pages to the exact corpus and continues an empty page until the null cursor", async () => {
  const signal = new AbortController().signal;
  vi.mocked(request)
    .mockReset()
    .mockResolvedValue({
      ...page,
      items: [],
      next_cursor: "6".repeat(32),
    });
  expect(
    await frameworkGenerationCandidates(connection, corpus, null, signal),
  ).toEqual({ ...page, items: [], next_cursor: "6".repeat(32) });
  expect(request).toHaveBeenLastCalledWith(
    connection,
    `/knowledge/frameworks/pyfly/generation-candidates?corpus_ref=${"a".repeat(32)}&page_size=50`,
    { signal },
  );
  vi.mocked(request).mockResolvedValue(page);
  expect(
    await frameworkGenerationCandidates(connection, corpus, "6".repeat(32)),
  ).toEqual(page);
  expect(request).toHaveBeenLastCalledWith(
    connection,
    `/knowledge/frameworks/pyfly/generation-candidates?corpus_ref=${"a".repeat(32)}&page_size=50&cursor=${"6".repeat(32)}`,
    { signal: undefined },
  );
});

it("rejects mismatched scope and malformed validation evidence before offering a run", async () => {
  for (const value of [
    { ...page, framework: "larafly" },
    { ...page, corpus_ref: "7".repeat(32) },
    { ...page, next_cursor: "invalid" },
    { ...page, version: 2 },
    { ...page, items: [{ ...candidate, validation_run_id: "invalid" }] },
    { ...page, items: [{ ...candidate, source_tree: "invalid" }] },
    { ...page, items: [{ ...candidate, validation_digest: "invalid" }] },
    { ...page, items: [{ ...candidate, created_at: "invalid" }] },
    {
      ...page,
      items: [{ ...candidate, runtime: { runtime: "php", version: "8.5.12" } }],
    },
    {
      ...page,
      items: [{ ...candidate, runtime: { runtime: "python", version: "3" } }],
    },
  ]) {
    vi.mocked(request).mockReset().mockResolvedValue(value);
    await expect(
      frameworkGenerationCandidates(connection, corpus),
    ).rejects.toThrow();
  }
});
