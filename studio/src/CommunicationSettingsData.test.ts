/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  communicationConfiguration,
  communicationBindings,
  communicationSaved,
  communicationStatus,
} from "./CommunicationSettingsData";
const route = {
  revision: 2,
  digest: "a".repeat(64),
  bindings: { implementation: "coding", communication: "updates" },
};
const profiles = [
  {
    id: "updates",
    name: "Project updates",
    provider: "bridge",
    tasks: ["communication"],
  },
  {
    id: "coding",
    name: "Coding",
    provider: "codex",
    tasks: ["implementation"],
  },
];
it("selects only compatible explicit profiles and preserves other task bindings", () => {
  const configuration = communicationConfiguration(profiles, route);
  expect(configuration.profiles.map((profile) => profile.id)).toEqual([
    "updates",
  ]);
  expect(communicationBindings(configuration, "")).toEqual({
    implementation: "coding",
  });
  expect(communicationBindings(configuration, "updates")).toEqual(
    route.bindings,
  );
  expect(() => communicationBindings(configuration, "coding")).toThrow();
});
it("holds malformed current routing rather than clearing or saving it", () => {
  for (const value of [
    { ...route, revision: true },
    { ...route, bindings: { unknown: "updates" } },
    { ...route, digest: "invalid" },
    { ...route, bindings: { communication: "removed" } },
  ])
    expect(() => communicationConfiguration(profiles, value)).toThrow();
});
it("distinguishes actual AI wording from visible deterministic fallback reasons", () => {
  expect(communicationStatus({ mode: "ai", reason: "generated" })).toBe(
    "AI wording · recorded facts",
  );
  expect(
    communicationStatus({ mode: "deterministic", reason: "quota" }),
  ).toContain("usage limit");
  expect(
    communicationStatus({ mode: "deterministic", reason: "state_changed" }),
  ).toContain("state changed");
  expect(communicationStatus(undefined)).toBe("");
});
it("accepts a save acknowledgment only for the exact preserved routing and a newer revision", () => {
  const configuration = communicationConfiguration(profiles, route);
  const desired = { implementation: "coding" };
  expect(
    communicationSaved(
      { revision: 3, digest: "b".repeat(64), bindings: desired },
      configuration,
      desired,
    ).bindings,
  ).toEqual(desired);
  for (const value of [
    { revision: 2, digest: route.digest, bindings: desired },
    { revision: 3, digest: "b".repeat(64), bindings: route.bindings },
    { revision: true, digest: "b".repeat(64), bindings: desired },
    {},
  ]) {
    expect(() => communicationSaved(value, configuration, desired)).toThrow();
  }
});

it("shows held AI delivery with bounded reasons while keeping historical recorded receipts", () => {
  expect(communicationStatus({ mode: "held", reason: "quota" })).toBe(
    "AI update held · provider usage limit reached",
  );
  expect(communicationStatus({ mode: "held", reason: "state_changed" })).toBe(
    "AI update held · recorded facts changed while drafting",
  );
  expect(
    communicationStatus({ mode: "held", reason: "private raw provider error" }),
  ).toBe("AI update held");
  expect(communicationStatus({ mode: "deterministic", reason: "quota" })).toBe(
    "Recorded update · provider usage limit reached",
  );
});

it("keeps inherited object names outside native communication reason labels", () => {
  for (const reason of ["constructor", "__proto__", "toString"]) {
    expect(communicationStatus({ mode: "held", reason })).toBe(
      "AI update held",
    );
    expect(communicationStatus({ mode: "deterministic", reason })).toBe(
      "Recorded update",
    );
  }
});
