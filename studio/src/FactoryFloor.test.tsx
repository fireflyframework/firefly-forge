/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { FactoryFloor } from "./FactoryFloor";

it("keeps a concise floor instruction and an accessible complete stage guide", () => {
  const html = renderToStaticMarkup(
    <FactoryFloor
      connection={{ base: "/api/v1", token: "fictional" }}
      connected={true}
      repositories={[]}
      runs={[]}
      theme="dark"
      onOpen={() => undefined}
      onCreate={() => undefined}
      selection={{ project: "", view: "floor", run: "" }}
      onSelectionChange={() => undefined}
    />,
  );
  expect(html).toContain("Select a stage to inspect its work.");
  expect(html).toContain("<summary>How stages work</summary>");
  expect(html).toContain("Prepare inspects source");
  expect(html).toContain("separate recorded actions");
  expect(html).toContain('aria-label="Recorded execution stages"');
  expect(html).toContain("No production is running in this view.");
  expect(html).toContain("Start a run");
});
