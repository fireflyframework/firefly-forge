/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { FormEvent, ReactElement } from "react";
import type { ForgeRun } from "./api";
const state = vi.hoisted(() => ({
  submit: null as ((event: FormEvent<HTMLFormElement>) => void) | null,
}));
vi.mock("./Wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./Wizard")>();
  return {
    ...actual,
    Wizard: (props: Parameters<typeof actual.Wizard>[0]) => {
      state.submit = (
        props.children as ReactElement<{
          onSubmit: (event: FormEvent<HTMLFormElement>) => void;
        }>
      ).props.onSubmit;
      return <actual.Wizard {...props} />;
    },
  };
});
import { NewProduct } from "./App";
import { ProductDraftBook } from "./ProductDrafts";
const repository = "a".repeat(32);
function setup() {
  const book = new ProductDraftBook(),
    session = book.repository(`run:standalone:${repository}`),
    received: ForgeRun[] = [];
  session.write("brief", "  Verify fictional stock\n", true);
  session.write("commands", '[["pytest", "-q"]]', true);
  session.write("step", 3);
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  renderToStaticMarkup(
    <NewProduct
      drafts={book}
      initialComponent={null}
      initialRepository={repository}
      repositories={[
        {
          id: repository,
          name: "Fictional inventory",
          url: "https://github.com/example/inventory",
          provider: "github",
          default_branch: "main",
        },
      ]}
      profiles={null}
      connected
      connection={{ base: "/api/v1", token: "" }}
      onStandalone={() => {}}
      onRepository={() => {}}
      onConnection={() => {}}
      onCreated={(run) => received.push(run)}
    />,
  );
  let finish!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    ),
  );
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  return { book, session, received, finish };
}
afterEach(() => vi.unstubAllGlobals());

it("clears only the submitted run draft after a successful queue response", async () => {
  const { book, session, received, finish } = setup();
  book.repository("run:standalone:other").write("brief", "Keep B", true);
  expect(session.read("busy", false)).toBe(true);
  finish(new Response(JSON.stringify({ id: "run-1" }), { status: 201 }));
  await vi.waitFor(() => expect(received).toEqual([{ id: "run-1" }]));
  expect(session.active).toBe(false);
  expect(
    book.repository(`run:standalone:${repository}`).read("brief", ""),
  ).toBe("");
  expect(book.repository("run:standalone:other").read("brief", "")).toBe(
    "Keep B",
  );
});

it("retains the raw draft and review step after a rejected queue request", async () => {
  const { session, received, finish } = setup();
  finish(
    new Response(JSON.stringify({ detail: "Execution unavailable" }), {
      status: 503,
    }),
  );
  await vi.waitFor(() => expect(session.read("busy", true)).toBe(false));
  expect(session.read("brief", "")).toBe("  Verify fictional stock\n");
  expect(session.read("commands", "")).toBe('[["pytest", "-q"]]');
  expect(session.read("step", 0)).toBe(3);
  expect(session.read("error", "")).toBe("Execution unavailable");
  expect(received).toEqual([]);
});

it("does not revive an old identity draft or open its run after logout", async () => {
  const { book, session, received, finish } = setup();
  book.clear();
  finish(new Response(JSON.stringify({ id: "old-run" }), { status: 201 }));
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  expect(session.read("busy", false)).toBe(false);
  expect(received).toEqual([]);
  expect(book.hasUnsaved).toBe(false);
  expect(
    book.repository(`run:standalone:${repository}`).read("brief", ""),
  ).toBe("");
});

it("preserves newer run edits when an earlier queue request finishes", async () => {
  const { book, session, received, finish } = setup();
  session.write("brief", "Newer unsubmitted work", true);
  finish(new Response(JSON.stringify({ id: "run-1" }), { status: 201 }));
  await vi.waitFor(() => expect(received).toEqual([{ id: "run-1" }]));
  expect(book.repository(`run:standalone:${repository}`)).toBe(session);
  expect(session.read("brief", "")).toBe("Newer unsubmitted work");
  expect(session.dirty).toBe(true);
  expect(session.read("busy", true)).toBe(false);
});

it("holds a second submit while the retained request is pending", async () => {
  const { received, finish } = setup();
  state.submit!({ preventDefault() {} } as FormEvent<HTMLFormElement>);
  expect(fetch).toHaveBeenCalledTimes(1);
  finish(new Response(JSON.stringify({ id: "run-1" }), { status: 201 }));
  await vi.waitFor(() => expect(received).toEqual([{ id: "run-1" }]));
});
