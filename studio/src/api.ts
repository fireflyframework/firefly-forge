/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type Connection = { base: string; token: string };
export type Repository = {
  id: string;
  name: string;
  url: string;
  provider: string;
  default_branch: string;
};
export type ForgeRun = {
  id: string;
  repository_id: string;
  brief: string;
  status: string;
  backend?: string;
  frontend?: string;
  agent?: string;
  created_at?: string;
  events?: Array<Record<string, unknown>>;
  verification?: Array<Record<string, unknown>> | Record<string, unknown>;
  changed_files?: string[];
  diff?: string;
  artifacts?: unknown[];
  error?: string | null;
  pr_url?: string | null;
};
export type NewRun = {
  repository_id: string;
  brief: string;
  backend: string;
  frontend: string;
  agent: string;
  commands: { argv: string[]; timeout: number }[];
  publish: false;
};
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export function normalizeBase(value: string): string {
  const candidate = value.trim().replace(/\/+$/, "") || "/api/v1";
  if (candidate === "/api/v1") return candidate;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new Error("Use /api/v1 or a complete HTTP(S) API address.");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error(
      "Use an HTTP(S) API address without credentials, query or fragment.",
    );
  return candidate;
}
export async function request<T>(
  connection: Connection,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(normalizeBase(connection.base) + path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(connection.token
        ? { Authorization: "Bearer " + connection.token }
        : {}),
      ...options.headers,
    },
  });
  const contents = await response.text();
  let data: unknown;
  try {
    data = contents ? JSON.parse(contents) : undefined;
  } catch {
    throw new ApiError(
      response.ok
        ? "The server returned a non-JSON response. Check the API address."
        : `Request failed (${response.status}).`,
      response.status,
    );
  }
  if (!response.ok) {
    const error = data as { detail?: unknown; error?: string } | undefined;
    const detail =
      typeof error?.detail === "string" ? error.detail : error?.error;
    throw new ApiError(
      detail ||
        (response.status === 401
          ? "The API token was rejected. Update your connection settings."
          : `Request failed (${response.status}).`),
      response.status,
    );
  }
  return data as T;
}
export const post = <T>(connection: Connection, path: string, body?: unknown) =>
  request<T>(connection, path, {
    method: "POST",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
export function parseCommands(value: string): NewRun["commands"] {
  if (!value.trim()) return [];
  let commands: unknown;
  try {
    commands = JSON.parse(value);
  } catch {
    throw new Error(
      "Verification commands must be a JSON array of argument arrays.",
    );
  }
  if (
    !Array.isArray(commands) ||
    commands.some(
      (command) =>
        !Array.isArray(command) ||
        !command.length ||
        command.some(
          (argument) => typeof argument !== "string" || !argument.length,
        ),
    )
  )
    throw new Error(
      "Each command needs at least one non-empty string argument.",
    );
  return (commands as string[][]).map((argv) => ({ argv, timeout: 120 }));
}
