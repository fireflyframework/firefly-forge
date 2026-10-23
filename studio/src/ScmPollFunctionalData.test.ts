/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  functionalInput,
  readFunctionalAttempt,
} from "./ScmPollFunctionalData";
const scope = {
  base: "https://factory.invalid",
  actor: "a".repeat(32),
  project: "b".repeat(32),
  repository: "c".repeat(32),
  binding: "d".repeat(32),
};
const review = {
  binding: {
    request: { request_id: scope.binding, component_ids: ["api"] },
    digest: "e".repeat(64),
    verified_reference: { issue_number: 4 },
  },
  project_binding_digest: "f".repeat(64),
  model_profile_id: "functional",
  model_profile_revision: 2,
  model_profile_digest: "1".repeat(64),
  routing_digest: "2".repeat(64),
  functional_policy_digest: "5".repeat(64),
  max_model_turns: 3,
  max_duration_minutes: 60,
};
it("creates only explicit finite actions and keeps the run budget outside the contract", () => {
  const input = functionalInput(review, "3".repeat(32), null, 3, 30, true, 0);
  expect(input.allowed_actions).toEqual([
    "assistant_start",
    "assistant_status",
    "assistant_turn",
  ]);
  expect(input.max_model_turns).toBe(3);
  expect(input.expires_at).toBe("1970-01-01T00:30:00.000Z");
  expect(input).not.toHaveProperty("run_budget");
  expect(() =>
    functionalInput(review, "3".repeat(32), null, 4, 30, true, 0),
  ).toThrow();
  expect(() =>
    functionalInput(review, "3".repeat(32), null, 3, 61, true, 0),
  ).toThrow();
});
it("retains one exact attempt across reload and refuses another actor or changed input", () => {
  const input = functionalInput(review, "3".repeat(32), null, 0, 15, true, 0);
  const raw = JSON.stringify({ version: 1, scope, input });
  expect(readFunctionalAttempt(raw, scope)?.input).toEqual(input);
  expect(() =>
    readFunctionalAttempt(raw, { ...scope, actor: "4".repeat(32) }),
  ).toThrow();
  expect(() =>
    readFunctionalAttempt(
      JSON.stringify({
        version: 1,
        scope,
        input: { ...input, max_model_turns: 4 },
      }),
      scope,
    ),
  ).toThrow();
  expect(readFunctionalAttempt(null, scope)).toBeNull();
});
