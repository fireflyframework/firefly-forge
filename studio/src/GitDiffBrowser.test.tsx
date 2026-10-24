/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { it, expect } from "vitest";
import { GitDiffBrowser } from "./GitDiffBrowser";
it("offers a read-only bounded diff browser without loading all changed paths", () => {
  const html = renderToStaticMarkup(
    <GitDiffBrowser
      connection={{ base: "/api/v1", token: "" }}
      runId={"f".repeat(32)}
      artifact={{
        version: 1,
        artifact_id: "a".repeat(32),
        manifest_sha256: "b".repeat(64),
        sha256: "c".repeat(64),
        byte_count: 50_000_000,
        file_count: 20_000,
        index_page_count: 200,
      }}
    />,
  );
  expect(html).toContain("20,000 changed files");
  expect(html).toContain("Loading file index");
  expect(html).not.toContain(">Save</button>");
  expect(html).not.toContain("50000000");
});
