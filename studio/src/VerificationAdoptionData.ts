/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { RecoveryContext } from "./InvalidReadRecoveryData";
import { attemptDigest } from "./LearningObservationAttemptData";
type Row = Record<string, unknown>;
export type ReusableBuild = {
  command_index: number;
  command_digest: string;
  argv: string[];
  purpose: "source_build";
  requires_declaration: true;
};
export type AdoptionCandidate = {
  version: 1;
  run_id: string;
  worker_request_id: string;
  agent: string;
  commands_count: number;
  max_continuations: number;
  pending_request_digest: string;
  source_digest: string;
  commands_digest: string;
  policy_digest: string;
  provider_digest: string;
  reusable_build_candidates?: ReusableBuild[];
};
export type AdoptionInput = Pick<
  AdoptionCandidate,
  | "pending_request_digest"
  | "source_digest"
  | "commands_digest"
  | "policy_digest"
  | "provider_digest"
  | "agent"
> & {
  request_id: string;
  reusable_build_checks?: {
    command_digest: string;
    purpose: "source_build";
    independent_of_time_and_external_state: true;
  }[];
};
export type AdoptionMarker = {
  version: 1;
  context: RecoveryContext;
  body: AdoptionInput;
};
const fields = [
  "pending_request_digest",
  "source_digest",
  "commands_digest",
  "policy_digest",
  "provider_digest",
] as const;
const id = (value: unknown): value is string =>
  typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
const hash = (value: unknown): value is string =>
  typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
function need(value: unknown): asserts value {
  if (!value)
    throw Error(
      "The verification selection could not be verified. Keep its saved identity.",
    );
}
function object(value: unknown): Row {
  need(value && typeof value === "object" && !Array.isArray(value));
  return value as Row;
}
function keys(value: Row, names: string[], optional: string[] = []) {
  need(
    Object.keys(value).every((key) => [...names, ...optional].includes(key)) &&
      names.every((key) => key in value),
  );
}
const agent = (value: unknown) =>
  typeof value === "string" &&
  ["agentic", "bridge", "codex", "claude"].includes(value);
const integer = (value: unknown, min = 0): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= min;
export function adoptionCandidate(
  value: unknown,
  context: RecoveryContext,
  provider: string,
): AdoptionCandidate {
  const c = object(value);
  keys(
    c,
    [
      "version",
      "run_id",
      "worker_request_id",
      "agent",
      "commands_count",
      "max_continuations",
      ...fields,
    ],
    ["reusable_build_candidates"],
  );
  need(
    c.version === 1 &&
      id(context.run) &&
      id(context.request) &&
      c.run_id === context.run &&
      c.worker_request_id === context.request &&
      agent(c.agent) &&
      c.agent === provider &&
      fields.every((key) => hash(c[key])) &&
      integer(c.commands_count, 1) &&
      integer(c.max_continuations, 1),
  );
  if (c.reusable_build_candidates !== undefined) {
    need(Array.isArray(c.reusable_build_candidates));
    const indices = new Set<number>(),
      digests = new Set<string>();
    for (const value of c.reusable_build_candidates) {
      const check = object(value);
      keys(check, [
        "command_index",
        "command_digest",
        "argv",
        "purpose",
        "requires_declaration",
      ]);
      need(
        integer(check.command_index, 1) &&
          check.command_index <= c.commands_count &&
          hash(check.command_digest) &&
          !indices.has(check.command_index) &&
          !digests.has(check.command_digest) &&
          check.purpose === "source_build" &&
          check.requires_declaration === true &&
          Array.isArray(check.argv) &&
          check.argv.length &&
          check.argv.every((value) => typeof value === "string"),
      );
      indices.add(check.command_index);
      digests.add(check.command_digest);
    }
  }
  return c as AdoptionCandidate;
}
function input(value: unknown): AdoptionInput {
  const body = object(value);
  keys(body, ["request_id", "agent", ...fields], ["reusable_build_checks"]);
  need(
    id(body.request_id) &&
      agent(body.agent) &&
      fields.every((key) => hash(body[key])),
  );
  if (body.reusable_build_checks !== undefined) {
    need(
      Array.isArray(body.reusable_build_checks) &&
        body.reusable_build_checks.length > 0,
    );
    const seen = new Set<string>();
    for (const value of body.reusable_build_checks) {
      const check = object(value);
      keys(check, [
        "command_digest",
        "purpose",
        "independent_of_time_and_external_state",
      ]);
      need(
        hash(check.command_digest) &&
          !seen.has(check.command_digest) &&
          check.purpose === "source_build" &&
          check.independent_of_time_and_external_state === true,
      );
      seen.add(check.command_digest);
    }
  }
  return body as AdoptionInput;
}
export function adoptionInput(
  candidate: AdoptionCandidate,
  requestId: string,
  selected: string[],
): AdoptionInput {
  const available = candidate.reusable_build_candidates ?? [];
  need(
    new Set(selected).size === selected.length &&
      selected.every((digest) =>
        available.some((check) => check.command_digest === digest),
      ),
  );
  return input({
    request_id: requestId,
    agent: candidate.agent,
    ...Object.fromEntries(fields.map((key) => [key, candidate[key]])),
    ...(selected.length
      ? {
          reusable_build_checks: available
            .filter((check) => selected.includes(check.command_digest))
            .map((check) => ({
              command_digest: check.command_digest,
              purpose: "source_build",
              independent_of_time_and_external_state: true,
            })),
        }
      : {}),
  });
}
export const adoptionKey = (context: RecoveryContext) =>
  "forge.verification-adoption.v1:" +
  encodeURIComponent(JSON.stringify(context));
export function readAdoption(
  raw: string | null,
  context: RecoveryContext,
): AdoptionMarker | null {
  if (raw === null) return null;
  const marker = object(JSON.parse(raw));
  keys(marker, ["version", "context", "body"]);
  const saved = object(marker.context);
  keys(saved, Object.keys(context));
  need(
    marker.version === 1 &&
      id(context.actor) &&
      id(context.run) &&
      id(context.request) &&
      id(context.repository) &&
      (context.project === null || id(context.project)) &&
      Object.entries(context).every(([key, value]) => saved[key] === value),
  );
  input(marker.body);
  return marker as AdoptionMarker;
}
export async function sendAdoptionOnce<T>(
  storage: Pick<Storage, "getItem" | "setItem">,
  marker: AdoptionMarker,
  send: () => Promise<T>,
): Promise<T> {
  const key = adoptionKey(marker.context),
    raw = JSON.stringify(marker);
  need(storage.getItem(key) === null);
  readAdoption(raw, marker.context);
  storage.setItem(key, raw);
  need(storage.getItem(key) === raw);
  return await send();
}
export async function confirmAdoption(
  value: unknown,
  marker: AdoptionMarker,
): Promise<void> {
  const receipt = object(value),
    actor = object(receipt.actor),
    policy = object(receipt.policy_snapshot);
  need(
    receipt.version === 1 &&
      hash(receipt.body_digest) &&
      (await attemptDigest(receipt.request)) === receipt.body_digest &&
      (await attemptDigest(marker.body)) === receipt.body_digest &&
      actor.user_id === marker.context.actor &&
      actor.authentication_method === marker.context.authentication &&
      policy.digest === marker.body.policy_digest,
  );
}
