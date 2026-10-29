/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  continuityPolicy,
  continuityPolicyView,
  readPendingPolicy,
  sameContinuityPolicy,
} from "./ContinuityPolicyData";
const settings = {
  version: 1 as const,
  enabled: true,
  allow_unavailable_source_observations: true,
  allow_pre_admission_recovery: true,
  allow_readonly_observation_recovery: true,
  allow_checkpoint_continuation: false,
  max_window_seconds: 180,
  max_tickets_per_run: 73,
  max_pending_tickets: 601,
};
const context = {
  base: "https://factory.example/api/v1/",
  actor: "1".repeat(32),
  authentication: "local_bootstrap",
};
it("supports final-result recovery without changing an absent legacy flag", () => {
  const retained = continuityPolicy(settings);
  expect(retained).not.toHaveProperty("allow_final_outcome_continuation");
  const enabled = continuityPolicy({
    ...settings,
    allow_final_outcome_continuation: true,
  });
  expect(sameContinuityPolicy(retained, enabled)).toBe(false);
  expect(sameContinuityPolicy(enabled, retained)).toBe(false);
  expect(
    sameContinuityPolicy(
      retained,
      continuityPolicy({
        ...settings,
        allow_final_outcome_continuation: false,
      }),
    ),
  ).toBe(true);
  expect(() =>
    continuityPolicy({ ...settings, allow_final_outcome_continuation: 1 }),
  ).toThrow();
});
it("keeps every saved policy option when editing the checkpoint switch and window", () => {
  const p = continuityPolicy(settings);
  const next = continuityPolicy({
    ...p,
    allow_checkpoint_continuation: true,
    max_window_seconds: 120,
  });
  expect(next.max_tickets_per_run).toBe(73);
  expect(next.max_pending_tickets).toBe(601);
  expect(next.allow_readonly_observation_recovery).toBe(true);
  expect(sameContinuityPolicy(p, next)).toBe(false);
  expect(sameContinuityPolicy(p, { ...p })).toBe(true);
  expect(
    continuityPolicyView({
      version: 1,
      revision: null,
      digest: "a".repeat(64),
      settings: p,
    }).settings,
  ).toEqual(p);
});
it.each([
  { version: true },
  { allow_checkpoint_continuation: 1 },
  { max_window_seconds: "180" },
  { max_window_seconds: 7201 },
  { max_tickets_per_run: 0 },
  { extra: true },
])("rejects invalid policy %j", (change) => {
  expect(() => continuityPolicy({ ...settings, ...change })).toThrow();
});
it("binds an uncertain configuration to the original server and current actor", () => {
  const raw = JSON.stringify({ version: 1, context, settings });
  expect(readPendingPolicy(raw, context)?.settings).toEqual(settings);
  expect(() =>
    readPendingPolicy(raw, { ...context, actor: "2".repeat(32) }),
  ).toThrow();
  expect(() =>
    readPendingPolicy(raw, {
      ...context,
      base: "https://other.example/api/v1/",
    }),
  ).toThrow();
});
