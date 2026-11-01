/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import {
  sourceRevisions,
  sourceCatalog,
  sourceFile,
  verifySourceContent,
  alignedSourceText,
} from "./SourceExplorerData";

const sha = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
const reference = {
  protocol: "forge-source-archive-v1",
  snapshot_id: "b".repeat(32),
  tenant_id: "1".repeat(32),
  project_id: "2".repeat(32),
  repository_id: "3".repeat(32),
  run_id: "a".repeat(32),
  attempt: 3,
  head: "4".repeat(40),
  tree: "5".repeat(40),
  branch: "forge/retained-source",
  storage_binding: "9".repeat(64),
  catalog: {
    version: 1,
    artifact_id: "c".repeat(32),
    manifest_sha256: "d".repeat(64),
    source_digest: "e".repeat(64),
    manifest_digest: "f".repeat(64),
    byte_count: 3,
    file_count: 1,
    entry_count: 2,
    page_count: 1,
  },
};
const entry = {
  path: "src/app.ts",
  kind: "file" as const,
  mode: 33188,
  byte_count: 3,
  sha256: sha,
};
function revisions() {
  return {
    run_id: reference.run_id,
    items: [{ reference, status: "published", reason: null }],
    next_after: null,
    recording_enabled: true,
    availability: "recorded",
  };
}
function catalog() {
  return {
    reference,
    catalog: {
      version: 1,
      artifact_id: reference.catalog.artifact_id,
      manifest_sha256: reference.catalog.manifest_sha256,
      page: 0,
      next_page: null,
      entries: [entry],
    },
  };
}
function file() {
  return {
    reference,
    entry,
    catalog_page: 0,
    entry_index: 0,
    offset: 0,
    byte_count: 3,
    next_offset: null,
    sha256: sha,
    content_base64: "YWJj",
    binary: false,
    text: "abc",
  };
}
it("keeps recorded revisions contextual and binds catalog/file bytes to their exact snapshot", async () => {
  const ref = sourceRevisions(revisions(), reference.run_id).items[0].reference;
  const page = sourceCatalog(catalog(), ref, 0);
  const result = sourceFile(file(), ref, 0, 0, page.catalog.entries[0], 0);
  expect((await verifySourceContent(result)).text).toBe("abc");
  expect(result.reference.head).toBe(reference.head);
});
it.each([
  "run",
  "snapshot",
  "manifest",
  "project",
  "storage",
  "page",
  "entry",
  "offset",
  "extent",
  "path",
])("refuses mismatched %s before displaying any source", (mutation) => {
  const ref = sourceRevisions(revisions(), reference.run_id).items[0].reference;
  const value = structuredClone(file());
  if (mutation === "run") value.reference.run_id = "f".repeat(32);
  if (mutation === "snapshot") value.reference.snapshot_id = "f".repeat(32);
  if (mutation === "manifest")
    value.reference.catalog.manifest_sha256 = "f".repeat(64);
  if (mutation === "project") value.reference.project_id = "f".repeat(32);
  if (mutation === "storage") value.reference.storage_binding = "f".repeat(64);
  if (mutation === "page") value.catalog_page = 1;
  if (mutation === "entry") value.entry_index = 1;
  if (mutation === "offset") value.offset = 1;
  if (mutation === "extent") value.byte_count = 4;
  if (mutation === "path") value.entry.path = "../private";
  expect(() => sourceFile(value, ref, 0, 0, entry, 0)).toThrow();
});
it.each([
  ".env",
  ".git/config",
  "a//b",
  "a/../b",
  "/absolute",
  "keys/server.key",
  "a\\b",
])("does not admit private or aliased catalog path %s", (path) => {
  const ref = sourceRevisions(revisions(), reference.run_id).items[0].reference;
  const value = catalog();
  value.catalog.entries[0] = { ...entry, path };
  expect(() => sourceCatalog(value, ref, 0)).toThrow();
});
it("rejects unverifiable bytes, invented text and a false binary flag", async () => {
  const ref = sourceRevisions(revisions(), reference.run_id).items[0].reference;
  for (const changes of [
    { content_base64: "ZmFr" },
    { text: "invented" },
    { binary: true },
  ]) {
    await expect(
      (async () => {
        const value = sourceFile(
          { ...file(), ...changes },
          ref,
          0,
          0,
          entry,
          0,
        );
        return verifySourceContent(value);
      })(),
    ).rejects.toThrow();
  }
});
it("keeps pending/failed metadata unavailable and refuses unknown states or a foreign run", () => {
  for (const status of ["pending", "failed"]) {
    const value = revisions();
    value.items[0].status = status;
    expect(sourceRevisions(value, reference.run_id).items[0].status).toBe(
      status,
    );
  }
  const value = revisions();
  value.items[0].status = "succeeded";
  expect(() => sourceRevisions(value, reference.run_id)).toThrow();
  expect(() => sourceRevisions(revisions(), "f".repeat(32))).toThrow();
});

it("aligns multibyte text with at most three verified boundary bytes and preserves exact offsets", async () => {
  const ref = sourceRevisions(revisions(), reference.run_id).items[0].reference;
  const data = new TextEncoder().encode("a€b");
  const fullHash = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", data)),
    (value) => value.toString(16).padStart(2, "0"),
  ).join("");
  const selected = { ...entry, byte_count: data.length, sha256: fullHash };
  async function window(offset: number, limit: number) {
    const bytes = data.slice(offset, offset + limit);
    let text: string | null;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
        bytes,
      );
    } catch {
      text = null;
    }
    const digest = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      (value) => value.toString(16).padStart(2, "0"),
    ).join("");
    const page = sourceFile(
      {
        ...file(),
        entry: selected,
        offset,
        byte_count: bytes.length,
        next_offset:
          offset + bytes.length < data.length ? offset + bytes.length : null,
        sha256: digest,
        content_base64: btoa(String.fromCharCode(...bytes)),
        binary: text === null,
        text,
      },
      ref,
      0,
      0,
      selected,
      offset,
      limit,
    );
    return { page, ...(await verifySourceContent(page)) };
  }
  const calls: number[][] = [];
  const read = async (offset: number, limit: number) => {
    calls.push([offset, limit]);
    return window(offset, limit);
  };
  const first = await alignedSourceText(await window(0, 2), read);
  expect(first).toEqual({ text: "a€", start: 0, end: 4 });
  const second = await alignedSourceText(await window(2, 3), read);
  expect(second).toEqual({ text: "€b", start: 1, end: 5 });
  expect(calls.every(([, length]) => length <= 3)).toBe(true);
  expect((first.text ?? "") + (second.text ?? "")).not.toContain("�");
});
