/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { FinalOutcomeRecovery } from "./FinalOutcomeRecovery";
import type { IdentitySession } from "./Identity";
const session = {
  user: { id: "1".repeat(32), authentication_method: "local_bootstrap" },
  global_admin: true,
  memberships: [],
} as unknown as IdentitySession;
const props = {
  run: {
    id: "4".repeat(32),
    repository_id: "3".repeat(32),
    project_id: "2".repeat(32),
    brief: "private brief",
    status: "failed",
  },
  session,
  requestId: "5".repeat(32),
  connection: {
    base: "https://factory.example/api/v1",
    token: "private token",
  },
};
afterEach(() => vi.unstubAllGlobals());
it("introduces a saved-result check without assuming successful implementation or exposing private data", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(<FinalOutcomeRecovery {...props} />);
  expect(html).toContain("Saved agent result");
  expect(html).toContain("Check saved result");
  expect(html).toContain("claim the continuation");
  for (const value of [
    "private brief",
    "private token",
    "textarea",
    "Implementation succeeded",
  ])
    expect(html).not.toContain(value);
});
it("withholds mutation and private readback without current administrator access", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <FinalOutcomeRecovery
      {...props}
      session={{ ...session, global_admin: false }}
    />,
  );
  expect(html).toContain("factory administrator");
  expect(html).not.toContain("Check saved result");
});
it("retains a malformed browser action as held without offering another POST", () => {
  vi.stubGlobal("localStorage", { getItem: () => "not-json" });
  const html = renderToStaticMarkup(<FinalOutcomeRecovery {...props} />);
  expect(html).toContain("saved action identity");
  expect(html).not.toContain("Accept saved result");
});
