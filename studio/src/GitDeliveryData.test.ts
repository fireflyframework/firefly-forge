/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import fixture from "../tests/git-delivery-contract-fixture.json";
import {
  deliveryList,
  policyView,
  policyRequest,
  policyAcknowledgment,
} from "./GitDeliveryData";

it("reads actual Python policy serialization without enabling existing projects", () => {
  const value = policyView(fixture.policy_default, fixture.project_id);
  expect(value.policy.enabled).toBe(false);
  expect(value.digest).toBeNull();
  expect(value.remote_capabilities[0].provider).toBe("gitlab");
});

it("binds a changed policy to its exact previous digest and fresh request", () => {
  const current = policyView(fixture.policy_default, fixture.project_id);
  expect(
    policyRequest(current, fixture.policy_enabled.policy, "6".repeat(32)),
  ).toEqual({
    version: 1,
    request_id: "6".repeat(32),
    expected_previous_digest: null,
    policy: {
      version: 1,
      enabled: true,
      every_units: 1,
      on_pause: true,
      remote: "draft_pr",
      max_checkpoints: 50,
    },
  });
});

it("refuses local configuration and remote publication without the advertised current permissions", () => {
  const current = policyView(fixture.policy_default, fixture.project_id);
  expect(() =>
    policyRequest(
      { ...current, permissions: { configure: false, publish: true } },
      fixture.policy_enabled.policy,
      "6".repeat(32),
    ),
  ).toThrow();
  expect(() =>
    policyRequest(
      { ...current, permissions: { configure: true, publish: false } },
      fixture.policy_enabled.policy,
      "6".repeat(32),
    ),
  ).toThrow();
  expect(() =>
    policyRequest(
      {
        ...current,
        remote_capabilities: [
          { ...current.remote_capabilities[0], draft_updates: false },
        ],
      },
      fixture.policy_enabled.policy,
      "6".repeat(32),
    ),
  ).toThrow();
});

it("does not confirm a save from a foreign, stale or mismatched acknowledgment", () => {
  const current = policyView(fixture.policy_default, fixture.project_id);
  const desired = fixture.policy_enabled.policy;
  const matching = { ...fixture.policy_enabled, request_id: "6".repeat(32) };
  expect(
    policyAcknowledgment(matching, current, desired, "6".repeat(32)).policy
      .enabled,
  ).toBe(true);
  for (const changed of [
    { ...matching, project_id: "8".repeat(32) },
    { ...matching, request_id: "7".repeat(32) },
    { ...matching, digest: null },
    { ...matching, policy: fixture.policy_default.policy },
  ]) {
    expect(() =>
      policyAcknowledgment(changed, current, desired, "6".repeat(32)),
    ).toThrow();
  }
});

it("reads each real remote-state serialization without treating a draft as ready", () => {
  for (const status of [
    "not_requested",
    "publishing",
    "draft",
    "unknown",
    "ready",
  ] as const) {
    const value = deliveryList(fixture.deliveries[status], fixture.run_id);
    expect(value.items[0].remote_status).toBe(status);
    expect(value.items[0].revision).toBe("b".repeat(40));
    expect(value.items[0].changed_file_count).toBe(7);
  }
});

it("refuses receipts or policy envelopes outside their exact requested scope", () => {
  expect(() => policyView(fixture.policy_default, "8".repeat(32))).toThrow();
  expect(() =>
    deliveryList(fixture.deliveries.draft, "8".repeat(32)),
  ).toThrow();
  expect(() =>
    deliveryList(
      {
        ...fixture.deliveries.draft,
        items: [
          { ...fixture.deliveries.draft.items[0], run_id: "8".repeat(32) },
        ],
      },
      fixture.run_id,
    ),
  ).toThrow();
});

it("refuses unknown states, bool-like values, excessive histories and invalid commit identity", () => {
  const initial = fixture.policy_default;
  for (const changed of [
    { ...initial, version: true },
    { ...initial, policy: { ...initial.policy, enabled: "false" } },
    { ...initial, policy: { ...initial.policy, max_checkpoints: 51 } },
    { ...initial, permissions: { configure: "true", publish: true } },
  ]) {
    expect(() => policyView(changed, fixture.project_id)).toThrow();
  }
  const list = fixture.deliveries.draft;
  for (const changed of [
    { ...list, items: Array(51).fill(list.items[0]) },
    { ...list, items: [{ ...list.items[0], remote_status: "passed" }] },
    { ...list, items: [{ ...list.items[0], revision: "not-a-commit" }] },
    { ...list, latest_checkpoint: "f".repeat(40) },
  ]) {
    expect(() => deliveryList(changed, fixture.run_id)).toThrow();
  }
});

it("accepts the backend's nullable or empty optional display text without treating it as a link or authority", () => {
  const view = {
    ...fixture.policy_default,
    remote_capabilities: [
      { ...fixture.policy_default.remote_capabilities[0], reason: "" },
    ],
  };
  expect(
    policyView(view, fixture.project_id).remote_capabilities[0].reason,
  ).toBe("");
  const list = {
    ...fixture.deliveries.not_requested,
    items: [{ ...fixture.deliveries.not_requested.items[0], pr_url: "" }],
  };
  expect(deliveryList(list, fixture.run_id).items[0].pr_url).toBe("");
});

it("preserves a one-slot policy without silently increasing the native checkpoint allowance", () => {
  const current = policyView(fixture.policy_default, fixture.project_id);
  const policy = { ...fixture.policy_enabled.policy, max_checkpoints: 1 };
  expect(
    policyRequest(current, policy, "6".repeat(32)).policy.max_checkpoints,
  ).toBe(1);
});

it("allows a current configuration administrator to stop existing remote checkpoints without release permission", () => {
  const current = policyView(
    {
      ...fixture.policy_enabled,
      permissions: { configure: true, publish: false },
    },
    fixture.project_id,
  );
  const desired = { ...current.policy, enabled: false };
  expect(policyRequest(current, desired, "6".repeat(32)).policy.enabled).toBe(
    false,
  );
  expect(() =>
    policyRequest(current, { ...desired, enabled: true }, "6".repeat(32)),
  ).toThrow();
});
