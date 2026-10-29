/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { VerificationAdoption } from "./VerificationAdoption";
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
    status: "paused",
    agent: "bridge",
    worker_request: {
      id: "5".repeat(32),
      kind: "verification",
      status: "pending",
    },
  },
  session,
  connection: {
    base: "https://factory.example/api/v1",
    token: "private token",
  },
};
afterEach(() => vi.unstubAllGlobals());
it("introduces three deliberate steps without exposing private run or connection data", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(<VerificationAdoption {...props} />);
  for (const text of [
    "Read the checks",
    "Choose reuse",
    "Review and follow",
    "Read approved checks",
  ])
    expect(html).toContain(text);
  for (const text of [
    "private brief",
    "private token",
    "Save verification approval",
  ])
    expect(html).not.toContain(text);
});
it("withholds approval without native execution access", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <VerificationAdoption
      {...props}
      session={{ ...session, global_admin: false }}
    />,
  );
  expect(html).toContain("current execute access");
  expect(html).not.toContain("Read approved checks");
});
it("holds unreadable saved intent without offering a replacement", () => {
  vi.stubGlobal("localStorage", { getItem: () => "unreadable" });
  const html = renderToStaticMarkup(<VerificationAdoption {...props} />);
  expect(html).toContain("Keep the saved submission identity");
  expect(html).not.toContain("Read approved checks");
});
it("offers configured providers and reserves host CLIs for factory administrators", () => {
  vi.stubGlobal("localStorage", { getItem: () => null });
  const html = renderToStaticMarkup(
    <VerificationAdoption
      {...props}
      session={{
        ...session,
        global_admin: false,
        memberships: [
          {
            project_id: props.run.project_id,
            role: "developer",
            revision: 1,
            permissions: ["execute"],
          },
        ],
      }}
    />,
  );
  expect(html).toContain(
    '<option value="agentic">Configured AI model</option>',
  );
  expect(html).toContain(
    '<option value="bridge" selected="">Agent bridge</option>',
  );
  expect(html).toContain(
    '<option value="codex" disabled="">Codex CLI</option>',
  );
  expect(html).toContain(
    '<option value="claude" disabled="">Claude Code CLI</option>',
  );
  expect(html).toContain("Read approved checks");
});
it.each(["publishing", "scm_mapping", "cancellation"])(
  "withholds approval during %s without changing saved intent",
  (reason) => {
    vi.stubGlobal("localStorage", { getItem: () => null });
    const html = renderToStaticMarkup(
      <VerificationAdoption
        {...props}
        run={{
          ...props.run,
          publish: reason === "publishing",
          ...(reason === "cancellation"
            ? { cancel_request: { id: "6".repeat(32), status: "requested" } }
            : {}),
        }}
        session={{
          ...session,
          global_admin: reason !== "publishing",
          user: {
            ...session.user,
            authentication_method:
              reason === "scm_mapping" ? "scm_mapping" : "local_bootstrap",
          },
          memberships: [
            {
              project_id: props.run.project_id,
              role: "developer",
              revision: 1,
              permissions: ["execute"],
            },
          ],
        }}
      />,
    );
    expect(html).toContain("current execute access");
    expect(html).not.toContain("Read approved checks");
  },
);
