/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { communicationConfiguration, communicationBindings, communicationStatus } from "./CommunicationSettingsData";
const route = { revision: 2, digest: "a".repeat(64), bindings: { implementation: "coding", communication: "updates" } };
const profiles = [{ id: "updates", name: "Project updates", provider: "bridge", tasks: ["communication"] }, { id: "coding", name: "Coding", provider: "codex", tasks: ["implementation"] }];
it("selects only compatible explicit profiles and preserves other task bindings", () => {
  const configuration = communicationConfiguration(profiles, route);
  expect(configuration.profiles.map((profile) => profile.id)).toEqual(["updates"]);
  expect(communicationBindings(configuration, "")).toEqual({ implementation: "coding" });
  expect(communicationBindings(configuration, "updates")).toEqual(route.bindings);
  expect(() => communicationBindings(configuration, "coding")).toThrow();
});
it("holds malformed current routing rather than clearing or saving it", () => {
  for (const value of [{ ...route, revision: true }, { ...route, bindings: { unknown: "updates" } }, { ...route, digest: "invalid" }, { ...route, bindings: { communication: "removed" } }]) expect(() => communicationConfiguration(profiles, value)).toThrow();
});
it("distinguishes actual AI wording from visible deterministic fallback reasons", () => {
  expect(communicationStatus({ mode: "ai", reason: "generated" })).toBe("AI wording · recorded facts");
  expect(communicationStatus({ mode: "deterministic", reason: "quota" })).toContain("usage limit");
  expect(communicationStatus({ mode: "deterministic", reason: "state_changed" })).toContain("state changed");
  expect(communicationStatus(undefined)).toBe("");
});
