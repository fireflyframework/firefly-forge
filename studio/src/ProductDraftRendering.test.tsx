/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProductDefinition } from "./ProductDefinition";
import { ProductDraftBook } from "./ProductDrafts";
import type { ProductSnapshot } from "./api";
const a = "a".repeat(32),
  b = "b".repeat(32);
const snapshot = (repository_id: string, revision = 1): ProductSnapshot => ({
  repository_id,
  revision,
  digest: String(revision).repeat(64),
  created_at: "2026-10-16T00:00:00Z",
  origin: { kind: "manual" },
  definition: {
    customer: "Saved customer",
    problem: "Saved problem",
    owners: ["Owner"],
    journey: [],
    user_stories: [],
    acceptance: [],
    security_requirements: [],
    nonfunctional_requirements: [],
  },
});
function render(book: ProductDraftBook, selected: string) {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  return renderToStaticMarkup(
    <ProductDefinition
      connection={{ base: "/api/v1", token: "" }}
      connected
      drafts={book}
      initialRepository={selected}
      repositories={[a, b].map((id) => ({
        id,
        name: id === a ? "Product A" : "Product B",
        url: `https://github.com/example/${id}`,
        provider: "github",
        default_branch: "main",
      }))}
      onConfigure={() => {}}
      onStart={() => {}}
    />,
  );
}
afterEach(() => vi.unstubAllGlobals());
it("renders the retained selected product when the real editor is remounted", () => {
  const book = new ProductDraftBook();
  book.repository(a).observe(snapshot(a), null);
  book.repository(a).write("customer", "Retained A", true);
  book.repository(b).observe(snapshot(b), null);
  book.repository(b).write("customer", "Retained B", true);
  expect(render(book, a)).toContain('value="Retained A"');
  expect(render(book, a)).not.toContain('value="Retained B"');
  expect(render(book, b)).toContain('value="Retained B"');
  expect(render(book, b)).not.toContain('value="Retained A"');
});
it("renders the retained step rather than reopening Context after remount", () => {
  const book = new ProductDraftBook(),
    session = book.repository(a);
  session.observe(snapshot(a), null);
  session.write("step", 1);
  session.write("stories", "Raw story\n", true);
  const html = render(book, a);
  expect(html).toContain("What does the customer need to do?");
  expect(html).toContain("Raw story");
  expect(html).not.toContain('id="product-customer"');
});
it("renders the observed revision hold without mixing the newer server body", () => {
  const book = new ProductDraftBook(),
    session = book.repository(a);
  session.observe(snapshot(a), null);
  session.write("customer", "Retained A", true);
  const newer = snapshot(a, 2);
  newer.definition.customer = "New server content";
  session.observe(newer, null);
  const html = render(book, a);
  expect(html).toContain("Saving is held");
  expect(html).toContain("Editing saved revision 1");
  expect(html).toContain('value="Retained A"');
  expect(html).not.toContain("New server content");
});

it("offers explicit removal when an interrupted reference review outlives its bundle", () => {
  const book = new ProductDraftBook(),
    session = book.repository(a);
  session.observe(snapshot(a), null);
  session.write("step", 4);
  session.write("reviewArea", "references");
  session.write("designPending", true);
  const html = render(book, a);
  expect(html).toContain("Reference review was interrupted");
  expect(html).toContain("Remove reference review");
  expect(html).not.toContain('aria-label="Journey reference viewer"');
  expect(session.read("designPending", false)).toBe(true);
});

it("offers a separate incremental preview on the saved product review step", () => {
  const book = new ProductDraftBook(),
    session = book.repository(a);
  session.observe(snapshot(a), null);
  session.write("step", 4);
  session.write("features", [], true);
  const html = render(book, a);
  expect(html).toContain("Preview contract change");
  expect(html).toContain("Why is this changing?");
  expect(html).toContain("Save draft definition");
});
