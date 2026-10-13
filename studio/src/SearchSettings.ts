/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type SearchSettings = {
  provider: "brave" | "tavily";
  revision: string | null;
  configured: boolean;
  live_search_verified: boolean;
  include_domains?: string[];
};
export type SearchDraft = {
  provider: SearchSettings["provider"];
  key: string;
  domains: string;
  remove: boolean;
};
function domains(values: string[]): string[] {
  if (values.length > 10) throw new Error("Use at most ten public hostnames.");
  if (values.some((value) => value.length > 253))
    throw new Error("Use hostnames with at most 253 characters.");
  const normalized = values.map((value) =>
    value.toLowerCase().replace(/\.$/, ""),
  );
  if (
    normalized.some(
      (host) =>
        host.length > 253 ||
        !host.includes(".") ||
        /\.(local|localhost|internal|invalid|test|localdomain)$/.test(host) ||
        /^\d+\.\d+\.\d+\.\d+$/.test(host) ||
        host
          .split(".")
          .some(
            (label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label),
          ),
    )
  )
    throw new Error(
      "Use public hostnames such as github.com, without URLs, paths, IP addresses or wildcards.",
    );
  return [...new Set(normalized)];
}
export function readSearchSettings(value: unknown): SearchSettings {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error(
      "Saved search settings could not be read. Reload before making changes.",
    );
  const record = value as Record<string, unknown>;
  if (
    !["brave", "tavily"].includes(String(record.provider)) ||
    typeof record.configured !== "boolean" ||
    typeof record.live_search_verified !== "boolean" ||
    !(
      record.revision === null ||
      (typeof record.revision === "string" &&
        /^[a-f0-9]{32}$/.test(record.revision))
    ) ||
    (record.include_domains !== undefined &&
      (!Array.isArray(record.include_domains) ||
        record.include_domains.some((item) => typeof item !== "string")))
  )
    throw new Error(
      "The server returned unsupported search settings. Reload before making changes.",
    );
  return {
    provider: record.provider as SearchSettings["provider"],
    revision: record.revision as string | null,
    configured: record.configured,
    live_search_verified: record.live_search_verified,
    ...(record.include_domains === undefined
      ? {}
      : { include_domains: domains(record.include_domains as string[]) }),
  };
}
export function searchPayload(
  current: SearchSettings,
  draft: SearchDraft,
): Record<string, unknown> {
  const key = draft.key.trim(),
    switching = draft.provider !== current.provider;
  if (draft.remove && (key || switching))
    throw new Error(
      "Remove the saved key separately from changing providers or entering a replacement.",
    );
  if (switching && !key)
    throw new Error("Changing providers requires a new provider key.");
  if (!draft.remove && !current.configured && !key)
    throw new Error("Enter a provider key before saving this connection.");
  if (key.length > 4096)
    throw new Error(
      "The provider key is too long. Use at most 4,096 characters.",
    );
  const restriction = domains(
    draft.domains
      .split("\n")
      .map((value) => value.trim())
      .filter(Boolean),
  );
  return {
    ...(switching || !current.revision ? { provider: draft.provider } : {}),
    ...(key ? { api_key: key } : {}),
    ...(draft.remove ? { clear_secret: true } : {}),
    ...(JSON.stringify(restriction) !==
    JSON.stringify(current.include_domains ?? [])
      ? { include_domains: restriction }
      : {}),
  };
}
export function searchError(cause: unknown, key: string) {
  let message =
    cause instanceof Error
      ? cause.message
      : "The request could not be completed.";
  for (const secret of [...new Set([key, key.trim()])].filter(Boolean))
    message = message.replaceAll(secret, "[redacted]");
  message = message.replace(/[\r\n\t]+/g, " ");
  return message.length > 800 ? message.slice(0, 800) + "…" : message;
}
