/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { CommunicationSettings } from "./CommunicationSettings";

it("explains one correction and held configured failures without promising a substitute", () => {
  const html = renderToStaticMarkup(
    <CommunicationSettings
      connection={{ base: "/api/v1", token: "" }}
      canConfigure
    />,
  );
  expect(html).toContain("one correction");
  expect(html).toContain("held without a substitute message");
  expect(html).not.toContain("forge sends its recorded update");
  expect(html).toContain("Recorded updates · no AI wording");
});
