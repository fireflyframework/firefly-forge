/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProductOrigin } from "./ProductProposal";
import { ProductDefinition } from "./ProductDefinition";
import { ProductDraftBook } from "./ProductDrafts";
import type {
  AIProposalProvenance,
  ProductSavedOrigin,
  ProductSnapshot,
} from "./api";

const source: AIProposalProvenance = {
  kind: "ai_generated",
  provider: "bridge",
  model: "bridge/default",
  generated_at: "2026-10-17T00:00:00Z",
  conversation_id: "c".repeat(32),
  turn_id: "d".repeat(32),
  proposal_digest: "e".repeat(64),
};
const savedOrigin: ProductSavedOrigin = {
  ...source,
  kind: "ai_assisted",
  edited: true,
};
const snapshot: ProductSnapshot = {
  repository_id: "a".repeat(32),
  revision: 1,
  digest: "b".repeat(64),
  created_at: source.generated_at,
  origin: savedOrigin,
  definition: {
    customer: "Human-refined supervisors",
    problem: "Reserve inventory safely",
    owners: ["Inventory team"],
    journey: ["Reserve stock"],
    user_stories: ["A supervisor reserves available stock"],
    acceptance: ["Reject excessive reservations"],
    security_requirements: [],
    nonfunctional_requirements: [],
    ai_origin: {
      conversation_id: source.conversation_id,
      turn_id: source.turn_id,
      proposal_digest: source.proposal_digest,
    },
  },
};
function badge(
  origin: AIProposalProvenance | ProductSavedOrigin | null,
  saved = true,
  dirty = false,
  revision?: number,
) {
  return renderToStaticMarkup(
    <ProductOrigin
      source={origin}
      saved={saved}
      dirty={dirty}
      revision={revision}
      onSource={() => {}}
    />,
  );
}
function editor(book: ProductDraftBook) {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  return renderToStaticMarkup(
    <ProductDefinition
      connection={{ base: "/api/v1", token: "" }}
      connected
      drafts={book}
      initialRepository={snapshot.repository_id}
      repositories={[
        {
          id: snapshot.repository_id,
          name: "Inventory",
          provider: "git",
          url: "/fixture",
          default_branch: "main",
        },
      ]}
      onConfigure={() => {}}
      onStart={() => {}}
    />,
  );
}
afterEach(() => vi.unstubAllGlobals());

describe("saved product attribution", () => {
  it("shows server-recorded human editing while preserving the original AI source", () => {
    const html = badge(savedOrigin);
    expect(html).toContain("AI-assisted · human-edited");
    expect(html).toContain("bridge · bridge/default");
    expect(html).toContain(new Date(source.generated_at).toLocaleString());
    expect(html).toContain('aria-label="View AI source"');
    expect(html).not.toContain("edited by you");
  });
  it.each([false, undefined])(
    "keeps edited=%s neutral without claiming an untouched proposal",
    (edited) => {
      const origin: ProductSavedOrigin = { ...source, kind: "ai_assisted" };
      const html = badge(edited === undefined ? origin : { ...origin, edited });
      expect(html).toContain("AI-assisted definition");
      expect(html).not.toContain("human-edited");
      expect(html).not.toMatch(/untouched|unmodified|unedited/i);
      expect(html).toContain('aria-label="View AI source"');
    },
  );
  it.each([
    { origin: source, saved: false, dirty: true },
    { origin: savedOrigin, saved: true, dirty: true },
  ])(
    "keeps unsaved working state separate from saved editing evidence %j",
    ({ origin, saved, dirty }) => {
      const html = badge(origin, saved, dirty);
      expect(html).toContain("AI-assisted draft · review needed");
      expect(html).not.toContain("human-edited");
      expect(html).toContain('aria-label="View AI source"');
    },
  );
  it("does not infer a saved editing flag from an applied proposal", () => {
    expect(badge(source)).toContain("AI-assisted definition");
    expect(badge(source)).not.toContain("human-edited");
  });
  it("keeps manual saved and unsaved definitions separate from AI attribution", () => {
    const manual = badge({ kind: "manual" }, true, false, 3);
    expect(manual).toContain("Saved revision 3");
    expect(manual).not.toContain("AI-assisted");
    expect(manual).not.toContain("View AI source");
    const draft = badge(null, false, true);
    expect(draft).toContain("Manual draft · review needed");
    expect(draft).toContain("Unsaved edits");
    expect(draft).not.toContain("View AI source");
  });
  it("uses authoritative saved attribution immediately despite a retained proposal source", () => {
    const book = new ProductDraftBook(),
      initial = book.repository(snapshot.repository_id);
    initial.observe(null, null);
    book.apply(initial, snapshot.definition, source);
    const session = book.repository(snapshot.repository_id);
    session.acceptSaved(snapshot, session.editVersion);
    expect(editor(book)).toContain("AI-assisted · human-edited");
    expect(session.read("draftSource", null)).toEqual(source);
    session.write("customer", "Another unsaved refinement", true);
    const working = editor(book);
    expect(working).toContain("AI-assisted draft · review needed");
    expect(working).not.toContain("human-edited");
  });
  it("retains saved human-edit attribution when only the persisted revision is loaded", () => {
    const book = new ProductDraftBook();
    book.repository(snapshot.repository_id).observe(snapshot, null);
    const html = editor(book);
    expect(html).toContain("AI-assisted · human-edited");
    expect(html).toContain('aria-label="View AI source"');
  });
});
