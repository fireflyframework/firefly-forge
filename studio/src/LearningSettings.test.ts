/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { readLearningSettings, learningPayload } from "./LearningSettings";
const snapshot = {
  version: 1,
  revision: "a".repeat(32),
  digest: "b".repeat(64),
  settings: {
    enabled: true,
    projected_retrieval: false,
    max_age_hours: 72,
    max_candidates: 8,
    max_lookup_calls: 30,
    max_repeated_attempts: 2,
  },
};
describe("native learning policy contract", () => {
  it("preserves the actual captured policy and retained repair contract", () => {
    const saved = readLearningSettings(snapshot);
    expect(
      learningPayload(saved, {
        ...saved.settings,
        projected_retrieval: true,
        max_age_hours: 48,
      }),
    ).toEqual({
      ...snapshot.settings,
      projected_retrieval: true,
      max_age_hours: 48,
    });
    expect(snapshot.settings.projected_retrieval).toBe(false);
  });
  it("omits unsupported projection field on older backend responses", () => {
    const legacy = {
      enabled: true,
      max_age_hours: 72,
      max_candidates: 8,
      max_lookup_calls: 30,
      max_repeated_attempts: 2,
    };
    const saved = readLearningSettings({ ...snapshot, settings: legacy });
    expect(saved.projectedSupported).toBe(false);
    expect(
      learningPayload(saved, { ...saved.settings, max_age_hours: 24 }),
    ).toEqual({ ...legacy, max_age_hours: 24 });
    expect(() =>
      learningPayload(saved, { ...saved.settings, projected_retrieval: true }),
    ).toThrow(/upgrade/i);
  });
  it.each([
    { ...snapshot, version: 2 },
    { ...snapshot, revision: "invalid" },
    { ...snapshot, digest: "invalid" },
    { ...snapshot, settings: { ...snapshot.settings, enabled: "false" } },
    { ...snapshot, settings: { ...snapshot.settings, max_age_hours: "72" } },
    { ...snapshot, settings: { ...snapshot.settings, max_candidates: 17 } },
    { ...snapshot, settings: { ...snapshot.settings, max_lookup_calls: 0 } },
    {
      ...snapshot,
      settings: { ...snapshot.settings, max_repeated_attempts: 4 },
    },
    {
      ...snapshot,
      settings: { ...snapshot.settings, future_permission: true },
    },
    null,
  ])("rejects malformed or unknown policy without defaulting it: %j", (value) =>
    expect(() => readLearningSettings(value)).toThrow(/learning settings/i),
  );
  it("rejects noninteger and out-of-bound drafts before a save", () => {
    const saved = readLearningSettings(snapshot);
    for (const values of [
      { max_age_hours: 0 },
      { max_age_hours: 721 },
      { max_candidates: 1.5 },
      { max_lookup_calls: 101 },
    ])
      expect(() =>
        learningPayload(saved, { ...saved.settings, ...values }),
      ).toThrow();
  });
});
