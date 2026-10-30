// @vitest-environment happy-dom
/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { PersonalTokens } from "./PersonalTokens";
import type { IdentitySession } from "./Identity";

const project = "a".repeat(32);
const session = {
  user: { id: "b".repeat(32), authentication_method: "oidc_session" },
  memberships: [{ project_id: project, permissions: ["read"] }],
} as IdentitySession;
const credential = {
  id: "c".repeat(32),
  name: "Release dashboard",
  prefix: "ffp_example",
  project_ids: [project],
  permissions: ["read"],
  status: "active",
  expires_at: "2027-12-01T12:00:00Z",
  created_at: "2026-10-01T12:00:00Z",
  revoked_at: null,
  predecessor_id: null,
};
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
function show(method = "oidc_session", items: unknown[] = []) {
  vi.stubGlobal("localStorage", { setItem: vi.fn() });
  vi.stubGlobal("sessionStorage", { setItem: vi.fn() });
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === "POST")
      return Response.json({
        credential,
        secret_state: "returned_once",
        token: "fictional-secret-only",
      });
    return Response.json(
      url.endsWith("/projects")
        ? [{ id: project, definition: { name: "Orbit CRM" } }]
        : { items, next_cursor: null },
    );
  });
  vi.stubGlobal("fetch", fetcher);
  const view = render(
    <PersonalTokens
      connection={{ base: "/api/v1", token: "must-not-be-forwarded" }}
      session={{
        ...session,
        user: { ...session.user, authentication_method: method },
      }}
    />,
  );
  return { fetcher, ...view };
}
it("does not fetch credentials or offer issuance outside an OIDC browser session", () => {
  const { fetcher } = show("local_bootstrap");
  expect(screen.getByText(/Sign in with your organization/)).toBeTruthy();
  expect(fetcher).not.toHaveBeenCalled();
});
it("creates an explicit read-only project credential and clears its one-time secret", async () => {
  const { fetcher } = show();
  fireEvent.click(await screen.findByRole("button", { name: "New token" }));
  fireEvent.change(screen.getByLabelText("Token name"), {
    target: { value: "Release dashboard" },
  });
  fireEvent.change(screen.getByLabelText("Expires on"), {
    target: { value: "2027-12-01T12:00" },
  });
  fireEvent.click(await screen.findByLabelText("Orbit CRM"));
  fireEvent.click(screen.getByRole("button", { name: "Create token" }));
  expect(await screen.findByDisplayValue("fictional-secret-only")).toBeTruthy();
  const call = fetcher.mock.calls.find(
    ([, options]) => options?.method === "POST",
  )!;
  expect(call[0]).toBe("/api/v1/identity/tokens");
  const body = JSON.parse(call[1]!.body as string);
  expect(body).toMatchObject({ project_ids: [project], permissions: ["read"] });
  expect(body.request_id).toMatch(/^[a-f0-9]{32}$/);
  expect(
    (call[1]!.headers as Record<string, string>).Authorization,
  ).toBeUndefined();
  expect(localStorage.setItem).not.toHaveBeenCalled();
  expect(sessionStorage.setItem).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "I saved this token" }));
  expect(screen.queryByDisplayValue("fictional-secret-only")).toBeNull();
});
it("requires confirmation before revoking and never replays a failed mutation", async () => {
  const { fetcher } = show("oidc_session", [credential]);
  fireEvent.click(
    await screen.findByRole("button", { name: "Revoke Release dashboard" }),
  );
  expect(
    fetcher.mock.calls.filter(([, options]) => options?.method === "POST"),
  ).toHaveLength(0);
  fetcher.mockImplementationOnce(async () => {
    throw new Error(
      "Connection lost. Inspect the token list before trying again.",
    );
  });
  fireEvent.click(screen.getByRole("button", { name: "Revoke token" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  await waitFor(() =>
    expect(
      fetcher.mock.calls.filter(([, options]) => options?.method === "POST"),
    ).toHaveLength(1),
  );
  expect(screen.queryByRole("button", { name: "Revoke token" })).toBeNull();
});

it("rotates through the exact lifecycle endpoint without changing project grants", async () => {
  const { fetcher } = show("oidc_session", [credential]);
  fireEvent.click(
    await screen.findByRole("button", { name: "Rotate Release dashboard" }),
  );
  expect(
    fetcher.mock.calls.filter(([, options]) => options?.method === "POST"),
  ).toHaveLength(0);
  fireEvent.change(screen.getByLabelText("Expires on"), {
    target: { value: "2027-12-02T12:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Rotate token" }));
  await screen.findByDisplayValue("fictional-secret-only");
  const call = fetcher.mock.calls.find(
    ([, options]) => options?.method === "POST",
  )!;
  expect(call[0]).toBe(`/api/v1/identity/tokens/${credential.id}/rotate`);
  expect(Object.keys(JSON.parse(call[1]!.body as string)).sort()).toEqual([
    "expires_at",
    "request_id",
  ]);
});

it("retries project discovery after a failed initial read without issuing a token", async () => {
  const { fetcher } = show();
  fetcher.mockImplementation(async (url) =>
    url.endsWith("/projects")
      ? Promise.reject(new Error("Project catalog unavailable"))
      : Response.json({ items: [], next_cursor: null }),
  );
  // The initial microtask already started; remount into a new selected server.
  cleanup();
  render(
    <PersonalTokens
      connection={{ base: "/api/v1", token: "" }}
      session={session}
    />,
  );
  await screen.findByRole("alert");
  fetcher.mockImplementation(async (url) =>
    Response.json(
      url.endsWith("/projects")
        ? [{ id: project, definition: { name: "Orbit CRM" } }]
        : { items: [], next_cursor: null },
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Refresh list" }));
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "New token" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  expect(
    fetcher.mock.calls.filter(([, options]) => options?.method === "POST"),
  ).toHaveLength(0);
});

it("clears a displayed secret when identity changes", async () => {
  const { rerender } = show();
  fireEvent.click(await screen.findByRole("button", { name: "New token" }));
  fireEvent.change(screen.getByLabelText("Token name"), {
    target: { value: "Release dashboard" },
  });
  fireEvent.change(screen.getByLabelText("Expires on"), {
    target: { value: "2027-12-01T12:00" },
  });
  fireEvent.click(await screen.findByLabelText("Orbit CRM"));
  fireEvent.click(screen.getByRole("button", { name: "Create token" }));
  await screen.findByDisplayValue("fictional-secret-only");
  rerender(
    <PersonalTokens
      connection={{ base: "/api/v1", token: "" }}
      session={{ ...session, user: { ...session.user, id: "d".repeat(32) } }}
    />,
  );
  expect(screen.queryByDisplayValue("fictional-secret-only")).toBeNull();
});

it("does not offer projects outside the current signed-in membership", async () => {
  const { fetcher } = show();
  cleanup();
  fetcher.mockImplementation(async (url) =>
    Response.json(
      url.endsWith("/projects")
        ? [
            { id: project, definition: { name: "Orbit CRM" } },
            { id: "e".repeat(32), definition: { name: "Foreign project" } },
          ]
        : { items: [], next_cursor: null },
    ),
  );
  render(
    <PersonalTokens
      connection={{ base: "/api/v1", token: "" }}
      session={session}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "New token" }));
  expect(await screen.findByLabelText("Orbit CRM")).toBeTruthy();
  expect(screen.queryByLabelText("Foreign project")).toBeNull();
});

it("does not recover or invent a one-time secret when the server returns retained metadata", async () => {
  const { fetcher } = show();
  fireEvent.click(await screen.findByRole("button", { name: "New token" }));
  fireEvent.change(screen.getByLabelText("Token name"), {
    target: { value: "Release dashboard" },
  });
  fireEvent.change(screen.getByLabelText("Expires on"), {
    target: { value: "2027-12-01T12:00" },
  });
  fireEvent.click(await screen.findByLabelText("Orbit CRM"));
  fetcher.mockImplementationOnce(async () =>
    Response.json({ credential, secret_state: "not_retained" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Create token" }));
  expect(
    await screen.findByText(/The token exists, but its secret is not retained/),
  ).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Save your token" })).toBeNull();
});

it("cannot disclose a late mutation response into a different user's screen", async () => {
  const { fetcher, rerender } = show();
  fireEvent.click(await screen.findByRole("button", { name: "New token" }));
  fireEvent.change(screen.getByLabelText("Token name"), {
    target: { value: "Release dashboard" },
  });
  fireEvent.change(screen.getByLabelText("Expires on"), {
    target: { value: "2027-12-01T12:00" },
  });
  fireEvent.click(await screen.findByLabelText("Orbit CRM"));
  let finish!: (response: Response) => void;
  fetcher.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Create token" }));
  rerender(
    <PersonalTokens
      connection={{ base: "/api/v1", token: "" }}
      session={{ ...session, user: { ...session.user, id: "e".repeat(32) } }}
    />,
  );
  finish(
    Response.json({
      credential,
      secret_state: "returned_once",
      token: "late-fictional-secret",
    }),
  );
  await waitFor(() =>
    expect(
      fetcher.mock.calls.filter(([url]) => url.endsWith("/projects")).length,
    ).toBeGreaterThanOrEqual(2),
  );
  expect(screen.queryByDisplayValue("late-fictional-secret")).toBeNull();
});
