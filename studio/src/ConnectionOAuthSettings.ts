/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type OAuthProviderChoice = {
  provider: string;
  status: "prerequisite_required" | "registration_available";
  reason: string;
  agent_ready: false;
};
export type OAuthSummary = {
  name: string;
  revision: number;
  provider: string;
  client_id: string;
  project_id: string | null;
  status:
    | "authorization_required"
    | "authorized_unverified"
    | "reauthorization_required"
    | "disconnected";
  resource: string;
  scopes: string[];
  agent_ready: false;
  remote_revocation_supported: false;
  policy_current: boolean;
  authentication: "client_secret_basic" | "client_secret_post";
  public_client: boolean;
  secret_configured: boolean;
  callback_uri: string;
  refresh_available: boolean;
  grant_expires_at: string | null;
  effect_state: "none" | "executing" | "unknown";
  unresolved_effects: OAuthEffect[];
  archived_unknown_effects: OAuthEffect[];
  reconciliation_required: boolean;
  allowed_actions: Record<
    "configure" | "authorize" | "refresh" | "disconnect" | "supersede",
    boolean
  >;
  family: string | null;
  grant_revision: number | null;
  grant_status: string | null;
  observed_at: string | null;
};
export type OAuthEffect = {
  id: string;
  kind: "code" | "refresh";
  state: "executing" | "unknown";
  registration_revision: number;
  lease_expired: boolean;
};
export type OAuthPending = {
  requestId: string;
  revision: number;
  kind: "refresh";
};
export type OAuthReceipt = Omit<OAuthEffect, "state"> & {
  request_id: string;
  state: "executing" | "unknown" | "completed" | "rejected";
  observed_at: string | null;
  current_context: boolean;
};
export type OAuthRegistrationDraft = {
  name: string;
  provider: string;
  project: string;
  clientId: string;
  clientType: string;
  authentication: string;
  secret: string;
};
export type OAuthActionMarker = {
  name: string;
  uncertain: boolean;
  pending?: OAuthPending;
};
export type OAuthResume = {
  name: string;
  actor: string;
  base: string;
  uncertain: boolean;
  pending?: OAuthPending;
  history?: OAuthActionMarker[];
};
export const resumeKey = "forge.browser-connection";
const identifier = /^[a-z][a-z0-9-]{0,63}$/;
const id = /^[a-f0-9]{32}$/;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("The connection state could not be read.");
  return value as Record<string, unknown>;
}
function text(value: unknown, limit = 200): string {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > limit ||
    controlCharacters(value)
  )
    throw new Error("The connection returned unsupported text.");
  return value;
}
function controlCharacters(value: string): boolean {
  return Array.from(value).some((character) => {
    const code = character.codePointAt(0)!;
    return code < 32 || code === 127 || (code >= 0xd800 && code <= 0xdfff);
  });
}
export function connectionName(value: string): string {
  if (!identifier.test(value) || value === "providers")
    throw new Error(
      "Use a name starting with a lowercase letter, followed by letters, numbers or hyphens (up to 64 characters). The name providers is reserved.",
    );
  return value;
}
function revision(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > 2147483647
  )
    throw new Error("The connection revision could not be read.");
  return value;
}
export function readOAuthProviders(value: unknown): OAuthProviderChoice[] {
  if (!Array.isArray(value) || value.length > 64)
    throw new Error("Provider prerequisites could not be read.");
  const result = value.map((entry) => {
    const p = record(entry);
    if (
      typeof p.provider !== "string" ||
      !identifier.test(p.provider) ||
      !["prerequisite_required", "registration_available"].includes(
        String(p.status),
      ) ||
      p.agent_ready !== false
    )
      throw new Error("The provider returned unsupported availability.");
    return {
      provider: p.provider as string,
      status: p.status as OAuthProviderChoice["status"],
      reason: text(p.reason, 2000),
      agent_ready: false as const,
    };
  });
  if (new Set(result.map((p) => p.provider)).size !== result.length)
    throw new Error("The provider catalog contains duplicate identities.");
  return result;
}
export function readOAuthSummary(value: unknown, name: string): OAuthSummary {
  const s = record(value);
  if (
    s.name !== connectionName(name) ||
    typeof s.provider !== "string" ||
    !identifier.test(s.provider) ||
    ![
      "authorization_required",
      "authorized_unverified",
      "reauthorization_required",
      "disconnected",
    ].includes(String(s.status)) ||
    s.agent_ready !== false ||
    s.remote_revocation_supported !== false ||
    (s.project_id !== null && !id.test(String(s.project_id))) ||
    !Array.isArray(s.scopes) ||
    !s.scopes.length ||
    s.scopes.length > 64 ||
    new Set(s.scopes).size !== s.scopes.length
  )
    throw new Error(
      "The server returned unsupported connection authority. Reload before acting.",
    );
  const result = {
    name,
    revision: revision(s.revision),
    provider: s.provider as string,
    client_id: text(s.client_id),
    project_id: s.project_id as string | null,
    status: s.status as OAuthSummary["status"],
    resource: text(s.resource, 2048),
    scopes: s.scopes.map((scope) => text(scope, 256)),
    agent_ready: false as const,
    remote_revocation_supported: false as const,
  };
  const actions = record(s.allowed_actions);
  if (
    !["none", "executing", "unknown"].includes(String(s.effect_state)) ||
    !["client_secret_basic", "client_secret_post"].includes(
      String(s.authentication),
    )
  )
    throw new Error("The server returned unsupported action authority.");
  return {
    ...result,
    policy_current: boolean(s.policy_current),
    authentication: s.authentication as OAuthSummary["authentication"],
    public_client: boolean(s.public_client),
    secret_configured: boolean(s.secret_configured),
    callback_uri: text(s.callback_uri, 4096),
    refresh_available: boolean(s.refresh_available),
    grant_expires_at: nullableText(s.grant_expires_at),
    effect_state: s.effect_state as OAuthSummary["effect_state"],
    unresolved_effects: effects(s.unresolved_effects),
    archived_unknown_effects: effects(s.archived_unknown_effects),
    reconciliation_required: boolean(s.reconciliation_required),
    allowed_actions: {
      configure: boolean(actions.configure),
      authorize: boolean(actions.authorize),
      refresh: boolean(actions.refresh),
      disconnect: boolean(actions.disconnect),
      supersede: boolean(actions.supersede),
    },
    family: s.family === null ? null : exactId(s.family),
    grant_revision:
      s.grant_revision === null ? null : revision(s.grant_revision),
    grant_status: nullableText(s.grant_status),
    observed_at: nullableText(s.observed_at),
  };
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean")
    throw new Error("The server action decision could not be read.");
  return value;
}
function exactId(value: unknown): string {
  if (typeof value !== "string" || !id.test(value))
    throw new Error("The action identity could not be read.");
  return value;
}
function nullableText(value: unknown): string | null {
  return value === null ? null : text(value, 128);
}
function effects(value: unknown): OAuthEffect[] {
  if (!Array.isArray(value) || value.length > 100)
    throw new Error("The effect history exceeds its reviewed bound.");
  const result = value.map((entry) => {
    const e = record(entry);
    if (
      !["code", "refresh"].includes(String(e.kind)) ||
      !["executing", "unknown"].includes(String(e.state))
    )
      throw new Error("The effect state could not be read.");
    return {
      id: exactId(e.id),
      kind: e.kind as OAuthEffect["kind"],
      state: e.state as OAuthEffect["state"],
      registration_revision: revision(e.registration_revision),
      lease_expired: boolean(e.lease_expired),
    };
  });
  if (new Set(result.map((e) => e.id)).size !== result.length)
    throw new Error("The effect history contains duplicate identities.");
  return result;
}
export function readOAuthDirectory(value: unknown): OAuthSummary[] {
  if (!Array.isArray(value) || value.length > 100)
    throw new Error("The connection directory exceeds its reviewed bound.");
  const result = value.map((entry) =>
    readOAuthSummary(entry, text(record(entry).name, 64)),
  );
  if (new Set(result.map((e) => e.name)).size !== result.length)
    throw new Error("The connection directory contains duplicate names.");
  return result;
}
export function readOAuthReceipt(value: unknown): OAuthReceipt {
  const e = record(value);
  if (
    !["code", "refresh"].includes(String(e.kind)) ||
    !["executing", "unknown", "completed", "rejected"].includes(String(e.state))
  )
    throw new Error("The action receipt could not be read.");
  return {
    id: exactId(e.id),
    request_id: exactId(e.request_id),
    kind: e.kind as OAuthReceipt["kind"],
    state: e.state as OAuthReceipt["state"],
    registration_revision: revision(e.registration_revision),
    lease_expired: boolean(e.lease_expired),
    observed_at: nullableText(e.observed_at),
    current_context: boolean(e.current_context),
  };
}
export function receiptResolves(
  receipt: OAuthReceipt,
  pending: OAuthPending,
): boolean {
  return (
    receipt.current_context &&
    receipt.request_id === pending.requestId &&
    receipt.kind === pending.kind &&
    receipt.registration_revision === pending.revision &&
    ["completed", "rejected"].includes(receipt.state)
  );
}
export function registrationPayload(value: OAuthRegistrationDraft) {
  connectionName(value.name);
  if (
    !identifier.test(value.provider) ||
    (value.project && !id.test(value.project))
  )
    throw new Error("Choose a current provider and project.");
  const client = text(value.clientId);
  if (
    !["public", "confidential"].includes(value.clientType) ||
    !["client_secret_basic", "client_secret_post"].includes(
      value.authentication,
    )
  )
    throw new Error(
      "Choose an explicit client type and supported authentication method.",
    );
  if (
    value.clientType === "confidential" &&
    (!value.secret ||
      new TextEncoder().encode(value.secret).length > 4096 ||
      controlCharacters(value.secret))
  )
    throw new Error(
      "Enter a nonempty client secret of at most 4,096 bytes without control characters.",
    );
  return {
    provider: value.provider,
    client_id: client,
    project_id: value.project || null,
    authentication:
      value.clientType === "public"
        ? "client_secret_post"
        : value.authentication,
    ...(value.clientType === "public"
      ? { public_client: true }
      : { client_secret: value.secret }),
  };
}
export function continuation(
  value: unknown,
  name: string,
  base: string,
  location: string,
): string {
  const v = record(value),
    api = new URL(base, location),
    url = new URL(text(v.continuation_url, 4096));
  if (
    v.expires_in !== 300 ||
    !["http:", "https:"].includes(url.protocol) ||
    url.origin !== api.origin ||
    url.username ||
    url.password ||
    url.hash ||
    url.pathname !==
      `/api/v1/connection-oauth/${connectionName(name)}/continue` ||
    [...url.searchParams.keys()].length !== 1 ||
    !/^[-A-Za-z0-9_]{1,128}$/.test(url.searchParams.get("ticket") || "")
  )
    throw new Error(
      "The browser handoff does not match this factory and connection. No redirect was opened.",
    );
  return url.href;
}
function actionMarker(value: unknown): OAuthActionMarker {
  const r = record(value);
  if (
    !["name,uncertain", "name,pending,uncertain"].includes(
      Object.keys(r).sort().join(","),
    ) ||
    typeof r.uncertain !== "boolean"
  )
    throw new Error("The pending action could not be read.");
  let pending: OAuthPending | undefined;
  if (r.pending !== undefined) {
    const packet = record(r.pending);
    if (
      Object.keys(packet).sort().join(",") !== "kind,requestId,revision" ||
      packet.kind !== "refresh" ||
      !r.uncertain
    )
      throw new Error("The pending action does not match.");
    pending = {
      kind: "refresh",
      requestId: exactId(packet.requestId),
      revision: revision(packet.revision),
    };
  }
  return {
    name: connectionName(text(r.name, 64)),
    uncertain: r.uncertain,
    ...(pending ? { pending } : {}),
  };
}
export function readResume(
  value: string | null,
  base: string,
  actor: string,
): OAuthResume | null {
  try {
    if (!value || value.length > 40000) return null;
    const r = record(JSON.parse(value));
    const keys = Object.keys(r)
      .filter((key) => !["history", "pending"].includes(key))
      .sort()
      .join(",");
    if (
      keys !== "actor,base,name,uncertain" ||
      r.actor !== actor ||
      r.base !== base
    )
      return null;
    const marker = actionMarker({
      name: r.name,
      uncertain: r.uncertain,
      ...(r.pending !== undefined ? { pending: r.pending } : {}),
    });
    let history: OAuthActionMarker[] | undefined;
    if (r.history !== undefined) {
      if (!Array.isArray(r.history) || r.history.length > 100) return null;
      history = r.history.map(actionMarker);
      if (
        new Set([marker.name, ...history.map((e) => e.name)]).size !==
          history.length + 1 ||
        history.some((e) => !e.uncertain)
      )
        return null;
    }
    return { ...marker, actor, base, ...(history?.length ? { history } : {}) };
  } catch {
    return null;
  }
}
export function connectionResume(
  resume: OAuthResume | null,
  name: string,
): OAuthActionMarker | null {
  return resume?.name === name
    ? resume
    : resume?.history?.find((e) => e.name === name) || null;
}
export function resumePacket(
  previous: OAuthResume | null,
  name: string,
  base: string,
  actor: string,
  uncertain: boolean,
  pending?: OAuthPending,
): OAuthResume {
  const relevant =
    previous?.base === base && previous.actor === actor ? previous : null;
  const history = (relevant?.history || []).filter((e) => e.name !== name);
  if (relevant?.uncertain && relevant.name !== name)
    history.push({
      name: relevant.name,
      uncertain: true,
      ...(relevant.pending ? { pending: relevant.pending } : {}),
    });
  if (history.length > 100)
    throw new Error(
      "Resolve an outstanding account request before adding another.",
    );
  return {
    name,
    base,
    actor,
    uncertain,
    ...(pending ? { pending } : {}),
    ...(history.length ? { history } : {}),
  };
}
