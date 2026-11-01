/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { ApiError } from "./api";

export type CatalogReference = {
  version: 1;
  artifact_id: string;
  manifest_sha256: string;
  source_digest: string;
  manifest_digest: string;
  byte_count: number;
  file_count: number;
  entry_count: number;
  page_count: number;
};
export type SourceReference = {
  protocol: "forge-source-archive-v1";
  snapshot_id: string;
  tenant_id: string;
  project_id: string;
  repository_id: string;
  run_id: string;
  attempt: number;
  head: string;
  tree: string;
  branch: string | null;
  catalog: CatalogReference;
  storage_binding: string;
};
export type SourceRevision = {
  reference: SourceReference;
  status: "published" | "pending" | "failed";
  reason: string | null;
};
export type SourceRevisions = {
  run_id: string;
  items: SourceRevision[];
  next_after: string | null;
  recording_enabled: boolean;
  availability: "recorded" | "unavailable";
};
export type SourceEntry = {
  path: string;
  kind: "file" | "directory";
  mode: number;
  byte_count: number;
  sha256: string | null;
};
export type SourceCatalog = {
  reference: SourceReference;
  catalog: {
    version: 1;
    artifact_id: string;
    manifest_sha256: string;
    page: number;
    next_page: number | null;
    entries: SourceEntry[];
  };
};
export type SourceFile = {
  reference: SourceReference;
  entry: SourceEntry;
  catalog_page: number;
  entry_index: number;
  offset: number;
  byte_count: number;
  next_offset: number | null;
  sha256: string;
  content_base64: string;
  binary: boolean;
  text: string | null;
};

