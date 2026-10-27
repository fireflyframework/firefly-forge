/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it, vi } from "vitest";
import { Children, isValidElement } from "react";
import type { IdentitySession } from "./Identity";
import type { ForgeRun } from "./api";
vi.mock("react", async (original) => {
  const actual = await original<typeof import("react")>();
  return {
    ...actual,
    useEffect: () => undefined,
    useState: () => [null, () => undefined],
  };
});
import { ExecutionRecord } from "./ExecutionRecord";
import { RunnerContinuation } from "./RunnerContinuation";
import { AgentCheckpointRecovery } from "./AgentCheckpointRecovery";
import { InvalidReadRecovery } from "./InvalidReadRecovery";
it("keeps recovery controls under distinct React identities through plan polls", () => {
  const run: ForgeRun = {
    id: "a".repeat(32),
    repository_id: "b".repeat(32),
    brief: "Retained source",
    status: "running",
  };
  const session = {
    user: { id: "c".repeat(32) },
    global_admin: true,
    memberships: [],
  } as unknown as IdentitySession;
  const render = (current: ForgeRun) => {
    const tree = ExecutionRecord({
      run: current,
      connection: { base: "/api/v1", token: "" },
      session,
    });
    return Children.toArray(tree.props.children).filter(
      (node) =>
        isValidElement(node) &&
        [RunnerContinuation, AgentCheckpointRecovery].includes(
          node.type as typeof RunnerContinuation,
        ),
    );
  };
  const first = render(run);
  expect(first).toHaveLength(2);
  const keys = first.map((node) => (isValidElement(node) ? node.key : null));
  expect(new Set(keys).size).toBe(2);
  expect(
    render({ ...run }).map((node) => (isValidElement(node) ? node.key : null)),
  ).toEqual(keys);
  expect(
    render({ ...run, status: "paused" }).map((node) =>
      isValidElement(node) ? node.key : null,
    ),
  ).not.toEqual(keys);
});
it("uses the same-ticket surface after continuity admission and withholds generic checkpoint recovery", () => {
  for (const marker of [
    { request_id: "d".repeat(32) },
    { request_id: "unknown" },
  ]) {
    const tree = ExecutionRecord({
      run: {
        id: "a".repeat(32),
        repository_id: "b".repeat(32),
        brief: "Retained source",
        status: "needs_reconciliation",
        invalid_read_continuity: marker,
      },
      connection: { base: "/api/v1", token: "" },
      session: {
        user: { id: "c".repeat(32) },
        global_admin: true,
        memberships: [],
      } as unknown as IdentitySession,
    });
    const children = Children.toArray(tree.props.children).filter(
      isValidElement,
    );
    expect(children.some((node) => node.type === InvalidReadRecovery)).toBe(
      true,
    );
    expect(children.some((node) => node.type === AgentCheckpointRecovery)).toBe(
      false,
    );
    expect(children.some((node) => node.type === RunnerContinuation)).toBe(
      false,
    );
  }
});
it("keeps an actual pending runner command visible after continuity admission", () => {
  const tree = ExecutionRecord({
    run: {
      id: "a".repeat(32),
      repository_id: "b".repeat(32),
      brief: "Pending original runner command",
      status: "running",
      invalid_read_continuity: { request_id: "d".repeat(32) },
      worker_request: {
        id: "e".repeat(32),
        kind: "command",
        status: "pending",
      },
    },
    connection: { base: "/api/v1", token: "" },
    session: {
      user: { id: "c".repeat(32) },
      global_admin: true,
      memberships: [],
    } as unknown as IdentitySession,
  });
  expect(
    Children.toArray(tree.props.children).some(
      (node) => isValidElement(node) && node.type === RunnerContinuation,
    ),
  ).toBe(true);
});
