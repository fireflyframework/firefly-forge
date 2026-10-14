/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export const localProvider = "local-repository-tools";
export type OAuthPolicyInput = {
  enabled: boolean;
  local_origin: string;
  expected_revision: number;
  request_id: string;
};
export type OAuthLocalPolicy = {
  name: typeof localProvider;
  revision: number;
  digest: string;
  mode: "local_development";
  enabled: boolean;
  local_origin: string;
  provider: {
    id: typeof localProvider;
    issuer: string;
    authorization_endpoint: string;
    token_endpoint: string;
    resource: string;
    scopes: ["repository:read"];
    scope_omission: "reject";
    refresh_scope_omission: "reject";
    refresh_token_omission: "reject";
    prerequisite: null;
  };
};
export type OAuthPolicyDirectory = {
  local_development_available: boolean;
  policies: OAuthLocalPolicy[];
};
export type OAuthPolicyPending = {
  actor: string;
  base: string;
  body: OAuthPolicyInput;
};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("The service configuration could not be read.");
  return value as Record<string, unknown>;
}
export function localOrigin(value: string): string {
  const match = /^http:\/\/(127\.0\.0\.1|\[::1\]):([1-9][0-9]{3,4})$/.exec(
    value,
  );
  if (!match || Number(match[2]) < 1024 || Number(match[2]) > 65535)
    throw new Error(
      "Choose a literal loopback host and a port from 1,024 to 65,535. Do not add paths, credentials or query parameters.",
    );
  return value;
}
function revision(value: unknown, maximum = 2147483647): number {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < 1 ||
    value > maximum
  )
    throw new Error("The service revision could not be read.");
  return value;
}
export function readLocalPolicy(value: unknown): OAuthLocalPolicy {
  const p = object(value),
    provider = object(p.provider);
  if (typeof p.local_origin !== "string")
    throw new Error("The service address could not be read.");
  const origin = localOrigin(p.local_origin);
  if (
    p.name !== localProvider ||
    p.mode !== "local_development" ||
    typeof p.enabled !== "boolean" ||
    typeof p.digest !== "string" ||
    !/^[a-f0-9]{64}$/.test(p.digest) ||
    provider.id !== localProvider ||
    provider.issuer !== origin ||
    provider.authorization_endpoint !== origin + "/oauth2/authorize" ||
    provider.token_endpoint !== origin + "/oauth2/token" ||
    provider.resource !== origin + "/mcp/readonly" ||
    !Array.isArray(provider.scopes) ||
    provider.scopes.length !== 1 ||
    provider.scopes[0] !== "repository:read" ||
    provider.scope_omission !== "reject" ||
    provider.refresh_scope_omission !== "reject" ||
    provider.refresh_token_omission !== "reject" ||
    provider.prerequisite !== null
  )
    throw new Error(
      "The service does not match the supported local repository contract.",
    );
  return {
    name: localProvider,
    revision: revision(p.revision),
    digest: p.digest,
    mode: "local_development",
    enabled: p.enabled,
    local_origin: origin,
    provider: {
      id: localProvider,
      issuer: origin,
      authorization_endpoint: origin + "/oauth2/authorize",
      token_endpoint: origin + "/oauth2/token",
      resource: origin + "/mcp/readonly",
      scopes: ["repository:read"],
      scope_omission: "reject",
      refresh_scope_omission: "reject",
      refresh_token_omission: "reject",
      prerequisite: null,
    },
  };
}
export function readPolicyDirectory(value: unknown): OAuthPolicyDirectory {
  const d = object(value);
  if (
    typeof d.local_development_available !== "boolean" ||
    !Array.isArray(d.policies) ||
    d.policies.length > 1
  )
    throw new Error("Local service availability could not be read.");
  return {
    local_development_available: d.local_development_available,
    policies: d.policies.map(readLocalPolicy),
  };
}
function input(value: unknown): OAuthPolicyInput {
  const p = object(value);
  if (
    Object.keys(p).sort().join(",") !==
      "enabled,expected_revision,local_origin,request_id" ||
    typeof p.enabled !== "boolean" ||
    typeof p.local_origin !== "string" ||
    typeof p.request_id !== "string" ||
    !/^[a-f0-9]{32}$/.test(p.request_id) ||
    typeof p.expected_revision !== "number" ||
    !Number.isInteger(p.expected_revision) ||
    p.expected_revision < 0 ||
    p.expected_revision > 2147483646
  )
    throw new Error("The saved configuration request could not be read.");
  return {
    enabled: p.enabled,
    local_origin: localOrigin(p.local_origin),
    expected_revision: p.expected_revision,
    request_id: p.request_id,
  };
}
export function policyPending(
  body: OAuthPolicyInput,
  base: string,
  actor: string,
): OAuthPolicyPending {
  return { body: input(body), base, actor };
}
export function readPolicyPending(
  value: string | null,
  base: string,
  actor: string,
): OAuthPolicyPending | null {
  try {
    if (!value || value.length > 8192) return null;
    const p = object(JSON.parse(value));
    if (
      Object.keys(p).sort().join(",") !== "actor,base,body" ||
      p.actor !== actor ||
      p.base !== base
    )
      return null;
    return policyPending(input(p.body), base, actor);
  } catch {
    return null;
  }
}
export function policyPendingKey(base: string, actor: string): string {
  return `forge.local-service-action:${actor}:${encodeURIComponent(base)}`;
}
