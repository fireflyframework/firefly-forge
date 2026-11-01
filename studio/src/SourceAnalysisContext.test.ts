/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { ProductDraftBook } from "./ProductDrafts";
import {
  assistantRequestCustody,
  reviewedSourcePayload,
  matchesReviewedSource,
  type ReviewedSourceContext,
} from "./SourceAnalysisContext";

const source: ReviewedSourceContext = {
  project_id: "a".repeat(32),
  analysis_id: "b".repeat(32),
  analysis_digest: "c".repeat(64),
  analysis_component_ids: ["stock-api", "warehouse-web"],
};
const snapshot = {
  project_id: source.project_id,
  analysis_id: source.analysis_id,
  analysis_digest: source.analysis_digest,
  context_digest: "d".repeat(64),
  source_reviewed: true,
  business_journeys_confirmed: false,
  live_behavior_verified: false,
  components: [{ id: "stock-api" }, { id: "warehouse-web" }],
  included_facts: 3,
  omitted_facts: 2,
  truncated: true,
  citations: [],
};

it("keeps explicit component slugs without empty-all fallback or a total ceiling", () => {
  expect(reviewedSourcePayload(source, source.project_id)).toEqual(source);
  const large = {
    ...source,
    analysis_component_ids: Array.from(
      { length: 5000 },
      (_, index) => `service-${index}`,
    ),
  };
  expect(
    reviewedSourcePayload(large, source.project_id).analysis_component_ids,
  ).toEqual(large.analysis_component_ids);
  for (const analysis_component_ids of [
    [],
    ["stock-api", "stock-api"],
    ["../foreign"],
    [""],
  ])
    expect(() =>
      reviewedSourcePayload(
        { ...source, analysis_component_ids },
        source.project_id,
      ),
    ).toThrow();
  expect(() => reviewedSourcePayload(source, "e".repeat(32))).toThrow();
});

it("accepts only the created conversation's exact reviewed snapshot and actual coverage", () => {
  expect(matchesReviewedSource(snapshot, source)).toBe(true);
  for (const mutation of [
    { analysis_id: "e".repeat(32) },
    { analysis_digest: "e".repeat(64) },
    { project_id: "e".repeat(32) },
    { components: [{ id: "stock-api" }] },
    { components: [{ id: "stock-api" }, { id: "stock-api" }] },
    { source_reviewed: false },
    { business_journeys_confirmed: true },
    { included_facts: -1 },
    { truncated: false },
  ])
    expect(matchesReviewedSource({ ...snapshot, ...mutation }, source)).toBe(
      false,
    );
});

it("retains exact pending request identities across navigation and product draft replacement", () => {
  const book = new ProductDraftBook();
  const custody = assistantRequestCustody(
    book,
    "f".repeat(32),
    source.project_id,
  );
  custody.markCreationPending();
  custody.markTurnPending("conversation-a", "request-a");
  book.discard(book.repository("f".repeat(32)));
  const reopened = assistantRequestCustody(
    book,
    "f".repeat(32),
    source.project_id,
  );
  expect(reopened).toBe(custody);
  expect(reopened.getSnapshot().creationUncertain).toBe(true);
  reopened.confirmTurn("conversation-a", "foreign-request");
  expect(reopened.getSnapshot().uncertainRequests).toEqual({
    "conversation-a": "request-a",
  });
  reopened.confirmTurn("conversation-a", "request-a");
  expect(reopened.getSnapshot().uncertainRequests).toEqual({});
  reopened.selectRecordedConversation();
  expect(reopened.getSnapshot().creationUncertain).toBe(false);
  expect(
    assistantRequestCustody(book, "f".repeat(32), "e".repeat(32)),
  ).not.toBe(custody);
  expect(
    assistantRequestCustody(
      new ProductDraftBook(),
      "f".repeat(32),
      source.project_id,
    ),
  ).not.toBe(custody);
});
