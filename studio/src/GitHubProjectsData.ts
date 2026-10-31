/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export const boardStates = [
  "queued",
  "running",
  "paused",
  "needs_reconciliation",
  "failed",
  "cancelled",
  "interrupted",
  "succeeded",
] as const;
export const boardMappingStates = boardStates.filter(
  (state) => state !== "succeeded",
);
export type BoardContext = {
  base: string;
  actor: string;
  project: string;
  repository: string;
};
export type BoardAccount = {
  version: 1;
  project_id: string;
  repository_id: string;
  connection_name: string;
  viewer: { id: string; login: string };
  repository: { id: string; name_with_owner: string; private: boolean };
};
export type BoardIdentity = {
  id: string;
  title: string;
  url: string;
  owner_kind: "user" | "organization";
  owner: string;
  number: number;
  viewer_can_update: true;
  closed: false;
  private: true;
};
export type BoardField = {
  id: string;
  name: string;
  options: { id: string; name: string }[];
};
export type BoardDiscovery = {
  version: 1;
  connection_name: string;
  board: BoardIdentity;
  repository: BoardAccount["repository"];
  viewer: BoardAccount["viewer"];
  fields: BoardField[];
  discovery_digest: string;
  truncated: false;
};
export type BoardBinding = {
  digest: string;
  revision: number;
  enabled: boolean;
  connection_name: string;
  owner_kind: "user" | "organization";
  owner: string;
  number: number;
  board_id: string;
  board_url: string;
  field_id: string;
  state_options: Record<string, string>;
  include_existing: boolean;
};
export function normalizeBoardMapping(
  discovery: BoardDiscovery,
  fieldId: string,
  choices: Record<string, string>,
  binding: BoardBinding | null,
): Record<string, string> {
  const field = discovery.fields.find((item) => item.id === fieldId);
  if (
    !field ||
    !boardMappingStates.every((state) =>
      field.options.some((option) => option.id === choices[state]),
    )
  )
    throw new Error(
      "Choose a current GitHub option for all seven effective run statuses.",
    );
  // The v1 contract requires succeeded even though completed runs project to paused.
  // Preserve its unused value only from the current board/field and current options.
  const retained =
    binding?.board_id === discovery.board.id && binding.field_id === fieldId
      ? binding.state_options.succeeded
      : undefined;
  const succeeded = field.options.some((option) => option.id === retained)
    ? retained!
    : choices.paused;
  return {
    ...Object.fromEntries(
      boardMappingStates.map((state) => [state, choices[state]]),
    ),
    succeeded,
  };
}
export type BoardView = {
  version: 1;
  project_id: string;
  repository_id: string;
  binding: BoardBinding | null;
  permissions: { configure: boolean; sync: boolean };
  status:
    | "not_configured"
    | "ready"
    | "authorization_required"
    | "conflict"
    | "unknown";
  latest_request: { id: string; status: string } | null;
};
export type BoardPending = {
  id: string;
  kind: "create" | "configure";
  name: string;
  owner: string;
  title: string;
  state: "unknown" | "observed";
};
export type BoardCreation = {
  version: 1;
  id: string;
  project_id: string;
  repository_id: string;
  request_digest: string;
  status: "unknown" | "completed";
  board: BoardIdentity | null;
  replay_authorized: false;
  created_at: string;
};
const native = /^[a-f0-9]{32}$/,
  digest = /^[a-f0-9]{64}$/,
  node = /^[A-Za-z0-9_=-]{1,200}$/,
  owner = /^[A-Za-z0-9][A-Za-z0-9-]{0,99}$/,
  name = /^[a-z][a-z0-9-]{0,63}$/;
