/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ForgeRun } from "./api";
const hooks = vi.hoisted(() => ({
  effects: [] as Array<() => void | (() => void)>,
}));
vi.mock("react", async (original) => {
  const actual = await original<typeof import("react")>();
  return {
    ...actual,
    useEffect: (...args: Parameters<typeof actual.useEffect>) => {
      actual.useEffect(...args);
      hooks.effects.push(args[0]);
    },
  };
});
import { RunVerification } from "./RunVerification";
afterEach(() => {
  hooks.effects = [];
  vi.unstubAllGlobals();
});
it("reads handoff evidence with the current authenticated GET and never dispatches a write", async () => {
  const run: ForgeRun = {
    id: "a".repeat(32),
    repository_id: "b".repeat(32),
    brief: "Retained checks",
    status: "failed",
    verification_handoff: { id: "c".repeat(32) },
  };
  const calls: Array<{
    url: string;
    method?: string;
    credentials?: RequestCredentials;
    headers: Record<string, string>;
  }> = [];
  vi.stubGlobal("fetch", async (url: string, options: RequestInit) => {
    calls.push({
      url,
      method: options.method,
      credentials: options.credentials,
      headers: options.headers as Record<string, string>,
    });
    return new Response(JSON.stringify({ valid: true, entries: [] }));
  });
  renderToStaticMarkup(
    <RunVerification
      run={run}
      connection={{
        base: "https://forge.example/api/v1",
        token: "public-test-token",
      }}
    />,
  );
  const dispose = hooks.effects[0]();
  await vi.waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toMatchObject({
    url: `https://forge.example/api/v1/runs/${run.id}/journal`,
    credentials: "include",
    headers: { Authorization: "Bearer public-test-token" },
  });
  expect(calls.every(({ method }) => !method || method === "GET")).toBe(true);
  if (typeof dispose === "function") dispose();
});
