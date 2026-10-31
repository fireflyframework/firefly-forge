/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  boardAccount,
  boardDiscovery,
  boardView,
  boardCreation,
  pendingBoards,
  retainBoardRequest,
  boardStorageKey,
} from "./GitHubProjectsData";

const context = {
  base: "https://factory.invalid",
  actor: "c".repeat(32),
  project: "a".repeat(32),
  repository: "b".repeat(32),
};
const account = {
  version: 1,
  project_id: context.project,
  repository_id: context.repository,
  connection_name: "delivery",
  viewer: { id: "U_1", login: "octocat" },
  repository: { id: "R_1", name_with_owner: "team/product", private: true },
};
const board = {
  id: "PVT_1",
  title: "Delivery",
  url: "https://github.com/users/octocat/projects/1",
  owner_kind: "user",
  owner: "octocat",
  number: 1,
  viewer_can_update: true,
  closed: false,
  private: true,
};
const discovery = {
  version: 1,
  connection_name: "delivery",
  board,
  repository: account.repository,
  viewer: account.viewer,
  fields: [
    {
      id: "FIELD_1",
      name: "Status",
      options: [{ id: "OPT_1", name: "In review" }],
    },
  ],
  discovery_digest: "d".repeat(64),
  truncated: false,
};
it("checks account scope and rejects injected authority and invalid viewer identity", () => {
  expect(boardAccount(account, context, "delivery")).toEqual(account);
  for (const value of [
    { ...account, project_id: "f".repeat(32) },
    { ...account, connection_name: "other" },
    { ...account, viewer: { id: "U_1", login: "invalid/login" } },
  ])
    expect(() => boardAccount(value, context, "delivery")).toThrow();
});
it("requires exact viewer/repository, private board update permission, and unambiguous fields", () => {
  const proof = boardAccount(account, context, "delivery");
  expect(boardDiscovery(discovery, proof)).toEqual(discovery);
  for (const value of [
    { ...discovery, board: { ...board, private: false } },
    { ...discovery, board: { ...board, viewer_can_update: false } },
    { ...discovery, viewer: { id: "U_2", login: "other" } },
    { ...discovery, fields: [...discovery.fields, ...discovery.fields] },
  ])
    expect(() => boardDiscovery(value, proof)).toThrow();
});
it("never grants configuration from an unavailable or foreign saved response", () => {
  const view = {
    version: 1,
    project_id: context.project,
    repository_id: context.repository,
    binding: null,
    permissions: { configure: false, sync: false },
    status: "not_configured",
    latest_request: null,
  };
  expect(boardView(view, context).permissions.configure).toBe(false);
  expect(() =>
    boardView({ ...view, repository_id: "f".repeat(32) }, context),
  ).toThrow();
  expect(() =>
    boardView(
      { ...view, permissions: { configure: "true", sync: true } },
      context,
    ),
  ).toThrow();
});
it("retains an immutable unknown create across aliases and checks readback scope", () => {
  const map = new Map<string, string>();
  const storage = {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
  };
  const marker = {
    id: "e".repeat(32),
    kind: "create" as const,
    name: "delivery",
    owner: "octocat",
    title: "Delivery",
    state: "unknown" as const,
  };
  retainBoardRequest(storage, context, marker);
  expect(pendingBoards(storage, context)).toEqual([marker]);
  expect(pendingBoards(storage, { ...context, actor: "f".repeat(32) })).toEqual(
    [],
  );
  expect(() =>
    retainBoardRequest(storage, context, { ...marker, name: "alias" }),
  ).toThrow();
  const receipt = {
    version: 1,
    id: marker.id,
    project_id: context.project,
    repository_id: context.repository,
    request_digest: "d".repeat(64),
    status: "unknown",
    board: null,
    replay_authorized: false,
    created_at: "2026-10-31T00:00:00Z",
  };
  expect(boardCreation(receipt, context, marker).status).toBe("unknown");
  expect(() =>
    boardCreation({ ...receipt, replay_authorized: true }, context, marker),
  ).toThrow();
  expect(() =>
    boardCreation({ ...receipt, id: "f".repeat(32) }, context, marker),
  ).toThrow();
  expect(boardStorageKey(context)).not.toBe(
    boardStorageKey({ ...context, base: "https://other.invalid" }),
  );
});
