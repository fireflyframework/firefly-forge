/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { EvidenceView } from "./ExecutionPresentation";
const manifest = {
  version: 1,
  run_id: "a".repeat(32),
  feedback_id: "design-question",
  request_id: "ownership",
  images: [
    {
      id: "b".repeat(64),
      filename: "choice.png",
      mime_type: "image/png",
      bytes: 1240,
      sha256: "c".repeat(64),
    },
  ],
};
it("explains retained question evidence without making a product QA claim", () => {
  const html = renderToStaticMarkup(
    <EvidenceView kind="worker_evidence" value={manifest} />,
  );
  const primary = html.split("<details")[0];
  expect(primary).toContain("Retained decision screenshots");
  expect(primary).toContain("choice.png");
  expect(primary).toContain("not product acceptance");
  expect(primary).not.toContain(manifest.images[0].sha256);
  expect(primary).not.toContain('"images"');
  expect(html).not.toContain("<details open");
});
it("holds a malformed evidence version instead of labeling it retained", () => {
  const html = renderToStaticMarkup(
    <EvidenceView
      kind="worker_evidence"
      value={{ ...manifest, version: true }}
    />,
  );
  expect(html).toContain("Screenshot record could not be verified");
  expect(html.split("<details")[0]).not.toContain("choice.png");
});
