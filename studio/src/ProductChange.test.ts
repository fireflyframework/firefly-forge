/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import type {
  CustomerProductDefinition,
  FunctionalFeature,
  ProductSnapshot,
} from "./api";
import { ProductDraftBook } from "./ProductDrafts";
import {
  buildProductChange,
  previewProductChange,
  saveProductChange,
} from "./ProductChange";
import { reconcileProductSave, saveProductDefinition } from "./ProductSave";
const repo = "a".repeat(32),
  id = "b".repeat(32);
const feature = (id: string): FunctionalFeature => ({
  id,
  name: id,
  journey_refs: ["Read stock"],
  acceptance_refs: ["acceptance-1"],
  component_refs: [],
  behaviors: [
    {
      id: "visible",
      given: "Stock exists",
      when: "Read stock",
      then: "Show stock",
      failure_paths: ["Show an unavailable message"],
    },
  ],
});
const definition: CustomerProductDefinition = {
  customer: "Operators",
  problem: "Read stock safely",
  journey: ["Read stock"],
  user_stories: ["Read my stock"],
  acceptance: ["Show available stock"],
  owners: ["Owner"],
  security_requirements: [],
  nonfunctional_requirements: [],
  features: [feature("read"), feature("keep")],
  integrations: [],
  functional_reviewed: true,
};
const base: ProductSnapshot = {
  repository_id: repo,
  revision: 1,
  digest: "1".repeat(64),
  created_at: "2026-10-17T00:00:00Z",
  definition,
};
const connection = { base: "/api/v1", token: "fixture" };
function setup() {
  const session = new ProductDraftBook().repository(repo);
  session.observe(base, null);
  const input = structuredClone(definition);
  input.features![0].name = "Read with freshness";
  session.write("features", input.features, true);
  return { session, input };
}
const impact = {
  changed_feature_ids: ["read"],
  unchanged_feature_ids: ["keep"],
  unchanged_feature_digests: { keep: "d".repeat(64) },
  changed_integration_ids: [],
  affected_journeys: ["Read stock"],
  regression_acceptance_refs: ["acceptance-1"],
  reviews_required: ["functional", "design"],
};
function previewResponse(input: CustomerProductDefinition) {
  return {
    base_revision: 1,
    base_digest: base.digest,
    definition: {
      ...structuredClone(input),
      functional_reviewed: false,
      design_review: null,
    },
    impact,
    implementation_status: "not_started",
  };
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
it("derives only changed stable IDs and append-only entries without replacing unaffected contracts", () => {
  const { input } = setup();
  input.journey.push("Explain stale stock");
  input.acceptance.push("Explain freshness");
  const change = buildProductChange(base, input, id, "Explain freshness");
  expect(change).toEqual({
    request_id: id,
    base_revision: 1,
    base_digest: base.digest,
    reason: "Explain freshness",
    feature_upserts: [{ ...feature("read"), name: "Read with freshness" }],
    remove_feature_ids: [],
    integration_upserts: [],
    remove_integration_ids: [],
    append_journeys: ["Explain stale stock"],
    append_acceptance: ["Explain freshness"],
    append_user_stories: [],
  });
  expect(definition.features![0].name).toBe("read");
});
it("refuses unrelated context edits, reordered acceptance, duplicate IDs and no-op patches", () => {
  for (const edit of [
    (x: CustomerProductDefinition) => {
      x.customer = "Other team";
    },
    (x: CustomerProductDefinition) => {
      x.acceptance = ["Replacement"];
    },
    (x: CustomerProductDefinition) => {
      x.features!.push(feature("read"));
    },
  ]) {
    const { input } = setup();
    edit(input);
    expect(() => buildProductChange(base, input, id, "Change")).toThrow();
  }
  expect(() => buildProductChange(base, definition, id, "Change")).toThrow(
    /no|change/i,
  );
});
it("preview sends only the patch and refuses a response after the draft or base changes", async () => {
  const { session, input } = setup();
  let resolve!: (r: Response) => void;
  const fetch = vi.fn<(...args: [string, RequestInit?]) => Promise<Response>>(
    () =>
      new Promise<Response>((done) => {
        resolve = done;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const pending = previewProductChange(session, connection, input, "Freshness");
  void pending.catch(() => {});
  await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(fetch.mock.calls[0][0]).toBe(
    `/api/v1/repositories/${repo}/product/changes/preview`,
  );
  session.write("customer", "Newer edit", true);
  resolve(new Response(JSON.stringify(previewResponse(input))));
  await expect(pending).rejects.toThrow(/changed|draft/i);
  expect(session.saveHeld).toBe(false);
});
it("holds the original mutation across response loss and ordinary save; readback requires its exact receipt", async () => {
  const { session, input } = setup();
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(previewResponse(input))))
    .mockRejectedValueOnce(new TypeError("Lost response"));
  vi.stubGlobal("fetch", fetch);
  const preview = await previewProductChange(
    session,
    connection,
    input,
    "Freshness",
  );
  await expect(
    saveProductChange(session, connection, preview),
  ).rejects.toThrow();
  const attempt = session.saveAttempt!;
  expect(attempt.change?.request_id).toMatch(/^[a-f0-9]{32}$/);
  expect(attempt.change?.request_digest).toMatch(/^[a-f0-9]{64}$/);
  expect(JSON.parse(attempt.body).request_id).toBe(attempt.change?.request_id);
  expect(session.saveHeld).toBe(true);
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow(/retained|save/i);
  fetch.mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        ...base,
        revision: 2,
        definition: preview.definition,
        digest: "2".repeat(64),
      }),
    ),
  );
  expect(await reconcileProductSave(session, connection)).toBeNull();
  expect(session.saveHeld).toBe(true);
  fetch.mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        ...base,
        revision: 2,
        definition: preview.definition,
        digest: "2".repeat(64),
        change_receipt: {
          ...attempt.change,
          base_revision: 1,
          base_digest: base.digest,
          implementation_status: "not_started",
        },
      }),
    ),
  );
  expect((await reconcileProductSave(session, connection))?.revision).toBe(2);
  expect(session.saveHeld).toBe(false);
  expect(
    fetch.mock.calls.filter(([, options]) => options?.method === "POST"),
  ).toHaveLength(2);
});
it("only an explicit retry sends the same original mutation after newer edits", async () => {
  const { session, input } = setup();
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(previewResponse(input))))
    .mockRejectedValueOnce(new TypeError("Lost response"));
  vi.stubGlobal("fetch", fetch);
  const preview = await previewProductChange(
    session,
    connection,
    input,
    "Freshness",
  );
  await expect(
    saveProductChange(session, connection, preview),
  ).rejects.toThrow();
  const original = session.saveAttempt!;
  session.write("customer", "Later draft", true);
  fetch.mockResolvedValueOnce(
    new Response(
      JSON.stringify({
        ...base,
        revision: 2,
        definition: preview.definition,
        digest: "2".repeat(64),
        change_receipt: {
          ...original.change,
          base_revision: 1,
          base_digest: base.digest,
          implementation_status: "not_started",
        },
      }),
    ),
  );
  await saveProductDefinition(session, connection, input, true);
  expect(fetch.mock.calls[2][0]).toBe(
    `/api/v1/repositories/${repo}/product/changes`,
  );
  expect(fetch.mock.calls[2][1].body).toBe(original.body);
  expect(session.read("customer", "")).toBe("Later draft");
  expect(session.dirty).toBe(true);
  expect(session.saveHeld).toBe(false);
});
it("normalizes a new integration's omitted wire defaults before binding the request digest", () => {
  const { input } = setup();
  input.integrations = [
    {
      id: "catalog",
      name: "Catalog",
      kind: "third_party",
      protocol: "http",
      endpoint: "https://catalog.example/v1/stock",
      service_ref: null,
      authentication: { method: "none", secret_refs: [] },
      inputs: [{ name: "sku", type: "string" }],
      outputs: [{ name: "stock", type: "integer" }],
      failure_paths: ["Explain unavailability"],
      retry: { max_attempts: 1, backoff_seconds: 0, retry_on: [] },
      idempotency: {
        mode: "not_applicable",
        key_field: null,
        key_location: "header",
      },
      side_effects: false,
      feature_refs: ["read"],
      journey_refs: ["Read stock"],
    },
  ];
  const change = buildProductChange(base, input, id, "Catalog");
  expect(change.integration_upserts[0].header_names).toEqual([]);
  expect(change.integration_upserts[0].inputs[0]).toEqual({
    name: "sku",
    type: "string",
    required: true,
    description: "",
  });
});
it("binds the normalized patch with the server's canonical request digest", async () => {
  const { session, input } = setup();
  vi.spyOn(crypto, "randomUUID").mockReturnValue(
    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
  );
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(previewResponse(input)))),
  );
  const preview = await previewProductChange(
    session,
    connection,
    input,
    "Freshness",
  );
  expect(preview.binding.request_digest).toBe(
    "0cd6494bd91fd12f5b523371d53cf9ad22ae420efbf16263585134164f993b79",
  );
  vi.restoreAllMocks();
});
it.each(["draft", "revision", "identity"])(
  "refuses to apply an invalidated %s preview without sending a mutation",
  async (drift) => {
    const { session, input } = setup();
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(previewResponse(input))));
    vi.stubGlobal("fetch", fetch);
    const preview = await previewProductChange(
      session,
      connection,
      input,
      "Freshness",
    );
    if (drift === "draft") session.write("customer", "Later editing", true);
    else if (drift === "revision")
      session.observe({ ...base, revision: 2, digest: "2".repeat(64) }, null);
    else session.close();
    await expect(
      saveProductChange(session, connection, preview),
    ).rejects.toThrow(/preview|current/i);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(session.saveHeld).toBe(false);
  },
);
it.each(["request_id", "request_digest", "base_digest", "definition"])(
  "holds a successful-looking mutation with the wrong %s binding",
  async (drift) => {
    const { session, input } = setup();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(previewResponse(input))),
      );
    vi.stubGlobal("fetch", fetch);
    const preview = await previewProductChange(
      session,
      connection,
      input,
      "Freshness",
    );
    const result = {
      ...base,
      revision: 2,
      digest: "2".repeat(64),
      definition: preview.definition,
      change_receipt: {
        ...preview.binding,
        base_revision: 1,
        base_digest: base.digest,
        implementation_status: "not_started",
      },
    };
    if (drift === "definition")
      result.definition = {
        ...preview.definition,
        customer: "Foreign content",
      };
    else
      result.change_receipt[
        drift as "request_id" | "request_digest" | "base_digest"
      ] = "c".repeat(drift === "request_id" ? 32 : 64);
    fetch.mockResolvedValueOnce(new Response(JSON.stringify(result)));
    await expect(
      saveProductChange(session, connection, preview),
    ).rejects.toThrow(/match|original/i);
    expect(session.saveHeld).toBe(true);
    expect(session.base?.revision).toBe(1);
    expect(fetch).toHaveBeenCalledTimes(2);
  },
);
it("refuses a changed retained preview body before any mutation", async () => {
  const { session, input } = setup();
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify(previewResponse(input))));
  vi.stubGlobal("fetch", fetch);
  const preview = await previewProductChange(
    session,
    connection,
    input,
    "Freshness",
  );
  preview.body = preview.body.replace(
    "Read with freshness",
    "An unreviewed change",
  );
  await expect(
    saveProductChange(session, connection, preview),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(session.saveHeld).toBe(false);
});
