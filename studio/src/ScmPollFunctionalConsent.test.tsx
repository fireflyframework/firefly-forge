/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import packet from "../tests/scm-poll-resume-contract-fixture.json";
import type { IdentitySession } from "./Identity";
import type { ProjectSnapshot } from "./api";
import type { PollingBindingReceipt } from "./ScmPollingData";
import { ScmPollFunctionalConsent } from "./ScmPollFunctionalConsent";
const project = { id: packet.binding.project_id } as ProjectSnapshot;
const connection = {
  base: "https://factory.invalid",
  token: "never-render-this-token",
};
const session = {
  global_admin: true,
  user: { id: packet.receipt.administering_user_id },
} as IdentitySession;
it("starts with a model-free review and clearly separate consent", () => {
  const html = renderToStaticMarkup(
    <ScmPollFunctionalConsent
      connection={connection}
      session={session}
      project={project}
      binding={packet.binding as PollingBindingReceipt}
      personName="Public operator"
    />,
  );
  expect(html).toContain("GitHub functional assistant");
  expect(html).toContain(
    "Existing status, answer and resume permissions remain unchanged",
  );
  expect(html).toContain("Review functional scope");
  expect(html).not.toContain("Record separate consent");
  expect(html).not.toContain(connection.token);
});
it("does not offer administration to a project reader", () => {
  const html = renderToStaticMarkup(
    <ScmPollFunctionalConsent
      connection={connection}
      session={{ ...session, global_admin: false }}
      project={project}
      binding={packet.binding as PollingBindingReceipt}
      personName="Public operator"
    />,
  );
  expect(html).toBe("");
});
