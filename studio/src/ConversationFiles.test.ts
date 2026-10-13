/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import {
  attachmentSelectionError,
  type ConversationAttachment,
} from "./ConversationFiles";
const file = (
  kind: "document" | "image",
  text_chars = 0,
): ConversationAttachment => ({
  id: "a",
  request_id: "b",
  filename: "file.md",
  kind,
  mime_type: "text/markdown",
  content_bytes: 10,
  sha256: "c",
  status: "ready",
  text_chars,
  truncated: false,
  created_at: "now",
});
describe("conversation attachment limits", () => {
  it("bounds selected files and image count independently", () => {
    expect(
      attachmentSelectionError(
        Array.from({ length: 9 }, () => file("document")),
      ),
    ).toContain("eight files");
    expect(
      attachmentSelectionError(Array.from({ length: 6 }, () => file("image"))),
    ).toContain("five images");
    expect(
      attachmentSelectionError([
        ...Array.from({ length: 5 }, () => file("image")),
        file("document", 30000),
        file("document", 30000),
      ]),
    ).toBe("");
  });
  it("uses actual extracted text metadata to bound total context", () => {
    expect(
      attachmentSelectionError([
        file("document", 30000),
        file("document", 30000),
        file("document", 1),
      ]),
    ).toContain("60,000");
  });
});
