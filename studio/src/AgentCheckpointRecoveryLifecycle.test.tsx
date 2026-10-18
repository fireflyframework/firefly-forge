/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import type { IdentitySession } from "./Identity";

// The retained dependencies have no DOM environment. Model the committed effect
// setup/cleanup/setup sequence using the real component, requests and handlers.
// This is not a mounted React DOM or browser acceptance test.
const hooks = vi.hoisted(() => ({
  effects: [] as Array<() => void | (() => void)>,
  states: [] as Array<{ value: unknown; writes: number }>,
}));
vi.mock("react", async (original) => {
  const actual = await original<typeof import("react")>();
  return {
    ...actual,
    useRef: (initial: unknown) => ({ current: initial }),
    useEffect: (effect: () => void | (() => void)) => {
      hooks.effects.push(effect);
    },
    useState: (initial: unknown) => {
      const cell = {
        value: typeof initial === "function" ? initial() : initial,
        writes: 0,
      };
      hooks.states.push(cell);
      return [
        cell.value,
        (update: unknown) => {
          cell.value =
            typeof update === "function" ? update(cell.value) : update;
          cell.writes++;
        },
      ];
    },
  };
});
import { AgentCheckpointRecovery } from "./AgentCheckpointRecovery";
const actor = "1".repeat(32),
  project = "2".repeat(32),
  repository = "3".repeat(32),
  run = "4".repeat(32);
const session = {
  authenticated: true,
  user: { id: actor },
  global_admin: true,
  memberships: [],
} as unknown as IdentitySession;
const candidate = {
  completion_id: "fbr1_" + "5".repeat(32),
  checkpoint_digest: "a".repeat(64),
  intent_digest: "b".repeat(64),
  task: "implement",
  attempt: 1,
  ordinal: 2,
  deadline_ms: 1893456000000,
  created_at: "2026-10-17T00:00:00Z",
  state: "awaiting_result",
  result: null,
  observation: null,
  ticket: null,
};
function packet(state = "awaiting_result") {
  return {
    version: 1,
    actor_id: actor,
    run_id: run,
    project_id: project,
    repository_id: repository,
    state: "available",
    items: [{ ...candidate, state }],
  };
}
function button(value: unknown, text: string): (() => void) | null {
  if (!value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (const child of value) {
      const found = button(child, text);
      if (found) return found;
    }
    return null;
  }
  const node = value as ReactElement<{
    children?: unknown;
    onClick?: () => void;
  }>;
  if (
    node.type === "button" &&
    Array.isArray(node.props.children) &&
    node.props.children.includes(text)
  )
    return node.props.onClick ?? null;
  return node.props ? button(node.props.children, text) : null;
}
function setup() {
  const pending: Array<{
    resolve: (response: Response) => void;
    signal: AbortSignal;
  }> = [];
  const fetch = vi.fn((_url: string, options: RequestInit) => {
    // Settle aborted reads late to exercise the real stale-result fence.
    return new Promise<Response>((resolve) => {
      pending.push({ resolve, signal: options.signal as AbortSignal });
    });
  });
  const setItem = vi.fn();
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("sessionStorage", { getItem: () => null, setItem });
  const tree = AgentCheckpointRecovery({
    run: {
      id: run,
      repository_id: repository,
      project_id: project,
      agent: "bridge",
      status: "needs_reconciliation",
      brief: "Fictional lifecycle control",
    },
    connection: { base: "https://factory.example/api/v1", token: "fictional" },
    session,
  });
  // These are the component's hook slots; no recovery behavior is duplicated.
  expect(hooks.states).toHaveLength(12);
  const items = hooks.states[3],
    busy = hooks.states[6];
  expect(items.value).toEqual([]);
  expect(busy.value).toBe(false);
  const mount = () => hooks.effects.map((effect) => effect());
  const dispose = (cleanups: ReturnType<typeof mount>) => {
    cleanups.forEach((cleanup) => cleanup?.());
  };
  return { pending, fetch, setItem, tree, items, busy, mount, dispose };
}
const respond = (value: unknown, read?: () => void) => {
  const response = new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
  });
  const text = response.text.bind(response);
  response.text = async () => {
    const contents = await text();
    read?.();
    return contents;
  };
  return response;
};
async function settle(
  pending: { resolve: (response: Response) => void },
  value: unknown,
) {
  let read = false;
  pending.resolve(
    respond(value, () => {
      read = true;
    }),
  );
  // Observe the response boundary and let its promise chain finish before
  // asserting that stale work had no state or single-flight effects.
  await vi.waitFor(() => expect(read).toBe(true));
}
beforeEach(() => {
  hooks.effects = [];
  hooks.states = [];
});
afterEach(() => vi.unstubAllGlobals());

it("replays initial discovery without manual refresh and ignores the cancelled setup's late result", async () => {
  const s = setup(),
    first = s.mount();
  expect(s.fetch).toHaveBeenCalledTimes(1);
  s.dispose(first);
  const current = s.mount();
  expect(s.pending[0].signal.aborted).toBe(true);
  expect(s.fetch).toHaveBeenCalledTimes(2);
  expect(s.pending[1].signal.aborted).toBe(false);
  s.pending[1].resolve(respond(packet()));
  await vi.waitFor(() => expect(s.items.value).toEqual([candidate]));
  expect(s.busy.value).toBe(false);
  await settle(s.pending[0], packet("held"));
  expect(s.items.value).toEqual([candidate]);
  expect(
    s.fetch.mock.calls.every(
      ([, options]) => (options.method ?? "GET") === "GET",
    ),
  ).toBe(true);
  expect(s.setItem).not.toHaveBeenCalled();
  s.dispose(current);
});

it("an obsolete read cannot clear a newer read's busy or single-flight state", async () => {
  const s = setup();
  s.dispose(s.mount());
  const current = s.mount();
  expect(s.fetch).toHaveBeenCalledTimes(2);
  await settle(s.pending[0], packet("held"));
  expect(s.busy.value).toBe(true);
  const refresh = button(s.tree, "Refresh saved result");
  expect(refresh).not.toBeNull();
  refresh!();
  expect(s.fetch).toHaveBeenCalledTimes(2);
  s.pending[1].resolve(respond(packet()));
  await vi.waitFor(() => expect(s.busy.value).toBe(false));
  refresh!();
  expect(s.fetch).toHaveBeenCalledTimes(3);
  expect(s.busy.value).toBe(true);
  s.dispose(current);
  const writes = hooks.states.map((state) => state.writes);
  await settle(s.pending[2], packet("held"));
  expect(hooks.states.map((state) => state.writes)).toEqual(writes);
  expect(s.setItem).not.toHaveBeenCalled();
});
