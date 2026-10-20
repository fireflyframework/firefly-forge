/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
const ID = /^[a-f0-9]{32}$/,
  HASH = /^[a-f0-9]{64}$/;
const CHUNK_BYTES = 16384,
  MAX_BYTES = 4194304;
export type ActivityDescriptor = {
  version: 1;
  status: "available" | "budget_exceeded";
  run_id: string;
  event_id: string;
  stage: "stdout" | "stderr";
  encoding: "utf-8";
  sanitized_message_bytes: number;
  sanitized_message_sha256: string;
  descriptor_sha256: string;
};
export type ActivitySummary = {
  version: 1;
  event_type: string;
  item_type: string | null;
  item_id?: string;
  command_preview?: string;
  output_preview?: string;
  exit_code?: number;
  provider?: "claude";
  tool_name?: string;
  tool_outcome?: "reported_result" | "reported_error";
  content_blocks?: ActivityContentBlock[];
  omitted_blocks?: number;
  preview_omitted: boolean;
  sanitized_message_bytes: number;
  sanitized_message_sha256: string;
};
export type ActivityContentBlock = {
  item_type: "agent_message" | "tool_use" | "tool_result";
  item_id?: string;
  tool_name?: string;
  tool_outcome?: "reported_result" | "reported_error";
  command_preview?: string;
  output_preview?: string;
};
const refused = () =>
  new Error(
    "This recorded event could not be verified. Refresh the run before trying again.",
  );
