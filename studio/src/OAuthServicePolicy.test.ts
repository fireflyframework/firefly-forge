/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  localOrigin,
  readPolicyDirectory,
  policyPending,
  readPolicyPending,
} from "./OAuthServicePolicy";
const origin = "http://[::1]:49152";
const provider = {
  id: "local-repository-tools",
  issuer: origin,
  authorization_endpoint: origin + "/oauth2/authorize",
  token_endpoint: origin + "/oauth2/token",
  resource: origin + "/mcp/readonly",
  scopes: ["repository:read"],
  scope_omission: "reject",
  refresh_scope_omission: "reject",
  refresh_token_omission: "reject",
  prerequisite: null,
};
const policy = {
  name: "local-repository-tools",
  revision: 1,
  digest: "a".repeat(64),
  mode: "local_development",
  enabled: true,
  local_origin: origin,
  provider,
};
it("accepts only canonical literal loopback origins and unprivileged ports", () => {
  expect(localOrigin(origin)).toBe(origin);
  expect(localOrigin("http://127.0.0.1:1024")).toBe("http://127.0.0.1:1024");
  for (const input of [
    "http://localhost:49152",
    "http://127.0.0.2:49152",
    "http://127.0.0.1:049152",
    "http://127.0.0.1:22",
    "https://127.0.0.1:49152",
    "http://[::1%zone]:49152",
    origin + "/",
    origin + "?x=1",
    origin + "#x",
    "http://user:secret@[::1]:49152",
  ])
    expect(() => localOrigin(input)).toThrow();
});
it("requires the exact fixed native local service contract without inventing vendor admission", () => {
  expect(
    readPolicyDirectory({
      local_development_available: true,
      policies: [policy],
    }).policies[0],
  ).toEqual(policy);
  for (const value of [
    { local_development_available: "true", policies: [] },
    {
      local_development_available: true,
      policies: [{ ...policy, name: "figma" }],
    },
    {
      local_development_available: true,
      policies: [
        {
          ...policy,
          provider: { ...provider, resource: origin + "/arbitrary" },
        },
      ],
    },
    {
      local_development_available: true,
      policies: [{ ...policy, provider: { ...provider, scopes: ["write"] } }],
    },
    { local_development_available: true, policies: [policy, policy] },
  ])
    expect(() => readPolicyDirectory(value)).toThrow();
});
it("retains a stable exact policy request for only its factory and actor, without credentials", () => {
  const body = {
    enabled: true,
    local_origin: origin,
    expected_revision: 0,
    request_id: "b".repeat(32),
  };
  const marker = policyPending(body, "/api/v1", "f".repeat(32));
  expect(
    readPolicyPending(JSON.stringify(marker), "/api/v1", "f".repeat(32)),
  ).toEqual(marker);
  expect(
    readPolicyPending(JSON.stringify(marker), "/api/v1", "c".repeat(32)),
  ).toBeNull();
  expect(
    readPolicyPending(
      JSON.stringify({
        ...marker,
        body: { ...body, client_secret: "invalid" },
      }),
      "/api/v1",
      "f".repeat(32),
    ),
  ).toBeNull();
});
