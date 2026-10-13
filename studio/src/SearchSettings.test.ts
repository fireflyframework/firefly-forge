/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  searchPayload,
  readSearchSettings,
  searchError,
} from "./SearchSettings";

const legacy = {
  provider: "brave",
  endpoint: "https://api.search.brave.com/res/v1/web/search",
  revision: "a".repeat(32),
  configured: true,
  max_results: 10,
  live_search_verified: true,
};
describe("search configuration boundaries", () => {
  it("omits an unchanged legacy provider and blank key rather than replacing saved credentials", () => {
    const current = readSearchSettings(Object.freeze(legacy));
    expect(
      searchPayload(current, {
        provider: "brave",
        key: "",
        domains: "",
        remove: false,
      }),
    ).toEqual({});
    expect(legacy.live_search_verified).toBe(true);
  });
  it("requires a new key when switching provider", () => {
    const current = readSearchSettings(legacy);
    expect(() =>
      searchPayload(current, {
        provider: "tavily",
        key: "",
        domains: "",
        remove: false,
      }),
    ).toThrow(/new.*key/i);
    expect(
      searchPayload(current, {
        provider: "tavily",
        key: "new-fixture-key",
        domains: "",
        remove: false,
      }),
    ).toEqual({ provider: "tavily", api_key: "new-fixture-key" });
  });
  it("normalizes and deduplicates hostnames while preserving domain removal", () => {
    const current = readSearchSettings({
      ...legacy,
      include_domains: ["github.com"],
    });
    expect(
      searchPayload(current, {
        provider: "brave",
        key: "",
        domains: "FireflyFramework.org.\nfireflyframework.org\nDocs.GitHub.com",
        remove: false,
      }),
    ).toEqual({ include_domains: ["fireflyframework.org", "docs.github.com"] });
    expect(
      searchPayload(current, {
        provider: "brave",
        key: "",
        domains: "",
        remove: false,
      }),
    ).toEqual({ include_domains: [] });
  });
  it("rejects URLs, IPs, local names, wildcards and more than ten hostnames", () => {
    const current = readSearchSettings(legacy);
    for (const domains of [
      "https://github.com",
      "127.0.0.1",
      "office.local",
      "*.github.com",
      "host.internal",
      "github.com/path",
      Array.from({ length: 11 }, (_, i) => `host${i}.example.com`).join("\n"),
    ]) {
      expect(() =>
        searchPayload(current, {
          provider: "brave",
          key: "",
          domains,
          remove: false,
        }),
      ).toThrow();
    }
  });
  it("does not accept unknown provider or misleading readiness types", () => {
    for (const value of [
      { ...legacy, provider: "extension" },
      { ...legacy, configured: "true" },
      { ...legacy, live_search_verified: "true" },
      { ...legacy, revision: "unexpected" },
      null,
    ])
      expect(() => readSearchSettings(value)).toThrow();
  });
  it("requires a key for new setup and rejects combined removal and rotation", () => {
    const current = readSearchSettings({
      ...legacy,
      configured: false,
      revision: null,
      live_search_verified: false,
    });
    expect(() =>
      searchPayload(current, {
        provider: "tavily",
        key: "",
        domains: "",
        remove: false,
      }),
    ).toThrow();
    expect(() =>
      searchPayload(readSearchSettings(legacy), {
        provider: "brave",
        key: "replacement",
        domains: "",
        remove: true,
      }),
    ).toThrow();
    expect(
      searchPayload(readSearchSettings(legacy), {
        provider: "brave",
        key: "",
        domains: "",
        remove: true,
      }),
    ).toEqual({ clear_secret: true });
  });
  it("redacts an entered key before bounding malicious error text", () => {
    const value = searchError(
      new Error("fixture-key <img onerror=alert(1)> " + "x".repeat(200000)),
      "fixture-key",
    );
    expect(value).not.toContain("fixture-key");
    expect(value.length).toBeLessThanOrEqual(805);
    expect(value).toContain("[redacted]");
  });
});
