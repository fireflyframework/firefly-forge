/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentCheckpointRecovery } from "./AgentCheckpointRecovery";
import type { IdentitySession } from "./Identity";
const session = {
  authenticated: true,
  user: {
    id: "1".repeat(32),
    display_name: null,
    email: null,
    authentication_method: "local_bootstrap",
  },
  global_admin: true,
  memberships: [],
  roles: { owner: [], maintainer: [], developer: [], reviewer: [], viewer: [] },
} as IdentitySession;
afterEach(() => vi.unstubAllGlobals());
it("shows readable held-result guidance with labeled local discovery and no raw digest fields", () => {
  vi.stubGlobal("sessionStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <AgentCheckpointRecovery
      run={{
        id: "4".repeat(32),
        repository_id: "3".repeat(32),
        project_id: "2".repeat(32),
        brief: "private brief",
        status: "needs_reconciliation",
        agent: "bridge",
      }}
      session={session}
      connection={{
        base: "https://factory.example/api/v1",
        token: "fictional",
      }}
    />,
  );
  expect(html).toContain("Original model result");
  expect(html).toContain("Refresh saved result");
  expect(html).toContain("retained work");
  expect(html).not.toContain("private brief");
  expect(html).not.toContain("textarea");
});
it("withholds recovery actions when current execute permission is absent", () => {
  vi.stubGlobal("sessionStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <AgentCheckpointRecovery
      run={{
        id: "4".repeat(32),
        repository_id: "3".repeat(32),
        project_id: "2".repeat(32),
        brief: "",
        status: "needs_reconciliation",
        agent: "bridge",
      }}
      session={{ ...session, global_admin: false }}
      connection={{
        base: "https://factory.example/api/v1",
        token: "fictional",
      }}
    />,
  );
  expect(html).toContain("Current project execution access");
  expect(html).not.toContain("Continue from original result");
});
