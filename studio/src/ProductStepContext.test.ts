/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import { buildStepPrompt } from "./ProductStepContext";
import type { CustomerProductDefinition } from "./api";
const definition: CustomerProductDefinition = {
  customer: "Warehouse operator",
  problem: "Reserve available inventory",
  journey: ["Reserve stock"],
  user_stories: ["Reserve without overselling"],
  acceptance: ["Reject excess quantity"],
  owners: ["Inventory team"],
  security_requirements: [],
  nonfunctional_requirements: [],
  features: [],
  integrations: [
    {
      id: "crm",
      name: "Customer records",
      kind: "third_party",
      protocol: "http",
      authentication: {
        method: "api_key",
        secret_refs: ["secret://private/API_KEY"],
      },
      inputs: [],
      outputs: [],
      failure_paths: [],
      retry: { max_attempts: 1, backoff_seconds: 0, retry_on: [] },
      idempotency: { mode: "not_applicable", key_location: "header" },
      feature_refs: [],
      journey_refs: [],
    },
  ],
};
describe("current step context", () => {
  it("marks the editor copy as unreviewed and includes only relevant readable fields", () => {
    const prompt = buildStepPrompt(3, definition);
    expect(prompt).toContain("Current editor content");
    expect(prompt).toContain("not automatically saved or reviewed");
    expect(prompt).toContain("Customer records");
    expect(prompt).not.toContain("secret://private");
    expect(prompt).not.toContain("API_KEY");
    expect(prompt).not.toContain('"authentication"');
  });
  it("bounds long narrative input and explicitly reports shortening", () => {
    const prompt = buildStepPrompt(4, {
      ...definition,
      problem: "x".repeat(20000),
    });
    expect(prompt.length).toBeLessThanOrEqual(12000);
    expect(prompt).toContain("Context shortened");
  });
});
