/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { InvalidReadRecovery } from "./InvalidReadRecovery";
import type { IdentitySession } from "./Identity";
const session = {
  authenticated: true,
  user: {
    id: "1".repeat(32),
    authentication_method: "local_bootstrap",
    display_name: null,
    email: null,
  },
  global_admin: true,
  memberships: [],
  roles: { owner: [], maintainer: [], developer: [], reviewer: [], viewer: [] },
} as IdentitySession;
const run = {
  id: "4".repeat(32),
  repository_id: "3".repeat(32),
  project_id: "2".repeat(32),
  brief: "private brief",
  status: "needs_reconciliation",
  invalid_read_continuity: { request_id: "b".repeat(32) },
};
const connection = {
  base: "https://factory.example/api/v1",
  token: "private token",
};
afterEach(() => vi.unstubAllGlobals());
it("uses existing run discovery with no pasted ID, digest, private brief or admission success claim", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <InvalidReadRecovery run={run} session={session} connection={connection} />,
  );
  expect(html).toContain("Saved continuation");
  expect(html).toContain("Check current admission");
  expect(html).toContain("worker");
  for (const privateValue of [
    "private brief",
    "private token",
    "textarea",
    "Requeue existing task",
  ])
    expect(html).not.toContain(privateValue);
});
it("withholds actions without current execution permission or after cancellation", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  for (const props of [
    {
      run,
      session: {
        ...session,
        global_admin: false,
        memberships: [
          {
            project_id: run.project_id,
            role: "developer" as const,
            revision: 1,
            permissions: ["execute" as const],
          },
        ],
      },
    },
    {
      run: {
        ...run,
        cancel_request: { id: "5".repeat(32), status: "requested" as const },
      },
      session,
    },
  ]) {
    const html = renderToStaticMarkup(
      <InvalidReadRecovery {...props} connection={connection} />,
    );
    expect(html).toContain("Current execution access");
    expect(html).not.toContain("Check current admission");
  }
});
it("does not offer recovery for unknown request identity", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  expect(
    renderToStaticMarkup(
      <InvalidReadRecovery
        run={{ ...run, invalid_read_continuity: { request_id: "unknown" } }}
        session={session}
        connection={connection}
      />,
    ),
  ).toBe("");
});
