/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type DiffReference = {
  version: 1;
  artifact_id: string;
  manifest_sha256: string;
  sha256: string;
  byte_count: number;
  file_count: number;
  index_page_count: number;
};
export type DiffFile = {
  file_index: number;
  path: string;
  offset: number;
  byte_count: number;
  sha256: string;
};
export type DiffIndexPage = {
  artifact_id: string;
  manifest_sha256: string;
  page: number;
  next_page: number | null;
  files: DiffFile[];
};
export type DiffFilePage = {
  artifact_id: string;
  manifest_sha256: string;
  file_index: number;
  offset: number;
  byte_count: number;
  total_bytes: number;
  next_offset: number | null;
  content_base64: string;
  content_sha256: string;
  starts_mid_line: boolean;
  ends_mid_line: boolean;
  utf8_complete: boolean;
};
type Data = Record<string, unknown>;
function check(value: unknown): asserts value {
  if (!value)
    throw Error(
      "The requested diff page could not be verified. Refresh its saved reference.",
    );
}
function object(value: unknown): Data {
  check(value && typeof value === "object" && !Array.isArray(value));
  return value as Data;
}
function integer(value: unknown): number {
  check(typeof value === "number" && Number.isSafeInteger(value) && value >= 0);
  return value;
}
function hash(value: unknown): string {
  check(typeof value === "string" && /^[a-f0-9]{64}$/.test(value));
  return value;
}
function identity(value: unknown): string {
  check(typeof value === "string" && /^[a-f0-9]{32}$/.test(value));
  return value;
}
function fields(row: Data, expected: string) {
  check(
    Object.keys(row).sort().join(" ") === expected.split(" ").sort().join(" "),
  );
}
export function diffReference(value: unknown): DiffReference {
  const r = object(value);
  fields(
    r,
    "version artifact_id manifest_sha256 sha256 byte_count file_count index_page_count",
  );
  check(r.version === 1);
  return {
    version: 1,
    artifact_id: identity(r.artifact_id),
    manifest_sha256: hash(r.manifest_sha256),
    sha256: hash(r.sha256),
    byte_count: integer(r.byte_count),
    file_count: integer(r.file_count),
    index_page_count: integer(r.index_page_count),
  };
}
function scope(row: Data, reference: DiffReference) {
  check(
    row.artifact_id === reference.artifact_id &&
      row.manifest_sha256 === reference.manifest_sha256,
  );
  check(row.version === 1);
}
export function diffIndexPage(
  value: unknown,
  reference: DiffReference,
  page: number,
): DiffIndexPage {
  const r = object(value);
  fields(r, "version artifact_id manifest_sha256 page next_page files");
  scope(r, reference);
  check(r.page === page);
  check(Array.isArray(r.files) && r.files.length <= 1000);
  const next = r.next_page === null ? null : integer(r.next_page);
  check(next === null || (next > page && next < reference.index_page_count));
  const files = r.files.map((value): DiffFile => {
    const f = object(value);
    fields(f, "file_index path offset byte_count sha256");
    const index = integer(f.file_index),
      offset = integer(f.offset),
      bytes = integer(f.byte_count);
    check(
      index < reference.file_count &&
        bytes > 0 &&
        offset + bytes <= reference.byte_count,
    );
    check(
      typeof f.path === "string" &&
        f.path.length > 0 &&
        f.path.length <= 16384 &&
        !f.path.startsWith("/") &&
        !f.path.includes("\0") &&
        !f.path.split("/").includes(".."),
    );
    return {
      file_index: index,
      path: f.path,
      offset,
      byte_count: bytes,
      sha256: hash(f.sha256),
    };
  });
  check(
    files.every((f, i) => i === 0 || files[i - 1].file_index < f.file_index),
  );
  return {
    artifact_id: reference.artifact_id,
    manifest_sha256: reference.manifest_sha256,
    page,
    next_page: next,
    files,
  };
}
export function diffFilePage(
  value: unknown,
  reference: DiffReference,
  fileIndex: number,
  offset: number,
): DiffFilePage {
  const r = object(value);
  fields(
    r,
    "version artifact_id manifest_sha256 file_index offset byte_count total_bytes next_offset content_base64 content_sha256 starts_mid_line ends_mid_line utf8_complete",
  );
  scope(r, reference);
  check(r.file_index === fileIndex && r.offset === offset);
  const bytes = integer(r.byte_count),
    total = integer(r.total_bytes);
  check(
    bytes <= 1048576 &&
      total <= reference.byte_count &&
      offset + bytes <= total,
  );
  const next = r.next_offset === null ? null : integer(r.next_offset);
  check(
    (next === null && offset + bytes === total) ||
      (next === offset + bytes && next > offset && next < total),
  );
  check(
    typeof r.content_base64 === "string" &&
      r.content_base64.length <= 1398104 &&
      /^[A-Za-z0-9+/]*={0,2}$/.test(r.content_base64),
  );
  check(atob(r.content_base64).length === bytes);
  for (const key of ["starts_mid_line", "ends_mid_line", "utf8_complete"])
    check(typeof r[key] === "boolean");
  return {
    artifact_id: reference.artifact_id,
    manifest_sha256: reference.manifest_sha256,
    file_index: fileIndex,
    offset,
    byte_count: bytes,
    total_bytes: total,
    next_offset: next,
    content_base64: r.content_base64,
    content_sha256: hash(r.content_sha256),
    starts_mid_line: r.starts_mid_line as boolean,
    ends_mid_line: r.ends_mid_line as boolean,
    utf8_complete: r.utf8_complete as boolean,
  };
}
export async function verifyDiffContent(
  page: DiffFilePage,
): Promise<Uint8Array> {
  const bytes = Uint8Array.from(atob(page.content_base64), (c) =>
    c.charCodeAt(0),
  );
  const digest = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (b) => b.toString(16).padStart(2, "0"),
  ).join("");
  check(digest === page.content_sha256);
  return bytes;
}