function row(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw refused();
  return value as Record<string, unknown>;
}
function text(value: unknown, maximum: number): value is string {
  return (
    typeof value === "string" &&
    new TextEncoder().encode(value).length <= maximum
  );
}
function size(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function claudeFields(r: Record<string, unknown>, limit: number) {
  if (
    r.tool_name !== undefined &&
    (typeof r.tool_name !== "string" ||
      !/^[A-Za-z0-9_.:-]{1,64}$/.test(r.tool_name))
  )
    throw refused();
  if (
    r.tool_outcome !== undefined &&
    (typeof r.tool_outcome !== "string" ||
      !["reported_result", "reported_error"].includes(r.tool_outcome))
  )
    throw refused();
  if (r.item_id !== undefined && !text(r.item_id, 128)) throw refused();
  for (const key of ["command_preview", "output_preview"])
    if (r[key] !== undefined && !text(r[key], limit)) throw refused();
}
function claudeBlock(value: unknown): ActivityContentBlock {
  const r = row(value);
  if (
    !Object.keys(r).every((key) =>
      [
        "item_type",
        "item_id",
        "tool_name",
        "tool_outcome",
        "command_preview",
        "output_preview",
      ].includes(key),
    ) ||
    typeof r.item_type !== "string" ||
    !["agent_message", "tool_use", "tool_result"].includes(r.item_type)
  )
    throw refused();
  claudeFields(r, 128);
  return { ...r } as ActivityContentBlock;
}
function descriptor(value: unknown): ActivityDescriptor {
  const r = row(value);
  if (
    Object.keys(r).sort().join(",") !==
      "descriptor_sha256,encoding,event_id,run_id,sanitized_message_bytes,sanitized_message_sha256,stage,status,version" ||
    r.version !== 1 ||
    !["available", "budget_exceeded"].includes(String(r.status)) ||
    typeof r.run_id !== "string" ||
    !ID.test(r.run_id) ||
    typeof r.event_id !== "string" ||
    !ID.test(r.event_id) ||
    !["stdout", "stderr"].includes(String(r.stage)) ||
    r.encoding !== "utf-8" ||
    !size(r.sanitized_message_bytes) ||
    (r.status === "available" && r.sanitized_message_bytes > MAX_BYTES) ||
    typeof r.sanitized_message_sha256 !== "string" ||
    !HASH.test(r.sanitized_message_sha256) ||
    typeof r.descriptor_sha256 !== "string" ||
    !HASH.test(r.descriptor_sha256)
  )
    throw refused();
  return { ...r } as ActivityDescriptor;
}
export function activityRecord(
  event: Record<string, unknown>,
  run: string,
): { descriptor: ActivityDescriptor; summary?: ActivitySummary } | null {
  if (
    !Object.hasOwn(event, "activity_content") &&
    !Object.hasOwn(event, "activity_summary")
  )
    return null;
  const d = descriptor(event.activity_content);
  if (d.run_id !== run || d.event_id !== event.id || d.stage !== event.stage)
    throw refused();
  if (!Object.hasOwn(event, "activity_summary")) return { descriptor: d };
  const s = { ...row(event.activity_summary) };
  if (
    s.version !== 1 ||
    !text(s.event_type, 80) ||
    !(s.item_type === null || text(s.item_type, 80)) ||
    typeof s.preview_omitted !== "boolean" ||
    s.sanitized_message_bytes !== d.sanitized_message_bytes ||
    s.sanitized_message_sha256 !== d.sanitized_message_sha256
  )
    throw refused();
  for (const key of ["command_preview", "output_preview"])
    if (s[key] !== undefined && !text(s[key], 768)) throw refused();
  if (s.item_id !== undefined && !text(s.item_id, 128)) throw refused();
  if (s.provider !== undefined && s.provider !== "claude") throw refused();
  claudeFields(s, 768);
  if (s.content_blocks !== undefined) {
    if (
      s.provider !== "claude" ||
      !Array.isArray(s.content_blocks) ||
      s.content_blocks.length < 1 ||
      s.content_blocks.length > 4 ||
      !size(s.omitted_blocks) ||
      s.omitted_blocks > 64
    )
      throw refused();
    s.content_blocks = s.content_blocks.map(claudeBlock);
  }
  if (
    s.exit_code !== undefined &&
    (typeof s.exit_code !== "number" ||
      !Number.isSafeInteger(s.exit_code) ||
      s.exit_code < -2147483648 ||
      s.exit_code > 2147483647)
  )
    throw refused();
  return { descriptor: d, summary: s as ActivitySummary };
}
async function sha256(bytes: Uint8Array): Promise<string> {
  return [
    ...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new Uint8Array(bytes)),
    ),
  ]
    .map((n) => n.toString(16).padStart(2, "0"))
    .join("");
}
export async function activityDescriptorDigest(
  value: ActivityDescriptor,
): Promise<string> {
  const raw: Record<string, unknown> = { ...value };
  delete raw.descriptor_sha256;
  const canonical = JSON.stringify(
    Object.fromEntries(
      Object.keys(raw)
        .sort()
        .map((key) => [key, raw[key]]),
    ),
  );
  return sha256(new TextEncoder().encode(canonical));
}
export async function readActivityContent(
  value: ActivityDescriptor,
  read: (chunk: number, signal: AbortSignal) => Promise<unknown>,
  signal: AbortSignal,
): Promise<string> {
  const d = descriptor(value);
  signal.throwIfAborted();
  if (
    d.status !== "available" ||
    (await activityDescriptorDigest(d)) !== d.descriptor_sha256
  )
    throw refused();
  const bytes = new Uint8Array(d.sanitized_message_bytes);
  let offset = 0;
  for (let chunk = 0; chunk < 256; chunk++) {
    signal.throwIfAborted();
    const r = row(await read(chunk, signal));
    signal.throwIfAborted();
    if (
      r.version !== 1 ||
      r.descriptor_sha256 !== d.descriptor_sha256 ||
      r.chunk !== chunk ||
      r.offset !== offset ||
      r.total_bytes !== bytes.length ||
      r.sanitized_message_sha256 !== d.sanitized_message_sha256 ||
      typeof r.data_base64 !== "string" ||
      r.data_base64.length > 21848 ||
      !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
        r.data_base64,
      )
    )
      throw refused();
    let data: Uint8Array;
    try {
      data = Uint8Array.from(atob(r.data_base64), (x) => x.charCodeAt(0));
    } catch {
      throw refused();
    }
    if (
      data.length !== Math.min(CHUNK_BYTES, bytes.length - offset) ||
      btoa(String.fromCharCode(...data)) !== r.data_base64
    )
      throw refused();
    bytes.set(data, offset);
    offset += data.length;
    if (r.next_chunk === null) {
      if (
        offset !== bytes.length ||
        (await sha256(bytes)) !== d.sanitized_message_sha256
      )
        throw refused();
      signal.throwIfAborted();
      try {
        return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        throw refused();
      }
    }
    if (r.next_chunk !== chunk + 1 || offset >= bytes.length) throw refused();
  }
  throw refused();
}
