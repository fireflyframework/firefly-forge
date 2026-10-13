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
  it("uses the protected server cookie without fabricating a bearer token", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"status":"ready"}'));
    vi.stubGlobal("fetch", fetch);
    await request(
      { base: "/api/v1", token: "", authenticated: true },
      "/health",
    );
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/health",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetch.mock.calls[0][1].headers.Authorization).toBeUndefined();
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
  it("formats bounded validation paths without reflecting inputs or context", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              {
                loc: ["body", "product_definition", "processes", 0, "name"],
                msg: "Field required",
                input: "private-token",
                ctx: { secret: "private-value" },
              },
              {
                loc: ["body", "webhook_secret"],
                msg: "String should have at least 8 characters",
                input: "sensitive-value",
              },
            ],
          }),
          { status: 422 },
        ),
      ),
    );
    await expect(
      request({ base: "/api/v1", token: "operator" }, "/product"),
    ).rejects.toEqual(
      new ApiError(
        "product_definition.processes.0.name: Field required; webhook_secret: String should have at least 8 characters",
        422,
      ),
    );
  });
  it("bounds validation feedback and rejects malformed error entries", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              { loc: ["body", { secret: "hidden" }], msg: "ignored" },
              ...Array.from({ length: 20 }, () => ({
                loc: ["body", "field"],
                msg: "x".repeat(1000),
              })),
            ],
          }),
          { status: 422 },
        ),
      ),
    );
    try {
      await request({ base: "/api/v1", token: "operator" }, "/product");
    } catch (error) {
      expect((error as Error).message.length).toBeLessThanOrEqual(1400);
      expect((error as Error).message).not.toContain("hidden");
      expect((error as Error).message).not.toContain("ignored");
    }
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
