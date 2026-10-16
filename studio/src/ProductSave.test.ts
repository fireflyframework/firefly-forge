/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CustomerProductDefinition, ProductSnapshot } from "./api";
import { ProductDraftBook } from "./ProductDrafts";
import { reconcileProductSave, saveProductDefinition } from "./ProductSave";

const repo = "a".repeat(32);
const definition: CustomerProductDefinition = {
  customer: "Fictional team",
  problem: "Keep stock",
  journey: ["Read stock"],
  user_stories: ["An operator reads stock"],
  acceptance: ["Stock is visible"],
  owners: ["Owner"],
  security_requirements: [],
  nonfunctional_requirements: [],
};
const saved = (revision = 1, input = definition): ProductSnapshot => ({
  repository_id: repo,
  revision,
  digest: String(revision).repeat(64),
  created_at: "2026-10-16T00:00:00Z",
  definition: structuredClone(input),
  origin: { kind: "manual" },
});
const connection = { base: "/api/v1", token: "fictional-token" };
function setup() {
  const book = new ProductDraftBook(),
    session = book.repository(repo);
  session.observe(saved(), null);
  session.write("customer", "Changed team", true);
  return { book, session, input: { ...definition, customer: "Changed team" } };
}
afterEach(() => vi.unstubAllGlobals());

