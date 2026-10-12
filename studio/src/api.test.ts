/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, normalizeBase, parseCommands, post, request } from "./api";
afterEach(() => vi.unstubAllGlobals());
describe("authenticated API client", () => {
  it("sends the entered token with the exact run payload", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response('{"id":"run-1"}', { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    expect(
      await post({ base: "/api/v1", token: "entered-token" }, "/runs", {
        publish: false,
      }),
    ).toEqual({ id: "run-1" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/runs",
      expect.objectContaining({
        method: "POST",
        body: '{"publish":false}',
        headers: expect.objectContaining({
          Authorization: "Bearer entered-token",
        }),
      }),
    );
  });
  it("preserves actionable authentication failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response('{"detail":"Token expired"}', { status: 401 }),
        ),
    );
    await expect(
      request({ base: "/api/v1", token: "old" }, "/runs"),
    ).rejects.toEqual(new ApiError("Token expired", 401));
  });
  it("rejects HTML returned by a mistaken API address", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("<html>Studio</html>")),
    );
    await expect(
      request({ base: "/api/v1", token: "" }, "/runs"),
    ).rejects.toThrow("non-JSON");
  });
  it("refuses credential and non-HTTP addresses", () => {
    expect(() => normalizeBase("javascript:alert(1)")).toThrow();
    expect(() => normalizeBase("https://secret@example.org/api/v1")).toThrow();
  });
  it("creates argv commands without shell interpolation", () => {
    expect(parseCommands('[["uv","run","pytest"],["npm","test"]]')).toEqual([
      { argv: ["uv", "run", "pytest"], timeout: 120 },
      { argv: ["npm", "test"], timeout: 120 },
    ]);
    expect(() => parseCommands('["npm test"]')).toThrow();
  });
});
