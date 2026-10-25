/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  executionPolicy,
  executionCampaign,
  positiveInteger,
  renewalBody,
} from "./CampaignExecutionData";
const project = "a".repeat(32),
  id = "b".repeat(32);
const policy = {
  version: 1,
  project_id: project,
  policy: { enabled: true, dispatch_window: 5000, max_parallel_runs: 128 },
  digest: null,
  effective_digest: "e".repeat(64),
  request_id: null,
  permissions: { configure: true },
};
const campaign = {
  id,
  project_id: project,
  status: "paused",
  control: null,
  launch: { publish: false },
  journal: { verified: true, head: "f".repeat(64) },
  children: [
    { current: true, status: "paused" },
    { current: true, status: "interrupted" },
    { current: true, status: "campaign_blocked" },
    { current: false, status: "running" },
  ],
  execution_budget_state: {
    version: 1,
    granted_dispatches: 50,
    consumed_dispatches: 50,
    remaining_dispatches: 0,
    max_parallel_runs: 4,
  },
};
it("accepts positive finite integer windows beyond former business ceilings", () => {
  expect(executionPolicy(policy, project).policy.dispatch_window).toBe(5000);
  expect(positiveInteger("5000")).toBe(5000);
  for (const value of [
    "",
    "0",
    "-1",
    "1.5",
    "Infinity",
    "1e2",
    "9007199254740993",
  ])
    expect(() => positiveInteger(value)).toThrow();
});
it("binds current policy identity and rejects malformed permissions or numbers", () => {
  for (const value of [
    { ...policy, project_id: "c".repeat(32) },
    { ...policy, version: 2 },
    { ...policy, permissions: { configure: "true" } },
    { ...policy, policy: { ...policy.policy, max_parallel_runs: 0 } },
    { ...policy, digest: "d".repeat(64) },
  ])
    expect(() => executionPolicy(value, project)).toThrow();
});
it("counts current held reservations including interrupted and paused work", () => {
  const result = executionCampaign(campaign, project, id);
  expect(result.held).toBe(2);
  expect(result.budget?.consumed_dispatches).toBe(50);
});
it("refuses unverified, mismatched or inconsistent budget records", () => {
  for (const value of [
    { ...campaign, project_id: "c".repeat(32) },
    { ...campaign, id: "d".repeat(32) },
    { ...campaign, journal: { ...campaign.journal, verified: false } },
    {
      ...campaign,
      execution_budget_state: {
        ...campaign.execution_budget_state,
        remaining_dispatches: 1,
      },
    },
  ])
    expect(() => executionCampaign(value, project, id)).toThrow();
});
it("builds an additive renewal from the exact reviewed policy and journal head", () => {
  const current = executionCampaign(campaign, project, id),
    saved = executionPolicy(policy, project);
  expect(renewalBody(current, saved, "1".repeat(32), "25", "8")).toEqual({
    request_id: "1".repeat(32),
    expected_journal_head: "f".repeat(64),
    expected_policy_digest: "e".repeat(64),
    additional_dispatches: 25,
    max_parallel_runs: 8,
  });
  for (const [amount, parallel] of [
    ["5001", "8"],
    ["25", "129"],
  ])
    expect(() =>
      renewalBody(current, saved, "1".repeat(32), amount, parallel),
    ).toThrow();
  expect(() =>
    renewalBody(
      { ...current, control: "cancelled" },
      saved,
      "1".repeat(32),
      "25",
      "8",
    ),
  ).toThrow();
  expect(() =>
    renewalBody(
      current,
      { ...saved, policy: { ...saved.policy, enabled: false } },
      "1".repeat(32),
      "25",
      "8",
    ),
  ).toThrow();
});
