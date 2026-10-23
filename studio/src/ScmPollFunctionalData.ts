/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { attemptDigest } from "./LearningObservationAttemptData";
export type FunctionalScope = {
  base: string;
  actor: string;
  project: string;
  repository: string;
  binding: string;
};
export type FunctionalReview = {
  binding: {
    request: { request_id: string; component_ids: string[] };
    digest: string;
    verified_reference: { issue_number: number };
  };
  project_binding_digest: string;
  model_profile_id: string;
  model_profile_revision: number;
  model_profile_digest: string;
  routing_digest: string;
  functional_policy_digest: string;
  max_model_turns: number;
  max_duration_minutes: number;
};
export type FunctionalInput = {
  version: 1;
  transport: "github_rest_poll_v1";
  request_id: string;
  expected_previous_digest: string | null;
  expected_project_binding_digest: string;
  expected_binding_request_id: string;
  expected_binding_digest: string;
  component_ids: string[];
  issue_number: number;
  allowed_actions: string[];
  max_model_turns: number;
  expires_at: string;
  model_profile_id: string;
  expected_model_profile_revision: number;
  expected_model_profile_digest: string;
  expected_routing_digest: string;
  expected_functional_policy_digest: string;
  active: boolean;
  acknowledge_separate_assistant_allowance: true;
};
export type FunctionalAttempt = {
  version: 1;
  scope: FunctionalScope;
  input: FunctionalInput;
};
const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const fail = () =>
  new Error(
    "The exact functional consent could not be verified. Preserve its request identity.",
  );
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw fail();
  return value as Record<string, unknown>;
}
function validateInput(value: unknown): FunctionalInput {
  const row = object(value);
  const fields = [
    "version",
    "transport",
    "request_id",
    "expected_previous_digest",
    "expected_project_binding_digest",
    "expected_binding_request_id",
    "expected_binding_digest",
    "component_ids",
    "issue_number",
    "allowed_actions",
    "max_model_turns",
    "expires_at",
    "model_profile_id",
    "expected_model_profile_revision",
    "expected_model_profile_digest",
    "expected_routing_digest",
    "expected_functional_policy_digest",
    "active",
    "acknowledge_separate_assistant_allowance",
  ];
  if (
    Object.keys(row).sort().join() !== fields.sort().join() ||
    row.version !== 1 ||
    row.transport !== "github_rest_poll_v1" ||
    row.acknowledge_separate_assistant_allowance !== true ||
    typeof row.active !== "boolean" ||
    !id.test(String(row.request_id)) ||
    !id.test(String(row.expected_binding_request_id)) ||
    (row.expected_previous_digest !== null &&
      !hash.test(String(row.expected_previous_digest))) ||
    ![
      "expected_project_binding_digest",
      "expected_binding_digest",
      "expected_model_profile_digest",
      "expected_routing_digest",
      "expected_functional_policy_digest",
    ].every((k) => typeof row[k] === "string" && hash.test(row[k] as string)) ||
    !Number.isInteger(row.issue_number) ||
    Number(row.issue_number) < 1 ||
    !Number.isInteger(row.expected_model_profile_revision) ||
    Number(row.expected_model_profile_revision) < 1 ||
    typeof row.model_profile_id !== "string" ||
    !/^[a-z][a-z0-9_-]{0,63}$/.test(row.model_profile_id) ||
    typeof row.expires_at !== "string" ||
    !Number.isFinite(Date.parse(row.expires_at)) ||
    !/(Z|[+-]\d\d:\d\d)$/.test(row.expires_at) ||
    !Array.isArray(row.component_ids) ||
    !row.component_ids.length ||
    row.component_ids.length > 50 ||
    !row.component_ids.every(
      (v) => typeof v === "string" && /^[a-z][a-z0-9-]{0,63}$/.test(v),
    ) ||
    new Set(row.component_ids).size !== row.component_ids.length ||
    !Array.isArray(row.allowed_actions) ||
    !row.allowed_actions.length ||
    row.allowed_actions.length > 3 ||
    !row.allowed_actions.every((v) =>
      ["assistant_start", "assistant_status", "assistant_turn"].includes(v),
    ) ||
    new Set(row.allowed_actions).size !== row.allowed_actions.length ||
    !Number.isInteger(row.max_model_turns) ||
    Number(row.max_model_turns) < 0 ||
    Number(row.max_model_turns) > 3 ||
    row.allowed_actions.includes("assistant_turn") !==
      Number(row.max_model_turns) > 0
  )
    throw fail();
  return row as FunctionalInput;
}
export function functionalInput(
  review: FunctionalReview,
  request: string,
  previous: string | null,
  turns: number,
  minutes: number,
  active: boolean,
  clock = Date.now(),
): FunctionalInput {
  if (
    !Number.isInteger(minutes) ||
    minutes < 1 ||
    minutes > 60 ||
    review.max_model_turns !== 3 ||
    review.max_duration_minutes !== 60
  )
    throw fail();
  return validateInput({
    version: 1,
    transport: "github_rest_poll_v1",
    request_id: request,
    expected_previous_digest: previous,
    expected_project_binding_digest: review.project_binding_digest,
    expected_binding_request_id: review.binding.request.request_id,
    expected_binding_digest: review.binding.digest,
    component_ids: review.binding.request.component_ids,
    issue_number: review.binding.verified_reference.issue_number,
    allowed_actions: [
      "assistant_start",
      "assistant_status",
      ...(turns > 0 ? ["assistant_turn"] : []),
    ],
    max_model_turns: turns,
    expires_at: new Date(clock + minutes * 60000).toISOString(),
    model_profile_id: review.model_profile_id,
    expected_model_profile_revision: review.model_profile_revision,
    expected_model_profile_digest: review.model_profile_digest,
    expected_routing_digest: review.routing_digest,
    expected_functional_policy_digest: review.functional_policy_digest,
    active,
    acknowledge_separate_assistant_allowance: true,
  });
}
export function readFunctionalAttempt(
  raw: string | null,
  scope: FunctionalScope,
): FunctionalAttempt | null {
  if (raw === null) return null;
  if (raw.length > 12000) throw fail();
  const row = object(JSON.parse(raw)),
    saved = object(row.scope);
  if (
    Object.keys(row).sort().join() !== "input,scope,version" ||
    row.version !== 1 ||
    Object.keys(saved).sort().join() !== Object.keys(scope).sort().join() ||
    !Object.entries(scope).every(([k, v]) => saved[k] === v)
  )
    throw fail();
  const input = validateInput(row.input);
  if (input.expected_binding_request_id !== scope.binding) throw fail();
  return { version: 1, scope, input };
}
export function functionalKey(scope: FunctionalScope): string {
  return "forge.scm-poll-functional.v1:" + JSON.stringify(scope);
}
export async function confirmFunctionalReceipt(
  value: unknown,
  scope: FunctionalScope,
  expected?: FunctionalInput,
): Promise<string> {
  const row = object(value),
    input = validateInput(row.request);
  const unsigned = Object.fromEntries(
    Object.entries(row).filter(([key]) => key !== "digest"),
  );
  if (
    row.version !== 1 ||
    row.project_id !== scope.project ||
    row.repository_id !== scope.repository ||
    input.expected_binding_request_id !== scope.binding ||
    !id.test(String(row.event_id)) ||
    !id.test(String(row.native_user_id)) ||
    !hash.test(String(row.binding_key)) ||
    row.previous_digest !== input.expected_previous_digest ||
    row.request_digest !== (await attemptDigest(input)) ||
    row.digest !== (await attemptDigest(unsigned)) ||
    (expected &&
      (await attemptDigest(expected)) !== (await attemptDigest(input)))
  )
    throw fail();
  return row.digest as string;
}
