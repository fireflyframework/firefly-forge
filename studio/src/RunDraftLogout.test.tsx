/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ProductDraftBook } from "./ProductDrafts";
const state = vi.hoisted(() => ({
  books: [] as ProductDraftBook[],
  calls: 0,
  signedOut: null as (() => void) | null,
  refreshSawUnsaved: true,
}));
vi.mock("./ProductDrafts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./ProductDrafts")>();
  return {
    ...actual,
    useProductDraftBook: () => state.books[state.calls++],
  };
});
vi.mock("./Identity", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./Identity")>();
  return {
    ...actual,
    useIdentity: () => ({
      session: {
        authenticated: true,
        user: {
          id: "f".repeat(32),
          display_name: "Fictional operator",
          email: null,
          authentication_method: "local_bootstrap",
        },
        global_admin: true,
        memberships: [],
        roles: {},
      },
      loading: false,
      error: "Identity unavailable",
      configuration: null,
      forgetSession: () => {},
      refresh: () => {
        state.refreshSawUnsaved = state.books[1].hasUnsaved;
      },
    }),
    IdentityMenu: ({ onSignedOut }: { onSignedOut: () => void }) => {
      state.signedOut = onSignedOut;
      return null;
    },
  };
});
import App from "./App";
import { ProductDraftBook as Book } from "./ProductDrafts";
afterEach(() => vi.unstubAllGlobals());
it("confirmed logout clears run drafts and context before a failing identity refresh", () => {
  state.books = [new Book(), new Book()];
  state.calls = 0;
  const prior = state.books[1].repository("run:standalone:source-a");
  prior.write("brief", "Private run instructions", true);
  const context = state.books[1].repository("run:context");
  context.write("repository", "source-a");
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
  renderToStaticMarkup(<App />);
  state.signedOut!();
  expect(state.refreshSawUnsaved).toBe(false);
  expect(prior.active).toBe(false);
  expect(context.active).toBe(false);
  prior.write("brief", "Late old-session completion", true);
  expect(
    state.books[1].repository("run:standalone:source-a").read("brief", ""),
  ).toBe("");
  expect(state.books[1].repository("run:context").read("repository", "")).toBe(
    "",
  );
});
