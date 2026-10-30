/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { IdentityMenu, type IdentitySession } from "./Identity";

it("keeps the foundation copyright in the account and sign-out dialog", () => {
  const session = {
    user: { id: "a".repeat(32), authentication_method: "oidc_session" },
    memberships: [],
  } as unknown as IdentitySession;
  const html = renderToStaticMarkup(
    <IdentityMenu
      connection={{ base: "/api/v1", token: "" }}
      session={session}
      onSignedOut={() => {}}
      onAccess={() => {}}
    />,
  );
  expect(html).toContain('class="foundation-footer identity-copyright"');
  expect(html).toContain("© 2026 Firefly Software Foundation");
  expect(html).toContain("Made with");
  expect(html).toContain("Sign out");
});
