/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ProductDraftBook } from "./ProductDrafts";
const state = vi.hoisted(() => ({
  book: null as ProductDraftBook | null,
  signedOut: null as (() => void) | null,
  refreshSawUnsaved: null as boolean | null,
  forgotSession: false,
  forgetSawUnsaved: null as boolean | null,
  refreshSawForgotten: null as boolean | null,
}));
vi.mock("./ProductDrafts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./ProductDrafts")>();
  return { ...actual, useProductDraftBook: () => state.book };
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
      error: "Identity refresh unavailable",
      configuration: null,
      forgetSession: () => {
        state.forgetSawUnsaved = state.book!.hasUnsaved;
        state.forgotSession = true;
      },
      refresh: () => {
        state.refreshSawForgotten = state.forgotSession;
        state.refreshSawUnsaved = state.book!.hasUnsaved;
      },
    }),
    IdentityMenu: ({ onSignedOut }: { onSignedOut: () => void }) => {
      state.signedOut = onSignedOut;
      return null;
    },
  };
});
import App from "./App";
import {
  ProductDraftBook as Book,
  warnProductDraftUnload,
} from "./ProductDrafts";
afterEach(() => vi.unstubAllGlobals());
it("successful logout clears drafts before an identity refresh that retains the old user", () => {
  state.book = new Book();
  const prior = state.book.repository("a".repeat(32));
  prior.write("customer", "Private unsaved work", true);
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
  renderToStaticMarkup(<App />);
  expect(state.signedOut).toBeTypeOf("function");
  expect(state.book.hasUnsaved).toBe(true);
  expect(state.forgotSession).toBe(false);
  state.signedOut!();
  expect(state.forgetSawUnsaved).toBe(false);
  expect(state.refreshSawForgotten).toBe(true);
  expect(state.refreshSawUnsaved).toBe(false);
  expect(state.book.hasUnsaved).toBe(false);
  expect(prior.active).toBe(false);
  prior.write("customer", "Late completion", true);
  expect(state.book.repository("a".repeat(32)).read("customer", "")).toBe("");
  const preventDefault = vi.fn();
  warnProductDraftUnload(state.book, {
    preventDefault,
    returnValue: undefined,
  });
  expect(preventDefault).not.toHaveBeenCalled();
});