function invalid(): never {
  throw new Error(
    "The board response could not be verified. Refresh before continuing.",
  );
}
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, pattern?: RegExp): string {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > 2048 ||
    Array.from(value).some((character) => {
      const code = character.codePointAt(0)!;
      return code < 32 || code === 127;
    }) ||
    (pattern && !pattern.test(value))
  )
    return invalid();
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") return invalid();
  return value;
}
function integer(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > 2147483647
  )
    return invalid();
  return value;
}
function scope(value: unknown, context: BoardContext) {
  const result = object(value);
  if (
    result.version !== 1 ||
    result.project_id !== context.project ||
    result.repository_id !== context.repository ||
    !native.test(context.project) ||
    !native.test(context.repository)
  )
    return invalid();
  return result;
}
function viewer(value: unknown): BoardAccount["viewer"] {
  const v = object(value);
  return { id: text(v.id, node), login: text(v.login, owner) };
}
function repository(value: unknown): BoardAccount["repository"] {
  const r = object(value);
  return {
    id: text(r.id, node),
    name_with_owner: text(
      r.name_with_owner,
      /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/,
    ),
    private: boolean(r.private),
  };
}
function identity(value: unknown): BoardIdentity {
  const b = object(value),
    kind = b.owner_kind;
  if (
    !["user", "organization"].includes(String(kind)) ||
    b.private !== true ||
    b.closed !== false ||
    b.viewer_can_update !== true
  )
    return invalid();
  const login = text(b.owner, owner),
    number = integer(b.number),
    url = text(b.url);
  const expected =
    kind === "user"
      ? `https://github.com/users/${login}/projects/${number}`
      : `https://github.com/orgs/${login}/projects/${number}`;
  if (url.toLowerCase() !== expected.toLowerCase()) return invalid();
  return {
    id: text(b.id, node),
    title: text(b.title),
    url,
    owner_kind: kind as BoardIdentity["owner_kind"],
    owner: login,
    number,
    viewer_can_update: true,
    closed: false,
    private: true,
  };
}
export function boardAccount(
  value: unknown,
  context: BoardContext,
  connection: string,
): BoardAccount {
  const a = scope(value, context);
  if (a.connection_name !== connection || !name.test(connection))
    return invalid();
  return {
    version: 1,
    project_id: context.project,
    repository_id: context.repository,
    connection_name: connection,
    viewer: viewer(a.viewer),
    repository: repository(a.repository),
  };
}
function options(value: unknown): Record<string, string> {
  const v = object(value);
  if (
    Object.keys(v).length !== boardStates.length ||
    boardStates.some((key) => !(key in v))
  )
    return invalid();
  return Object.fromEntries(
    boardStates.map((key) => [key, text(v[key], node)]),
  );
}
export function boardView(value: unknown, context: BoardContext): BoardView {
  const v = scope(value, context),
    permissions = object(v.permissions);
  if (
    ![
      "not_configured",
      "ready",
      "authorization_required",
      "conflict",
      "unknown",
    ].includes(String(v.status))
  )
    return invalid();
  let binding: BoardBinding | null = null;
  if (v.binding !== null) {
    const b = object(v.binding);
    if (!["user", "organization"].includes(String(b.owner_kind)))
      return invalid();
    const login = text(b.owner, owner),
      number = integer(b.number),
      url = text(b.board_url),
      kind = b.owner_kind as BoardIdentity["owner_kind"];
    if (
      url.toLowerCase() !==
      (kind === "user"
        ? `https://github.com/users/${login}/projects/${number}`
        : `https://github.com/orgs/${login}/projects/${number}`
      ).toLowerCase()
    )
      return invalid();
    binding = {
      digest: text(b.digest, digest),
      revision: integer(b.revision),
      enabled: boolean(b.enabled),
      connection_name: text(b.connection_name, name),
      owner_kind: kind,
      owner: login,
      number,
      board_id: text(b.board_id, node),
      board_url: url,
      field_id: text(b.field_id, node),
      state_options: options(b.state_options),
      include_existing: boolean(b.include_existing),
    };
  }
  let latest: BoardView["latest_request"] = null;
  if (v.latest_request !== null) {
    const item = object(v.latest_request);
    if (
      ![
        "queued",
        "executing",
        "completed",
        "conflict",
        "unknown",
        "failed",
      ].includes(String(item.status))
    )
      return invalid();
    latest = { id: text(item.id, native), status: String(item.status) };
  }
  return {
    version: 1,
    project_id: context.project,
    repository_id: context.repository,
    binding,
    permissions: {
      configure: boolean(permissions.configure),
      sync: boolean(permissions.sync),
    },
    status: v.status as BoardView["status"],
    latest_request: latest,
  };
}
export function boardDiscovery(
  value: unknown,
  account: BoardAccount,
): BoardDiscovery {
  const d = object(value),
    v = viewer(d.viewer),
    r = repository(d.repository),
    board = identity(d.board);
  if (
    d.version !== 1 ||
    d.connection_name !== account.connection_name ||
    d.truncated !== false ||
    JSON.stringify(v) !== JSON.stringify(account.viewer) ||
    JSON.stringify(r) !== JSON.stringify(account.repository) ||
    (board.owner_kind === "user" &&
      board.owner.toLowerCase() !== v.login.toLowerCase()) ||
    !Array.isArray(d.fields) ||
    !d.fields.length
  )
    return invalid();
  const fields = d.fields.map((value) => {
    const f = object(value);
    if (!Array.isArray(f.options) || !f.options.length) return invalid();
    const choices = f.options.map((value) => {
      const o = object(value);
      return { id: text(o.id, node), name: text(o.name) };
    });
    if (new Set(choices.map((o) => o.id)).size !== choices.length)
      return invalid();
    return { id: text(f.id, node), name: text(f.name), options: choices };
  });
  if (new Set(fields.map((f) => f.id)).size !== fields.length) return invalid();
  return {
    version: 1,
    connection_name: account.connection_name,
    board,
    repository: r,
    viewer: v,
    fields,
    discovery_digest: text(d.discovery_digest, digest),
    truncated: false,
  };
}
export function boardCreation(
  value: unknown,
  context: BoardContext,
  marker: BoardPending,
): BoardCreation {
  const c = scope(value, context);
  if (
    c.id !== marker.id ||
    c.replay_authorized !== false ||
    !["unknown", "completed"].includes(String(c.status))
  )
    return invalid();
  const board = c.board === null ? null : identity(c.board);
  if (
    (c.status === "completed") !== !!board ||
    (board &&
      (board.owner_kind !== "user" ||
        board.owner.toLowerCase() !== marker.owner.toLowerCase() ||
        board.title !== marker.title))
  )
    return invalid();
  return {
    version: 1,
    id: marker.id,
    project_id: context.project,
    repository_id: context.repository,
    request_digest: text(c.request_digest, digest),
    status: c.status as BoardCreation["status"],
    board,
    replay_authorized: false,
    created_at: text(c.created_at),
  };
}
type StoragePort = Pick<Storage, "getItem" | "setItem">;
export function boardStorageKey(context: BoardContext): string {
  return "forge.github-projects:" + JSON.stringify(context);
}
function marker(value: unknown): BoardPending {
  const m = object(value);
  if (
    !["create", "configure"].includes(String(m.kind)) ||
    !["unknown", "observed"].includes(String(m.state))
  )
    return invalid();
  return {
    id: text(m.id, native),
    kind: m.kind as BoardPending["kind"],
    name: text(m.name, name),
    owner: text(m.owner, owner),
    title: text(m.title),
    state: m.state as BoardPending["state"],
  };
}
export function pendingBoards(
  storage: StoragePort,
  context: BoardContext,
): BoardPending[] {
  const raw = storage.getItem(boardStorageKey(context));
  if (raw === null) return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) return invalid();
  const list = parsed.map(marker);
  if (new Set(list.map((m) => m.id)).size !== list.length) return invalid();
  return list;
}
export function retainBoardRequest(
  storage: StoragePort,
  context: BoardContext,
  value: BoardPending,
): BoardPending[] {
  const incoming = marker(value),
    list = pendingBoards(storage, context),
    old = list.find((m) => m.id === incoming.id);
  if (
    old &&
    (JSON.stringify({ ...old, state: "unknown" }) !==
      JSON.stringify({ ...incoming, state: "unknown" }) ||
      (old.state === "observed" && incoming.state !== "observed"))
  )
    return invalid();
  const result = [...list.filter((m) => m.id !== incoming.id), incoming];
  storage.setItem(boardStorageKey(context), JSON.stringify(result));
  if (
    JSON.stringify(pendingBoards(storage, context)) !== JSON.stringify(result)
  )
    return invalid();
  return result;
}
