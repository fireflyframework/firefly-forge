/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { safeExternalLink } from "./FeatureLinks";
describe("provider and citation links", () => {
  it("permits recorded HTTP(S) links", () => {
    expect(safeExternalLink("https://git.example/project/issues/12")).toBe(
      "https://git.example/project/issues/12",
    );
    expect(safeExternalLink("http://127.0.0.1:8080/status")).toBe(
      "http://127.0.0.1:8080/status",
    );
  });
  it("rejects executable URLs, credentials and relative invented links", () => {
    for (const value of [
      "javascript:alert(1)",
      "data:text/html,unsafe",
      "https://user:secret@git.example/issue",
      "https://%75ser@git.example/issue",
      "/generated/issue/12",
      undefined,
      null,
    ])
      expect(safeExternalLink(value)).toBeNull();
  });
});
