/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import type { AIProposalProvenance, ProductSnapshot } from "./api";
import { ProductDraftBook, warnProductDraftUnload } from "./ProductDrafts";

const a = "a".repeat(32),
  b = "b".repeat(32);
const source: AIProposalProvenance = {
  kind: "ai_generated",
  provider: "bridge",
  model: "fictional-model",
  generated_at: "2026-10-16T00:00:00Z",
  conversation_id: "c".repeat(32),
  turn_id: "d".repeat(32),
  proposal_digest: "e".repeat(64),
};
const snapshot = (repository_id = a, revision = 1): ProductSnapshot => ({
  repository_id,
  revision,
  digest: String(revision).repeat(64),
  created_at: "2026-10-16T00:00:00Z",
  origin: { ...source, kind: "ai_assisted" },
  definition: {
    customer: "Saved customer",
    problem: "Saved problem",
    owners: ["Owner"],
    journey: [],
    user_stories: [],
    acceptance: [],
    security_requirements: [],
    nonfunctional_requirements: [],
    ai_origin: {
      conversation_id: source.conversation_id,
      turn_id: source.turn_id,
      proposal_digest: source.proposal_digest,
    },
  },
});

describe("tab-scoped product drafts", () => {
  it("retains raw human edits and the current step across A/B session re-entry", () => {
    const book = new ProductDraftBook(),
      first = book.repository(a);
    first.observe(snapshot(), null);
    first.write("customer", "  Human edit\n", true);
    first.write("step", 3);
    const second = book.repository(b);
    second.observe(snapshot(b), null);
    second.write("customer", "Different product", true);
    expect(book.repository(a).read("customer", "")).toBe("  Human edit\n");
    expect(book.repository(a).read("step", 0)).toBe(3);
    expect(book.repository(b).read("customer", "")).toBe("Different product");
    expect(book.hasUnsaved).toBe(true);
  });
  it("pins the original revision and AI attribution when a newer server cut is observed", () => {
    const session = new ProductDraftBook().repository(a);
    session.observe(snapshot(), null);
    session.write("problem", "Human refinement", true);
    const newer = snapshot(a, 2);
    newer.origin = { kind: "manual" };
    newer.definition.ai_origin = null;
    session.observe(newer, null);
    expect(session.base?.revision).toBe(1);
    expect(session.base?.definition.ai_origin?.turn_id).toBe(source.turn_id);
    expect(session.read("problem", "")).toBe("Human refinement");
    expect(session.stale).toBe(true);
    expect(session.dirty).toBe(true);
  });
  it("rejects a foreign repository read without replacing the current draft", () => {
    const session = new ProductDraftBook().repository(a);
    session.observe(snapshot(), null);
    session.write("customer", "Keep A", true);
    expect(() => session.observe(snapshot(b, 2), null)).toThrow(/repository/);
    expect(session.base?.repository_id).toBe(a);
    expect(session.read("customer", "")).toBe("Keep A");
  });
  it("discards only the selected session and fences its late save completion", () => {
    const book = new ProductDraftBook(),
      old = book.repository(a);
    old.observe(snapshot(), null);
    old.write("customer", "Old draft", true);
    const submitted = old.editVersion;
    const other = book.repository(b);
    other.write("customer", "Keep B", true);
    book.discard(old);
    expect(old.acceptSaved(snapshot(a, 2), submitted)).toBe(false);
    expect(book.repository(a)).not.toBe(old);
    expect(book.repository(a).read("customer", "")).toBe("");
    expect(other.read("customer", "")).toBe("Keep B");
    expect(other.dirty).toBe(true);
  });
  it("does not clear newer local edits when an earlier save finishes", () => {
    const session = new ProductDraftBook().repository(a);
    session.observe(snapshot(), null);
    session.write("customer", "Submitted", true);
    const submitted = session.editVersion;
    session.write("customer", "Newer typing", true);
    expect(session.acceptSaved(snapshot(a, 2), submitted)).toBe(false);
    expect(session.read("customer", "")).toBe("Newer typing");
    expect(session.dirty).toBe(true);
    expect(session.stale).toBe(true);
  });
  it("does not erase a newer observed server revision when an older save arrives", () => {
    const session = new ProductDraftBook().repository(a);
    session.observe(snapshot(), null);
    session.write("customer", "Submitted", true);
    const submitted = session.editVersion;
    session.observe(snapshot(a, 3), null);
    expect(session.acceptSaved(snapshot(a, 2), submitted)).toBe(false);
    expect(session.base?.revision).toBe(1);
    expect(session.stale).toBe(true);
    expect(session.dirty).toBe(true);
  });
  it("clears the exact submitted session after save without clearing another product", () => {
    const book = new ProductDraftBook(),
      session = book.repository(a);
    session.observe(snapshot(), null);
    session.write("customer", "Submitted", true);
    book.repository(b).write("customer", "Keep B", true);
    expect(session.acceptSaved(snapshot(a, 2), session.editVersion)).toBe(true);
    expect(session.base?.revision).toBe(2);
    expect(session.dirty).toBe(false);
    expect(book.hasUnsaved).toBe(true);
  });
  it("applies a reviewed AI proposal only to its exact session and keeps its source", () => {
    const book = new ProductDraftBook(),
      session = book.repository(a);
    session.observe(snapshot(), null);
    book.repository(b).write("customer", "Keep B", true);
    book.apply(
      session,
      { ...snapshot().definition, customer: "Reviewed proposal" },
      source,
    );
    const current = book.repository(a);
    expect(current.read("draft", snapshot().definition)?.customer).toBe(
      "Reviewed proposal",
    );
    expect(current.read("draftSource", null)).toEqual(source);
    expect(current.dirty).toBe(true);
    expect(book.repository(b).read("customer", "")).toBe("Keep B");
  });
  it("keeps a changed server cut held when applying a new proposal", () => {
    const book = new ProductDraftBook(),
      session = book.repository(a);
    session.observe(snapshot(), null);
    session.observe(snapshot(a, 2), null);
    book.apply(session, snapshot().definition, source);
    expect(book.repository(a).base?.revision).toBe(1);
    expect(book.repository(a).stale).toBe(true);
  });
  it("clears prior identity data and refuses late updates after the book is disposed", () => {
    const book = new ProductDraftBook(),
      session = book.repository(a);
    session.observe(snapshot(), null);
    session.write("customer", "Private draft", true);
    book.dispose();
    session.write("customer", "Late update", true);
    expect(session.read("customer", "")).toBe("");
    expect(session.base).toBeNull();
    expect(book.hasUnsaved).toBe(false);
    const next = new ProductDraftBook();
    expect(next.repository(a).read("customer", "")).toBe("");
  });
  it("warns before reload for any unsaved product, without browser persistence", () => {
    const book = new ProductDraftBook();
    let prevented = false;
    const event = {
      preventDefault: () => {
        prevented = true;
      },
      returnValue: undefined as unknown,
    };
    warnProductDraftUnload(book, event);
    expect(prevented).toBe(false);
    book.repository(b).write("problem", "Unsaved on another page", true);
    warnProductDraftUnload(book, event);
    expect(prevented).toBe(true);
    book.discard(book.repository(b));
    expect(book.hasUnsaved).toBe(false);
  });
});
