/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { afterEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import packet from "../tests/scm-poll-resume-contract-fixture.json";
import type { IdentitySession } from "./Identity";
import type { ProjectSnapshot } from "./api";
import type { PollingBindingReceipt } from "./ScmPollingData";
import {
  ScmPollResumeConsent,
  ScmPollResumePanel,
} from "./ScmPollResumeConsent";
const project = {
  id: packet.binding.project_id,
  revision: 1,
  digest: "a".repeat(64),
  definition: {
    components: [
      {
        id: "api",
        name: "Inventory API",
        repository_id: packet.binding.repository_id,
      },
    ],
  },
} as unknown as ProjectSnapshot;
const session = {
  global_admin: true,
  user: {
    id: packet.receipt.administering_user_id,
    authentication_method: "local",
  },
  memberships: [],
} as unknown as IdentitySession;
const connection = {
  base: "https://factory.example.test",
  token: "fictional-local-test-token",
};
afterEach(() => vi.unstubAllGlobals());
it("introduces GitHub polling separately without claiming configuration is empty", () => {
  vi.stubGlobal("window", {
    location: { href: "https://factory.example.test/" },
  });
  const html = renderToStaticMarkup(
    <ScmPollResumePanel
      connection={connection}
      session={session}
      project={project}
      repository={packet.binding.repository_id}
      people={[]}
    />,
  );
  expect(html).toContain("Local GitHub continuation");
  expect(html).toContain("Read GitHub accounts");
  expect(html).not.toContain("No accounts");
  expect(html).not.toContain("fictional-local-test-token");
});
it("offers explicit transport-specific consent without running work", () => {
  vi.stubGlobal("window", {
    location: { href: "https://factory.example.test/" },
  });
  const html = renderToStaticMarkup(
    <ScmPollResumeConsent
      connection={connection}
      session={session}
      project={project}
      binding={packet.binding as PollingBindingReceipt}
      personName="Test operator"
    />,
  );
  expect(html).toContain("Review GitHub continuation");
  expect(html).toContain("does not start a run");
  expect(html).not.toContain("signed webhook");
  expect(html).not.toContain("fictional-local-test-token");
});
it("does not offer management controls to a project reader", () => {
  vi.stubGlobal("window", {
    location: { href: "https://factory.example.test/" },
  });
  const reader = {
    ...session,
    global_admin: false,
    memberships: [{ project_id: project.id, permissions: ["read"] }],
  } as unknown as IdentitySession;
  const html = renderToStaticMarkup(
    <ScmPollResumeConsent
      connection={connection}
      session={reader}
      project={project}
      binding={packet.binding as PollingBindingReceipt}
      personName="Test operator"
    />,
  );
  expect(html).not.toContain("Review GitHub continuation");
  expect(html).toContain("Read current GitHub consent");
});
