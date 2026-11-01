/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { FactoryFloor } from "./FactoryFloor";

it("keeps a concise floor instruction and an accessible complete stage guide", () => {
  const html = renderToStaticMarkup(
    <FactoryFloor
      connection={{ base: "/api/v1", token: "fictional" }}
      connected={true}
      theme="dark"
      onOpen={() => undefined}
      selection={{ kind: "factory", project: "", view: "floor", run: "" }}
      onSelectionChange={() => undefined}
    />,
  );
  expect(html).toContain("Factory overview");
  expect(html).toContain("Browse work");
  expect(html).toContain('aria-label="Recorded execution stages"');
  expect(html).toContain('data-carrier-mode="hidden"');
  expect(html).not.toContain("<select");
  expect(html).not.toContain("No production is running");
});
