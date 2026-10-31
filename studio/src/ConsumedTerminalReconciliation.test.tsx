/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ConsumedTerminalReconciliation } from "./ConsumedTerminalReconciliation";
import type { IdentitySession } from "./Identity";
const props = {
  run: {
    id: "a".repeat(32),
    repository_id: "b".repeat(32),
    brief: "private brief",
    status: "paused",
    attempt: 3,
    consumed_terminal_recovery: {
      state: "claimed",
      request_id: "c".repeat(32),
      record_digest: "d".repeat(64),
      claim_digest: "e".repeat(64),
    },
  },
  session: {
    user: { id: "f".repeat(32), authentication_method: "local_bootstrap" },
    global_admin: true,
    memberships: [],
  } as unknown as IdentitySession,
  connection: { base: "/api/v1", token: "secret token" },
};
afterEach(() => vi.unstubAllGlobals());
it("introduces explicit same-result local postprocessing without revealing private run content", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <ConsumedTerminalReconciliation {...props} />,
  );
  expect(html).toContain("Reconcile the saved result");
  expect(html).toContain("Check claimed action");
  expect(html).toContain("local postprocessing");
  expect(html).not.toContain("private brief");
  expect(html).not.toContain("secret token");
  expect(html).not.toContain("<pre");
});
it("withholds private reads and mutation for another access role", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <ConsumedTerminalReconciliation
      {...props}
      session={{ ...props.session, global_admin: false }}
    />,
  );
  expect(html).toContain("original operator");
  expect(html).not.toContain("Check claimed action");
});
it("keeps a malformed durable marker held and offers readback only", () => {
  vi.stubGlobal("localStorage", { getItem: () => "broken marker" });
  const html = renderToStaticMarkup(
    <ConsumedTerminalReconciliation {...props} />,
  );
  expect(html).toContain("Read reconciliation receipt");
  expect(html).toContain("New submissions remain held");
});