class SourceIntegrityError extends Error {}
type Row = Record<string, unknown>;
function check(value: unknown): asserts value {
  if (!value)
    throw new SourceIntegrityError(
      "The recorded source response could not be verified.",
    );
}
function row(value: unknown): Row {
  check(value && typeof value === "object" && !Array.isArray(value));
  return value as Row;
}
function fields(value: Row, names: string) {
  check(
    Object.keys(value).sort().join(" ") === names.split(" ").sort().join(" "),
  );
}
function integer(value: unknown): number {
  check(typeof value === "number" && Number.isSafeInteger(value) && value >= 0);
  return value;
}
function hash(value: unknown): string {
  check(typeof value === "string" && /^[a-f0-9]{64}$/.test(value));
  return value;
}
function id(value: unknown): string {
  check(typeof value === "string" && /^[a-f0-9]{32}$/.test(value));
  return value;
}
function revision(value: unknown): string {
  check(
    typeof value === "string" && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(value),
  );
  return value;
}
function hasControls(value: string) {
  return [...value].some(
    (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
  );
}
function catalogReference(value: unknown): CatalogReference {
  const r = row(value);
  fields(
    r,
    "version artifact_id manifest_sha256 source_digest manifest_digest byte_count file_count entry_count page_count",
  );
  check(r.version === 1);
  const count = integer(r.entry_count),
    pages = integer(r.page_count),
    files = integer(r.file_count);
  check(
    files <= count && (count === 0 ? pages === 0 : pages > 0 && pages <= count),
  );
  return {
    version: 1,
    artifact_id: id(r.artifact_id),
    manifest_sha256: hash(r.manifest_sha256),
    source_digest: hash(r.source_digest),
    manifest_digest: hash(r.manifest_digest),
    byte_count: integer(r.byte_count),
    file_count: files,
    entry_count: count,
    page_count: pages,
  };
}
export function sourceReference(
  value: unknown,
  runId: string,
): SourceReference {
  const r = row(value);
  fields(
    r,
    "protocol snapshot_id tenant_id project_id repository_id run_id attempt head tree branch catalog storage_binding",
  );
  check(r.protocol === "forge-source-archive-v1" && r.run_id === id(runId));
  check(
    r.branch === null ||
      (typeof r.branch === "string" &&
        r.branch.length <= 1024 &&
        !hasControls(r.branch)),
  );
  return {
    protocol: "forge-source-archive-v1",
    snapshot_id: id(r.snapshot_id),
    tenant_id: id(r.tenant_id),
    project_id: id(r.project_id),
    repository_id: id(r.repository_id),
    run_id: id(r.run_id),
    attempt: integer(r.attempt),
    head: revision(r.head),
    tree: revision(r.tree),
    branch: r.branch as string | null,
    catalog: catalogReference(r.catalog),
    storage_binding: hash(r.storage_binding),
  };
}
function same(value: unknown, expected: SourceReference): SourceReference {
  const ref = sourceReference(value, expected.run_id);
  check(JSON.stringify(ref) === JSON.stringify(expected));
  return ref;
}
export function sourceRevisions(
  value: unknown,
  runId: string,
  after: string | null = null,
): SourceRevisions {
  const r = row(value);
  fields(r, "run_id items next_after recording_enabled availability");
  check(
    r.run_id === id(runId) &&
      typeof r.recording_enabled === "boolean" &&
      Array.isArray(r.items) &&
      r.items.length <= 128,
  );
  let previous = after === null ? null : id(after);
  const items = r.items.map((item): SourceRevision => {
    const itemRow = row(item);
    fields(itemRow, "reference status reason");
    check(
      itemRow.status === "published" ||
        itemRow.status === "pending" ||
        itemRow.status === "failed",
    );
    check(
      itemRow.reason === null ||
        itemRow.reason === "source_publication_failed_reconciliation_required",
    );
    const ref = sourceReference(itemRow.reference, runId);
    check(previous === null || ref.snapshot_id > previous);
    previous = ref.snapshot_id;
    return {
      reference: ref,
      status: itemRow.status,
      reason: itemRow.reason as string | null,
    };
  });
  check(r.availability === (items.length ? "recorded" : "unavailable"));
  const next = r.next_after === null ? null : id(r.next_after);
  check(next === null || (items.length === 128 && next === previous));
  return {
    run_id: runId,
    items,
    next_after: next,
    recording_enabled: r.recording_enabled,
    availability: r.availability as SourceRevisions["availability"],
  };
}
function entry(value: unknown): SourceEntry {
  const r = row(value);
  fields(r, "path kind mode byte_count sha256");
  check(
    typeof r.path === "string" &&
      r.path.length > 0 &&
      !r.path.startsWith("/") &&
      !r.path.includes("\\") &&
      !hasControls(r.path),
  );
  const parts = r.path.replace(/\/$/, "").split("/");
  const privateNames = new Set([
    ".git",
    ".codex",
    ".claude",
    ".forge",
    "node_modules",
    ".venv",
    "vendor",
    ".env",
    ".npmrc",
    ".pypirc",
    ".ssh",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
  ]);
  check(
    parts.every(
      (part) =>
        part &&
        part !== "." &&
        part !== ".." &&
        !privateNames.has(part.toLowerCase()) &&
        !/\.(?:pem|key)$/i.test(part) &&
        !(
          part.toLowerCase().startsWith(".env.") &&
          ![".env.example", ".env.sample", ".env.template"].includes(
            part.toLowerCase(),
          )
        ),
    ),
  );
  const mode = integer(r.mode),
    bytes = integer(r.byte_count);
  check(mode <= 0o177777);
  check(r.kind === "file" || r.kind === "directory");
  if (r.kind === "file")
    check(
      !r.path.endsWith("/") &&
        (mode & 0o170000) === 0o100000 &&
        typeof r.sha256 === "string",
    );
  else
    check(
      r.path.endsWith("/") &&
        (mode & 0o170000) === 0o040000 &&
        r.sha256 === null &&
        bytes === 0,
    );
  return {
    path: r.path,
    kind: r.kind,
    mode,
    byte_count: bytes,
    sha256: r.sha256 === null ? null : hash(r.sha256),
  };
}
export function sourceCatalog(
  value: unknown,
  reference: SourceReference,
  page: number,
): SourceCatalog {
  const r = row(value);
  fields(r, "reference catalog");
  const ref = same(r.reference, reference),
    c = row(r.catalog);
  fields(c, "version artifact_id manifest_sha256 page next_page entries");
  check(
    c.version === 1 &&
      c.artifact_id === ref.catalog.artifact_id &&
      c.manifest_sha256 === ref.catalog.manifest_sha256 &&
      c.page === integer(page),
  );
  check(
    page < ref.catalog.page_count ||
      (page === 0 && ref.catalog.entry_count === 0),
  );
  check(
    Array.isArray(c.entries) &&
      c.entries.length <= 128 &&
      c.entries.length <= ref.catalog.entry_count,
  );
  const entries = c.entries.map(entry);
  check(entries.every((item, i) => i === 0 || entries[i - 1].path < item.path));
  const next = c.next_page === null ? null : integer(c.next_page);
  check(next === (page + 1 < ref.catalog.page_count ? page + 1 : null));
  return {
    reference: ref,
    catalog: {
      version: 1,
      artifact_id: ref.catalog.artifact_id,
      manifest_sha256: ref.catalog.manifest_sha256,
      page,
      next_page: next,
      entries,
    },
  };
}
export function sourceFile(
  value: unknown,
  reference: SourceReference,
  page: number,
  entryIndex: number,
  selected: SourceEntry,
  offset: number,
  limit = 32768,
): SourceFile {
  const r = row(value);
  fields(
    r,
    "reference entry catalog_page entry_index offset byte_count next_offset sha256 content_base64 binary text",
  );
  const ref = same(r.reference, reference),
    file = entry(r.entry);
  check(
    file.kind === "file" && JSON.stringify(file) === JSON.stringify(selected),
  );
  check(
    r.catalog_page === integer(page) &&
      r.entry_index === integer(entryIndex) &&
      r.offset === integer(offset),
  );
  const count = integer(r.byte_count),
    total = file.byte_count;
  check(
    count <= limit &&
      offset + count <= total &&
      (count > 0 || offset === total),
  );
  const next = r.next_offset === null ? null : integer(r.next_offset);
  check(next === (offset + count < total ? offset + count : null));
  check(
    typeof r.content_base64 === "string" &&
      r.content_base64.length === 4 * Math.ceil(count / 3) &&
      /^[A-Za-z0-9+/]*={0,2}$/.test(r.content_base64),
  );
  check(
    typeof r.binary === "boolean" &&
      (r.text === null || typeof r.text === "string") &&
      r.binary === (r.text === null),
  );
  return {
    reference: ref,
    entry: file,
    catalog_page: page,
    entry_index: entryIndex,
    offset,
    byte_count: count,
    next_offset: next,
    sha256: hash(r.sha256),
    content_base64: r.content_base64,
    binary: r.binary,
    text: r.text as string | null,
  };
}
export async function verifySourceContent(
  page: SourceFile,
): Promise<{ bytes: Uint8Array; text: string | null }> {
  const bytes = Uint8Array.from(atob(page.content_base64), (char) =>
    char.charCodeAt(0),
  );
  const digest = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (value) => value.toString(16).padStart(2, "0"),
  ).join("");
  check(bytes.length === page.byte_count && digest === page.sha256);
  let text: string | null;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      bytes,
    );
    if (text.includes("\0")) text = null;
  } catch {
    text = null;
  }
  check(page.text === text && page.binary === (text === null));
  return { bytes, text };
}
export type VerifiedSourceWindow = {
  page: SourceFile;
  bytes: Uint8Array;
  text: string | null;
};
export async function alignedSourceText(
  window: VerifiedSourceWindow,
  read: (offset: number, limit: number) => Promise<VerifiedSourceWindow>,
): Promise<{ text: string | null; start: number; end: number }> {
  const end = window.page.offset + window.bytes.length;
  const raw = { text: window.text, start: window.page.offset, end };
  if (window.text !== null || !window.bytes.length || window.bytes.includes(0))
    return raw;
  const length = (byte: number) =>
    byte >= 0xc2 && byte <= 0xdf
      ? 2
      : byte >= 0xe0 && byte <= 0xef
        ? 3
        : byte >= 0xf0 && byte <= 0xf4
          ? 4
          : 0;
  async function boundary(offset: number, count: number) {
    const value = await read(offset, count);
    check(
      value.page.offset === offset &&
        value.bytes.length === count &&
        JSON.stringify(value.page.reference) ===
          JSON.stringify(window.page.reference) &&
        JSON.stringify(value.page.entry) ===
          JSON.stringify(window.page.entry) &&
        value.page.catalog_page === window.page.catalog_page &&
        value.page.entry_index === window.page.entry_index,
    );
    return value.bytes;
  }
  let prefix: Uint8Array = new Uint8Array(),
    suffix: Uint8Array = new Uint8Array();
  if ((window.bytes[0] & 0xc0) === 0x80 && window.page.offset > 0) {
    const count = Math.min(3, window.page.offset),
      before = await boundary(window.page.offset - count, count);
    for (let i = before.length - 1; i >= 0; i--) {
      if (length(before[i]) > before.length - i) {
        prefix = before.slice(i);
        break;
      }
    }
    if (!prefix.length) return raw;
  }
  let last = window.bytes.length - 1;
  while (last >= 0 && (window.bytes[last] & 0xc0) === 0x80) last--;
  const needed =
    last >= 0
      ? Math.max(0, length(window.bytes[last]) - (window.bytes.length - last))
      : 0;
  if (needed && end < window.page.entry.byte_count)
    suffix = await boundary(
      end,
      Math.min(needed, window.page.entry.byte_count - end),
    );
  if (!prefix.length && !suffix.length) return raw;
  const bytes = new Uint8Array(
    prefix.length + window.bytes.length + suffix.length,
  );
  bytes.set(prefix);
  bytes.set(window.bytes, prefix.length);
  bytes.set(suffix, prefix.length + window.bytes.length);
  try {
    const text = new TextDecoder("utf-8", {
      fatal: true,
      ignoreBOM: true,
    }).decode(bytes);
    if (!text.includes("\0"))
      return {
        text,
        start: window.page.offset - prefix.length,
        end: end + suffix.length,
      };
  } catch {
    /* Preserve undecodable data as exact bytes. */
  }
  return raw;
}
export function sourceProblem(error: unknown): {
  kind: string;
  title: string;
  message: string;
} {
  if (error instanceof ApiError && [401, 403].includes(error.status))
    return {
      kind: "revoked",
      title: "Source access is unavailable",
      message:
        "Sign in or check your current project access, then refresh. Previous source content has been cleared.",
    };
  if (
    error instanceof SourceIntegrityError ||
    (error instanceof ApiError && error.status === 409)
  )
    return {
      kind: "review_required",
      title: "Snapshot needs review",
      message:
        "The recorded source could not be verified with current access and configuration. Refresh its records before continuing.",
    };
  if (error instanceof ApiError && error.status === 404)
    return {
      kind: "unavailable",
      title: "Recorded source is unavailable",
      message: "Check the selected run and server, then refresh its records.",
    };
  return {
    kind: "error",
    title: "Source records could not be loaded",
    message:
      "Check the connection and refresh. No unverified source is displayed.",
  };
}
