/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AssistantSources } from "./AssistantSources";

it("shows receipt names and preserves exact citation IDs inside expandable details", () => {
  const html = renderToStaticMarkup(
    <AssistantSources
      ids={["document-receipt", "knowledge-receipt", "unmapped-receipt"]}
      context={{
        attachments: [{ id: "document-receipt", filename: "journey-notes.md" }],
        knowledge: {
          citations: [{ id: "knowledge-receipt", source_title: "Stock rules" }],
        },
      }}
    />,
  );
  const [visible, receipts] = html.split("<details");
  expect(visible).toContain("journey-notes.md");
  expect(visible).toContain("Stock rules");
  expect(visible).toContain("Source 3");
  expect(visible).not.toContain("document-receipt");
  expect(receipts).toContain("document-receipt");
  expect(receipts).toContain("knowledge-receipt");
  expect(receipts).toContain("unmapped-receipt");
});
