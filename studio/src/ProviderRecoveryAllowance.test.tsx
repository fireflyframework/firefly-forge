/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ProviderRecoveryAllowance } from "./ProviderRecoveryAllowance";
import type { IdentitySession } from "./Identity";
import type { ForgeRun } from "./api";
const session = {
  authenticated: true,
  user: { id: "f".repeat(32), authentication_method: "local_bootstrap" },
  global_admin: true,
  memberships: [],
} as unknown as IdentitySession;
const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  brief: "private brief should stay private",
  status: "failed",
  active_task: "implement",
  attempt: 4,
  agent: "codex",
} as ForgeRun;
function html(current = run, actor = session) {
  return renderToStaticMarkup(
    <ProviderRecoveryAllowance
      run={current}
      session={actor}
      connection={{
        base: "https://factory.example/api/v1",
        token: "not-displayed",
      }}
    />,
  );
}
it("introduces an advanced native administrator action without claiming eligibility or execution", () => {
  expect(html()).toContain("Final provider allowance");
  expect(html()).toContain("Checking saved request");
  expect(html()).not.toContain(run.brief);
  expect(html()).not.toContain("not-displayed");
});
it("does not expose a reservation action to nonadmins, mapped identities or unrelated runs", () => {
  expect(html(run, { ...session, global_admin: false })).toBe("");
  expect(
    html(run, {
      ...session,
      user: { ...session.user, authentication_method: "scm_mapping" },
    }),
  ).toBe("");
  for (const change of [
    { agent: "bridge" },
    { attempt: 3 },
    { active_task: "verify" },
  ])
    expect(html({ ...run, ...change })).toBe("");
});
