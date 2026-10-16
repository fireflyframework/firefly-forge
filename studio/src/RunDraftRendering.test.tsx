/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { NewProduct } from "./App";
import { ProductDraftBook } from "./ProductDrafts";
import type { ComponentRunContext } from "./api";

const a = "a".repeat(32),
  b = "b".repeat(32);
function render(
  book: ProductDraftBook,
  repository = a,
  component: ComponentRunContext | null = null,
) {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  return renderToStaticMarkup(
    <NewProduct
      drafts={book}
      initialComponent={component}
      initialRepository={repository}
      repositories={[a, b].map((id) => ({
        id,
        name: id === a ? "Product A" : "Product B",
        url: `https://github.com/example/${id}`,
        provider: "github",
        default_branch: "main",
      }))}
      profiles={null}
      connected
      connection={{ base: "/api/v1", token: "" }}
      onStandalone={() => {}}
      onRepository={() => {}}
      onConnection={() => {}}
      onCreated={() => {}}
    />,
  );
}
afterEach(() => vi.unstubAllGlobals());

it("restores the raw run brief and selected source when the form remounts", () => {
  const book = new ProductDraftBook(),
    draft = book.repository(`run:standalone:${a}`);
  draft.write("brief", "  Keep **this fictional request**\n", true);
  draft.write("repositoryId", b, true);
  draft.write("operation", "maintain", true);
  const first = render(book);
  const returned = render(book);
  for (const html of [first, returned]) {
    expect(html).toContain("  Keep **this fictional request**\n</textarea>");
    expect(html).toContain(`<option value="${b}" selected="">`);
    expect(html).toContain('<option value="maintain" selected="">');
  }
});

it("restores the Checks step and raw commands rather than starting a blank run", () => {
  const book = new ProductDraftBook(),
    draft = book.repository(`run:standalone:${a}`);
  draft.write("step", 2);
  draft.write("commands", '  [["pytest", "-q"]]\n', true);
  draft.write("reviewAgent", "claude", true);
  const html = render(book);
  expect(html).toContain("Set the checks");
  expect(html).toContain(
    "  [[&quot;pytest&quot;, &quot;-q&quot;]]\n</textarea>",
  );
  expect(html).toContain('<option value="claude" selected="">');
  expect(html).not.toContain('class="run-brief"');
});

it("keeps standalone source drafts separate and closes cleared drafts against late writes", () => {
  const book = new ProductDraftBook(),
    previous = book.repository(`run:standalone:${a}`);
  previous.write("brief", "Private A draft", true);
  book
    .repository(`run:standalone:${b}`)
    .write("brief", "Private B draft", true);
  expect(render(book, a)).toContain("Private A draft");
  expect(render(book, a)).not.toContain("Private B draft");
  expect(render(book, b)).toContain("Private B draft");
  book.clear();
  previous.write("brief", "Late old-identity completion", true);
  const html = render(book, a);
  expect(html).not.toContain("Private A draft");
  expect(html).not.toContain("Late old-identity completion");
  expect(html).toContain('class="run-brief"');
});

it("retains every review choice and a pending submission across form remount", () => {
  const book = new ProductDraftBook(),
    draft = book.repository(`run:standalone:${a}`);
  draft.write("step", 3);
  draft.write("brief", "Review the fictional stock flow", true);
  draft.write("commands", '[["pytest"]]', true);
  draft.write("backend", "larafly", true);
  draft.write("frontend", "vue", true);
  draft.write("agent", "claude", true);
  draft.write("operation", "maintain", true);
  draft.write("reviewAgent", "codex", true);
  draft.write("overridePolicies", true, true);
  draft.write("selectedSkills", ["test-policy"], true);
  draft.write("busy", true);
  const html = render(book);
  expect(html).toContain("Review your run");
  expect(html).toContain("Review the fictional stock flow");
  expect(html).toContain("larafly / vue");
  expect(html).toContain("Requested from codex in a separate workspace");
  expect(html).toContain("test-policy");
  expect(html).toContain("Start maintenance</button>");
  expect(html).toContain('class="primary" disabled=""');
});

it("isolates a component run from standalone drafts and a newer component contract", () => {
  const book = new ProductDraftBook();
  const component = {
    id: "web",
    name: "Stock UI",
    repository_id: a,
    path: "web",
    kind: "web" as const,
    backend: "none",
    frontend: "react",
    verification: [{ argv: ["npm", "test"], timeout: 120 }],
    depends_on: [],
    responsibility: "Display fictional stock",
  };
  const context: ComponentRunContext = {
    component,
    project: {
      id: "project-a",
      revision: 2,
      digest: "1".repeat(64),
      created_at: "2026-10-16T00:00:00Z",
      updated_at: "2026-10-16T00:00:00Z",
      definition: {
        name: "Stock product",
        description: "Fictional inventory",
        topology: "monorepo",
        owners: ["Owner"],
        components: [component],
      },
    },
  };
  book
    .repository("run:component:project-a/2/web")
    .write("brief", "Retained component instructions", true);
  expect(render(book, a, context)).toContain("Retained component instructions");
  expect(render(book, a)).not.toContain("Retained component instructions");
  expect(
    render(book, a, {
      ...context,
      project: { ...context.project, revision: 3 },
    }),
  ).not.toContain("Retained component instructions");
  expect(render(book, a, context)).toContain("Retained component instructions");
});
