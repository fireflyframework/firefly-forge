/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  continuation,
  registrationPayload,
  readOAuthProviders,
  readOAuthSummary,
  readResume,
  readOAuthDirectory,
  readOAuthReceipt,
  receiptResolves,
  resumePacket,
  connectionResume,
} from "./ConnectionOAuthSettings";
const summary = {
  name: "owned",
  revision: 1,
  provider: "fixture",
  client_id: "client",
  project_id: null,
  status: "authorization_required",
  resource: "https://fixture.example/mcp",
  scopes: ["read"],
  agent_ready: false,
  remote_revocation_supported: false,
  policy_current: true,
  authentication: "client_secret_post",
  public_client: true,
  secret_configured: false,
  callback_uri: "https://forge.example/api/v1/connection-oauth/owned/callback",
  refresh_available: false,
  grant_expires_at: null,
  effect_state: "none",
  unresolved_effects: [],
  archived_unknown_effects: [],
  reconciliation_required: false,
  allowed_actions: {
    configure: true,
    authorize: true,
    refresh: false,
    disconnect: true,
    supersede: false,
  },
  family: null,
  grant_revision: null,
  grant_status: null,
  observed_at: null,
};
it("refuses unsupported authority or status instead of presenting a connected provider", () => {
  expect(() =>
    readOAuthProviders([
      {
        provider: "figma",
        status: "registration_available",
        reason: "fixture",
        agent_ready: true,
      },
    ]),
  ).toThrow();
  expect(() =>
    readOAuthSummary({ ...summary, status: "connected" }, "owned"),
  ).toThrow();
  expect(() =>
    readOAuthSummary({ ...summary, name: "other" }, "owned"),
  ).toThrow();
  expect(() =>
    readOAuthSummary({ ...summary, scopes: Array(65).fill("read") }, "owned"),
  ).toThrow();
});
it("accepts the actual unverified summary without inventing refresh availability", () => {
  expect(readOAuthSummary(summary, "owned")).toEqual(summary);
});
it("requires an explicit secret choice and preserves exact secret characters", () => {
  const fields = {
    name: "owned",
    provider: "fixture",
    project: "",
    clientId: "client",
    clientType: "confidential",
    authentication: "client_secret_post",
    secret: " exact secret ",
  };
  expect(registrationPayload(fields)).toEqual({
    provider: "fixture",
    client_id: "client",
    project_id: null,
    authentication: "client_secret_post",
    client_secret: " exact secret ",
  });
  expect(() => registrationPayload({ ...fields, secret: "" })).toThrow();
  expect(() => registrationPayload({ ...fields, name: "providers" })).toThrow();
  expect(() =>
    registrationPayload({ ...fields, secret: "x".repeat(4097) }),
  ).toThrow();
  expect(() =>
    registrationPayload({ ...fields, secret: "é".repeat(2049) }),
  ).toThrow();
  expect(() => registrationPayload({ ...fields, secret: "\ud800" })).toThrow();
  expect(
    registrationPayload({ ...fields, clientType: "public", secret: "" }),
  ).toEqual({
    provider: "fixture",
    client_id: "client",
    project_id: null,
    public_client: true,
    authentication: "client_secret_post",
  });
});
it("only follows the exact native continuation route with one bounded handoff", () => {
  const input = {
    continuation_url:
      "https://forge.example/api/v1/connection-oauth/owned/continue?ticket=fixture-ticket",
    expires_in: 300,
  };
  expect(
    continuation(
      input,
      "owned",
      "https://forge.example/api/v1",
      "https://forge.example/",
    ),
  ).toBe(input.continuation_url);
  for (const url of [
    "https://evil.example/api/v1/connection-oauth/owned/continue?ticket=x",
    "javascript:alert(1)",
    "https://forge.example/api/v1/connection-oauth/other/continue?ticket=x",
    "https://forge.example/api/v1/connection-oauth/owned/continue?ticket=x&ticket=y",
  ])
    expect(() =>
      continuation(
        { ...input, continuation_url: url },
        "owned",
        "https://forge.example/api/v1",
        "https://forge.example/",
      ),
    ).toThrow();
});
it("resumes only nonsecret metadata for the current actor and factory", () => {
  const data = {
    name: "owned",
    actor: "f".repeat(32),
    base: "/api/v1",
    uncertain: false,
  };
  expect(readResume(JSON.stringify(data), "/api/v1", "f".repeat(32))).toEqual(
    data,
  );
  expect(
    readResume(
      JSON.stringify({ ...data, actor: "a".repeat(32) }),
      "/api/v1",
      "f".repeat(32),
    ),
  ).toBeNull();
  expect(
    readResume(
      JSON.stringify({ ...data, client_secret: "private" }),
      "/api/v1",
      "f".repeat(32),
    ),
  ).toBeNull();
});