describe("conditional product-save recovery", () => {
  it("retains one exact input and holds navigation, discard and proposal after commit-before-response-loss", async () => {
    const { book, session, input } = setup();
    const fetch = vi
      .fn()
      .mockRejectedValue(new TypeError("Fictional lost response"));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    expect(session.saveAttempt?.definition).toEqual(input);
    expect(session.saveAttempt?.expected_revision).toBe(1);
    expect(session.saveAttempt?.expected_digest).toBe(saved().digest);
    expect(session.saveHeld).toBe(true);
    expect(book.repository(repo)).toBe(session);
    expect(() => book.discard(session)).toThrow(/save/i);
    expect(() =>
      book.apply(session, input, {
        kind: "ai_generated",
        provider: "bridge",
        model: "fictional",
        generated_at: saved().created_at,
        conversation_id: "b".repeat(32),
        turn_id: "c".repeat(32),
        proposal_digest: "d".repeat(64),
      }),
    ).toThrow(/save/i);
    await expect(
      saveProductDefinition(session, connection, {
        ...input,
        customer: "Another edit",
      }),
    ).rejects.toThrow(/save/i);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe(
      `/api/v1/repositories/${repo}/product/conditional`,
    );
  });
  it.each([
    null,
    saved(),
    saved(2, { ...definition, customer: "Another writer" }),
  ])("never resubmits during exact GET observation %j", async (observed) => {
    const { session, input } = setup();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Lost response"))
      .mockResolvedValue(new Response(JSON.stringify(observed)));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    await reconcileProductSave(session, connection);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][1].method).toBeUndefined();
    expect(session.read("customer", "")).toBe("Changed team");
    if (!observed || observed.revision === 1)
      expect(session.saveHeld).toBe(true);
    else expect(session.stale).toBe(true);
  });
  it("resolves only a later matching conditional cut without claiming original request identity", async () => {
    const { session, input } = setup();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Lost response"))
      .mockResolvedValueOnce(new Response(JSON.stringify(saved(2, input))));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    expect(await reconcileProductSave(session, connection)).toEqual(
      saved(2, input),
    );
    expect(session.saveHeld).toBe(false);
    expect(session.base?.revision).toBe(2);
    expect(session.dirty).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("keeps later typing while observing the committed submitted definition", async () => {
    const { session, input } = setup();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Lost response"))
      .mockResolvedValueOnce(new Response(JSON.stringify(saved(2, input))));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    session.write("customer", "Newer typing", true);
    await reconcileProductSave(session, connection);
    expect(session.read("customer", "")).toBe("Newer typing");
    expect(session.dirty).toBe(true);
    expect(session.stale).toBe(true);
  });
  it("allows only an explicit retry of the frozen conditional body, with no automatic POST", async () => {
    const { session, input } = setup();
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Lost response"))
      .mockResolvedValueOnce(new Response(JSON.stringify(saved(2, input))));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    await saveProductDefinition(session, connection, input, true);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
    expect(session.saveHeld).toBe(false);
  });
  it("keeps foreign, malformed and failed observations held", async () => {
    const { session, input } = setup();
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("Lost response"));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    for (const value of [
      { ...saved(2, input), repository_id: "b".repeat(32) },
      { definition: input },
      "bad",
    ]) {
      fetch.mockResolvedValueOnce(new Response(JSON.stringify(value)));
      await expect(reconcileProductSave(session, connection)).rejects.toThrow();
      expect(session.saveHeld).toBe(true);
    }
    fetch.mockRejectedValueOnce(new TypeError("GET failed"));
    await expect(reconcileProductSave(session, connection)).rejects.toThrow();
    expect(session.saveHeld).toBe(true);
  });
  it("does not send through a closed identity session or mutate its held payload", async () => {
    const { book, session, input } = setup();
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("Lost response"));
    vi.stubGlobal("fetch", fetch);
    await expect(
      saveProductDefinition(session, connection, input),
    ).rejects.toThrow();
    session.saveAttempt!.definition.customer = "Mutated getter";
    expect(session.saveAttempt?.definition.customer).toBe("Changed team");
    book.dispose();
    await expect(
      saveProductDefinition(session, connection, input, true),
    ).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

it("holds malformed successful delivery and never falls back for an older server", async () => {
  const first = setup();
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response("<html>Lost JSON</html>"));
  vi.stubGlobal("fetch", fetch);
  await expect(
    saveProductDefinition(first.session, connection, first.input),
  ).rejects.toThrow();
  expect(first.session.saveHeld).toBe(true);
  await expect(
    saveProductDefinition(first.session, connection, first.input),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
  const old = setup();
  fetch.mockResolvedValueOnce(
    new Response('{"detail":"Not found"}', { status: 404 }),
  );
  await expect(
    saveProductDefinition(old.session, connection, old.input),
  ).rejects.toThrow();
  expect(old.session.saveHeld).toBe(false);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(
    fetch.mock.calls.every(([url]) =>
      String(url).endsWith("/product/conditional"),
    ),
  ).toBe(true);
});

it("holds synchronous duplicate send and retry while the original request is still pending", async () => {
  const { session, input } = setup();
  let finish!: (value: Response) => void;
  const fetch = vi.fn().mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const first = saveProductDefinition(session, connection, input);
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow();
  await expect(
    saveProductDefinition(session, connection, input, true),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
  finish(new Response(JSON.stringify(saved(2, input))));
  await first;
  expect(session.saveHeld).toBe(false);
});
it("retains a lost result across repository switches without allowing another PUT", async () => {
  const { book, session, input } = setup();
  const fetch = vi.fn().mockRejectedValue(new TypeError("Lost result"));
  vi.stubGlobal("fetch", fetch);
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow();
  book.repository("b".repeat(32)).write("customer", "Other product", true);
  await expect(
    saveProductDefinition(book.repository(repo), connection, input),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(book.hasUnsaved).toBe(true);
});
it("refuses a prior identity's completion after its session was disposed", async () => {
  const { book, session, input } = setup();
  let finish!: (value: Response) => void;
  const fetch = vi.fn().mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const pending = saveProductDefinition(session, connection, input);
  book.dispose();
  finish(new Response(JSON.stringify(saved(2, input))));
  await expect(pending).rejects.toThrow(/ended/);
  expect(session.base).toBeNull();
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("releases a parsed pre-write validation rejection while preserving editable fields", async () => {
  const { session, input } = setup();
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(
        '{"detail":[{"loc":["body","definition","customer"],"msg":"Field required"}]}',
        { status: 422 },
      ),
    );
  vi.stubGlobal("fetch", fetch);
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow();
  expect(session.saveHeld).toBe(false);
  expect(session.read("customer", "")).toBe("Changed team");
  session.write("customer", "Corrected input", true);
  fetch.mockResolvedValueOnce(
    new Response(
      JSON.stringify(saved(2, { ...input, customer: "Corrected input" })),
    ),
  );
  await saveProductDefinition(session, connection, {
    ...input,
    customer: "Corrected input",
  });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(session.base?.definition.customer).toBe("Corrected input");
});
it("keeps an unreadable error response held even with a validation status", async () => {
  const { session, input } = setup();
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response("<html>Unknown effect</html>", { status: 422 }),
      ),
  );
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow();
  expect(session.saveHeld).toBe(true);
});
it("a rejected retry cannot settle the original uncertain write", async () => {
  const { session, input } = setup();
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("Original response lost"))
    .mockResolvedValueOnce(
      new Response('{"detail":"Permission denied"}', { status: 403 }),
    );
  vi.stubGlobal("fetch", fetch);
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow();
  await expect(
    saveProductDefinition(session, connection, input, true),
  ).rejects.toThrow();
  expect(session.saveHeld).toBe(true);
  expect(session.saveAttempt?.definition).toEqual(input);
  expect(fetch.mock.calls[0][1].body).toBe(fetch.mock.calls[1][1].body);
});
it("permits correcting an explicitly refused business validation but holds unqualified conflicts", async () => {
  const { session, input } = setup();
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(
        '{"detail":"Product brand changed after design review","code":"product_save_not_applied"}',
        { status: 409 },
      ),
    );
  vi.stubGlobal("fetch", fetch);
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow(/brand changed/);
  expect(session.saveHeld).toBe(false);
  expect(session.read("customer", "")).toBe("Changed team");
  fetch.mockResolvedValueOnce(
    new Response('{"detail":"Unqualified conflict"}', { status: 409 }),
  );
  await expect(
    saveProductDefinition(session, connection, input),
  ).rejects.toThrow();
  expect(session.saveHeld).toBe(true);
});
