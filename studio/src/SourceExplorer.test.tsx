/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { SourceExplorer } from "./SourceExplorer";

it("starts with a read-only verified-source loading state and never exposes connection credentials", () => {
  const html = renderToStaticMarkup(
    <SourceExplorer
      connection={{ base: "/api/v1", token: "private-ui-token-never-display" }}
      runId={"a".repeat(32)}
    />,
  );
  expect(html).toContain("Loading recorded snapshots");
  expect(html).toContain("Recorded source snapshot");
  expect(html).not.toContain("private-ui-token-never-display");
  expect(html).not.toMatch(/>Save|>Run|>Commit|<textarea|<iframe|<script/);
});
it("refuses an invalid run selector without inventing a workspace location", () => {
  const html = renderToStaticMarkup(
    <SourceExplorer
      connection={{ base: "/api/v1", token: "" }}
      runId="../private"
    />,
  );
  expect(html).toContain("Select a recorded run");
  expect(html).not.toContain("../private");
  expect(html).not.toContain("Loading recorded snapshots");
});