it("retains server action decisions and bounded exact effect identities", () => {
  const effect = {
    id: "a".repeat(32),
    kind: "refresh",
    state: "unknown",
    registration_revision: 1,
    lease_expired: true,
  };
  const current = {
    ...summary,
    effect_state: "unknown",
    unresolved_effects: [effect],
    reconciliation_required: true,
    allowed_actions: {
      configure: false,
      authorize: false,
      refresh: false,
      disconnect: true,
      supersede: true,
    },
  };
  expect(readOAuthSummary(current, "owned").allowed_actions).toEqual(
    current.allowed_actions,
  );
  expect(readOAuthDirectory([current])).toEqual([current]);
  for (const invalid of [
    {
      ...current,
      allowed_actions: { ...current.allowed_actions, refresh: "false" },
    },
    { ...current, unresolved_effects: [effect, effect] },
    { ...current, revision: 2147483648 },
    {
      ...current,
      archived_unknown_effects: [{ ...effect, state: "completed" }],
    },
    { ...current, secret_configured: "false" },
  ])
    expect(() => readOAuthSummary(invalid, "owned")).toThrow();
  expect(() => readOAuthDirectory([current, current])).toThrow();
  expect(() => readOAuthDirectory(Array(101).fill(current))).toThrow();
});
it("resolves only an exact closed refresh receipt in the current actor and revision context", () => {
  const pending = {
    requestId: "a".repeat(32),
    revision: 1,
    kind: "refresh" as const,
  };
  const receipt = {
    id: "b".repeat(32),
    request_id: pending.requestId,
    kind: "refresh",
    state: "completed",
    registration_revision: 1,
    lease_expired: false,
    observed_at: null,
    current_context: true,
  };
  expect(receiptResolves(readOAuthReceipt(receipt), pending)).toBe(true);
  for (const invalid of [
    { ...receipt, state: "unknown" },
    { ...receipt, state: "executing" },
    { ...receipt, current_context: false },
    { ...receipt, registration_revision: 2 },
    { ...receipt, request_id: "c".repeat(32) },
    { ...receipt, kind: "code" },
  ])
    expect(receiptResolves(readOAuthReceipt(invalid), pending)).toBe(false);
  expect(() =>
    readOAuthReceipt({ ...receipt, current_context: "true" }),
  ).toThrow();
});

it("keeps another account's outstanding identity when navigation writes a new resume pointer", () => {
  const actor = "f".repeat(32),
    base = "/api/v1";
  const pending = {
    requestId: "a".repeat(32),
    revision: 1,
    kind: "refresh" as const,
  };
  const previous = { name: "first", actor, base, uncertain: true, pending };
  const next = resumePacket(previous, "second", base, actor, false);
  const parsed = readResume(JSON.stringify(next), base, actor)!;
  expect(connectionResume(parsed, "first")).toMatchObject({
    name: "first",
    uncertain: true,
    pending,
  });
  expect(connectionResume(parsed, "second")).toMatchObject({
    name: "second",
    uncertain: false,
  });
  const closed = resumePacket(parsed, "first", base, actor, false);
  expect(connectionResume(closed, "first")?.uncertain).toBe(false);
  expect(
    readResume(
      JSON.stringify({
        ...next,
        history: [
          { name: "first", uncertain: true, pending, secret: "invalid" },
        ],
      }),
      base,
      actor,
    ),
  ).toBeNull();
});
