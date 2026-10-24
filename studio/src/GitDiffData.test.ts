/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  diffReference,
  diffIndexPage,
  diffFilePage,
  verifyDiffContent,
} from "./GitDiffData";

const reference = {
  version: 1 as const,
  artifact_id: "a".repeat(32),
  manifest_sha256: "b".repeat(64),
  sha256: "c".repeat(64),
  byte_count: 50_000_000,
  file_count: 20_000,
  index_page_count: 200,
};
it("keeps a large diff as an immutable reference without loading its content", () => {
  expect(diffReference(reference)).toEqual(reference);
  expect(() =>
    diffReference({ ...reference, host_path: "/private/file" }),
  ).toThrow();
  expect(() =>
    diffReference({ ...reference, byte_count: Number.NaN }),
  ).toThrow();
});
it("binds an index page to its exact artifact and manifest", () => {
  const page = {
    version: 1,
    artifact_id: reference.artifact_id,
    manifest_sha256: reference.manifest_sha256,
    page: 0,
    next_page: 1,
    files: [
      {
        file_index: 0,
        path: "backend/service.py",
        offset: 0,
        byte_count: 200,
        sha256: "d".repeat(64),
      },
    ],
  };
  expect(diffIndexPage(page, reference, 0).files[0].path).toBe(
    "backend/service.py",
  );
  expect(() =>
    diffIndexPage({ ...page, manifest_sha256: "e".repeat(64) }, reference, 0),
  ).toThrow();
  expect(() => diffIndexPage({ ...page, page: 1 }, reference, 0)).toThrow();
});
it("preserves byte offsets and refuses content for another selected file", () => {
  const page = {
    version: 1,
    artifact_id: reference.artifact_id,
    manifest_sha256: reference.manifest_sha256,
    file_index: 0,
    offset: 0,
    byte_count: 3,
    total_bytes: 20,
    next_offset: 3,
    content_base64: "YWJj",
    content_sha256: "d".repeat(64),
    starts_mid_line: false,
    ends_mid_line: true,
    utf8_complete: true,
  };
  expect(diffFilePage(page, reference, 0, 0).next_offset).toBe(3);
  expect(() =>
    diffFilePage({ ...page, file_index: 1 }, reference, 0, 0),
  ).toThrow();
  expect(() =>
    diffFilePage({ ...page, byte_count: 5 }, reference, 0, 0),
  ).toThrow();
});

it("verifies the exact page bytes before displaying code", async () => {
  const page = diffFilePage(
    {
      version: 1,
      artifact_id: reference.artifact_id,
      manifest_sha256: reference.manifest_sha256,
      file_index: 0,
      offset: 0,
      byte_count: 3,
      total_bytes: 3,
      next_offset: null,
      content_base64: "YWJj",
      content_sha256:
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
      starts_mid_line: false,
      ends_mid_line: false,
      utf8_complete: true,
    },
    reference,
    0,
    0,
  );
  expect(new TextDecoder().decode(await verifyDiffContent(page))).toBe("abc");
  await expect(
    verifyDiffContent({ ...page, content_base64: "eHl6" }),
  ).rejects.toThrow();
});

it("refuses a page that silently stops before its declared file extent", () => {
  expect(() =>
    diffFilePage(
      {
        version: 1,
        artifact_id: reference.artifact_id,
        manifest_sha256: reference.manifest_sha256,
        file_index: 0,
        offset: 0,
        byte_count: 3,
        total_bytes: 20,
        next_offset: null,
        content_base64: "YWJj",
        content_sha256: "d".repeat(64),
        starts_mid_line: false,
        ends_mid_line: true,
        utf8_complete: true,
      },
      reference,
      0,
      0,
    ),
  ).toThrow();
});
