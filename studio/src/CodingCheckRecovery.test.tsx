/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CodingCheckRecovery } from "./CodingCheckRecovery";
import type { ForgeRun } from "./api";
import type { IdentitySession } from "./Identity";
const run = {
  id: "c".repeat(32),
  repository_id: "b".repeat(32),
  attempt: 4,
  active_task: "implement",
  status: "failed",
  agent: "codex",
  brief: "never display this private brief",
} as ForgeRun;
const session = {
  global_admin: true,
  user: { id: "a".repeat(32), authentication_method: "session" },
} as IdentitySession;
function html(r = run, s = session) {
  return renderToStaticMarkup(
    <CodingCheckRecovery
      run={r}
      session={s}
      connection={{
        base: "https://factory.example/api/v1",
        token: "never display token",
      }}
    />,
  );
}
it("introduces scoped check authority as a separate explicit administrator flow", () => {
  expect(html()).toContain("Recovery with configured checks");
  expect(html()).toContain("independent final verification");
  expect(html()).not.toContain(run.brief);
  expect(html()).not.toContain("never display token");
});
it("does not offer the grant flow to mapped identities, nonadmins or unrelated runs", () => {
  expect(html(run, { ...session, global_admin: false })).toBe("");
  expect(
    html(run, {
      ...session,
      user: { ...session.user, authentication_method: "scm_mapping" },
    }),
  ).toBe("");
  for (const patch of [
    { agent: "bridge" },
    { attempt: 3 },
    { active_task: "verify" },
  ])
    expect(html({ ...run, ...patch })).toBe("");
});
