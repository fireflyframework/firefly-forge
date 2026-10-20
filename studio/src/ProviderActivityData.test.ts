/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { describe, expect, it } from "vitest";
import nativeFixture from "../../tests/fixtures/activity-archive-contract-v1.json";
import {
  activityRecord,
  activityDescriptorDigest,
  readActivityContent,
  type ActivityDescriptor,
} from "./ProviderActivityData";

const run = "1".repeat(32),
  event = "2".repeat(32);
async function hash(bytes: Uint8Array) {
  return [
    ...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new Uint8Array(bytes)),
    ),
  ]
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
}
async function fixture(text = "Recorded output") {
  const bytes = new TextEncoder().encode(text);
  const descriptor = {
    version: 1,
    status: "available",
    run_id: run,
    event_id: event,
    stage: "stdout",
    encoding: "utf-8",
    sanitized_message_bytes: bytes.length,
    sanitized_message_sha256: await hash(bytes),
  } as ActivityDescriptor;
  descriptor.descriptor_sha256 = await activityDescriptorDigest(descriptor);
  const row = {
    id: event,
    stage: "stdout",
    message: text.slice(0, 100),
    activity_content: descriptor,
    activity_summary: {
      version: 1,
      event_type: "item.completed",
      item_type: "command_execution",
      command_preview: "npm test",
      output_preview: "Recorded output",
      exit_code: 0,
      preview_omitted: false,
      sanitized_message_bytes: bytes.length,
      sanitized_message_sha256: descriptor.sanitized_message_sha256,
    },
  };
  const calls: number[] = [];
  async function read(chunk: number, signal: AbortSignal): Promise<unknown> {
    signal.throwIfAborted();
    calls.push(chunk);
    const start = chunk * 16384,
      data = bytes.slice(start, start + 16384);
    return {
      version: 1,
      descriptor_sha256: descriptor.descriptor_sha256,
      chunk,
      offset: start,
      total_bytes: bytes.length,
      sanitized_message_sha256: descriptor.sanitized_message_sha256,
      data_base64: btoa(String.fromCharCode(...data)),
      next_chunk: start + data.length === bytes.length ? null : chunk + 1,
    };
  }
  return { row, descriptor, bytes, read, calls };
}
describe("archived provider activity", () => {
  it("binds the server projection to the exact run, event, stage and sanitized payload", async () => {
    const f = await fixture();
    expect(activityRecord(f.row, run)).toEqual({
      descriptor: f.descriptor,
      summary: f.row.activity_summary,
    });
    for (const change of [
      { id: "3".repeat(32) },
      { stage: "stderr" },
      { activity_content: { ...f.descriptor, version: 2 } },
      { activity_content: { ...f.descriptor, run_id: "3".repeat(32) } },
      {
        activity_summary: {
          ...f.row.activity_summary,
          sanitized_message_sha256: "f".repeat(64),
        },
      },
      { activity_summary: { ...f.row.activity_summary, exit_code: true } },
      {
        activity_summary: {
          ...f.row.activity_summary,
          command_preview: "x".repeat(769),
        },
      },
    ])
      expect(() => activityRecord({ ...f.row, ...change }, run)).toThrow();
    expect(activityRecord({ message: "Historical event" }, run)).toBeNull();
  });
  it("reconstructs exact UTF-8 split across chunks, then verifies the complete message", async () => {
    const text = "a".repeat(16383) + "🌟" + "b".repeat(20000),
      f = await fixture(text);
    expect(
      await readActivityContent(
        f.descriptor,
        f.read,
        new AbortController().signal,
      ),
    ).toBe(text);
    expect(f.calls).toEqual([0, 1, 2]);
  });
  it("refuses changed, skipped, repeated, incomplete and malformed chunks", async () => {
    for (const change of [
      { descriptor_sha256: "f".repeat(64) },
      { offset: 1 },
      { chunk: 1 },
      { version: 2 },
      { total_bytes: 2 },
      { sanitized_message_sha256: "f".repeat(64) },
      { next_chunk: 0 },
      { next_chunk: 999 },
      { data_base64: "!bad!" },
      { data_base64: btoa("Changed output") },
    ]) {
      const f = await fixture();
      await expect(
        readActivityContent(
          f.descriptor,
          async (i, s) => ({ ...((await f.read(i, s)) as object), ...change }),
          new AbortController().signal,
        ),
      ).rejects.toThrow();
      expect(f.calls).toEqual([0]);
    }
    const f = await fixture("x".repeat(17000));
    await expect(
      readActivityContent(
        f.descriptor,
        async (i, s) => ({
          ...((await f.read(i, s)) as object),
          next_chunk: null,
        }),
        new AbortController().signal,
      ),
    ).rejects.toThrow();
  });
  it("checks the descriptor digest and capture budget before any request", async () => {
    for (const change of [
      { descriptor_sha256: "f".repeat(64) },
      { status: "budget_exceeded" },
      { sanitized_message_bytes: 4194305 },
    ]) {
      const f = await fixture();
      await expect(
        readActivityContent(
          { ...f.descriptor, ...change } as ActivityDescriptor,
          f.read,
          new AbortController().signal,
        ),
      ).rejects.toThrow();
      expect(f.calls).toEqual([]);
    }
  });
  it("does not continue after cancellation or a rejected authorized read", async () => {
    const f = await fixture("x".repeat(20000)),
      controller = new AbortController();
    await expect(
      readActivityContent(
        f.descriptor,
        async (i, s) => {
          const row = await f.read(i, s);
          controller.abort();
          return row;
        },
        controller.signal,
      ),
    ).rejects.toThrow();
    expect(f.calls).toEqual([0]);
    await expect(
      readActivityContent(
        f.descriptor,
        async () => {
          throw new Error("Denied");
        },
        new AbortController().signal,
      ),
    ).rejects.toThrow("Denied");
  });
  it("handles a recorded empty message without another chunk", async () => {
    const f = await fixture("");
    expect(
      await readActivityContent(
        f.descriptor,
        f.read,
        new AbortController().signal,
      ),
    ).toBe("");
    expect(f.calls).toEqual([0]);
  });
});

it("reads the exact Python-produced descriptor and UTF-8 chunk contract", async () => {
  const record = activityRecord(nativeFixture.event, nativeFixture.run_id)!;
  expect(await activityDescriptorDigest(record.descriptor)).toBe(
    record.descriptor.descriptor_sha256,
  );
  expect(
    await readActivityContent(
      record.descriptor,
      async (chunk) => nativeFixture.chunks[chunk],
      new AbortController().signal,
    ),
  ).toBe(nativeFixture.sanitized_message);
});

it("refuses malformed optional Claude projections before rendering mixed blocks", async () => {
  const f = await fixture();
  for (const change of [
    { provider: "other" },
    { provider: "claude", tool_name: ["Bash"] },
    { provider: "claude", tool_outcome: "succeeded" },
    { provider: "claude", tool_outcome: ["reported_result"] },
    {
      provider: "claude",
      content_blocks: [{ item_type: ["tool_use"], tool_name: "Read" }],
      omitted_blocks: 0,
    },
    { provider: "claude", content_blocks: "not blocks", omitted_blocks: 0 },
    {
      provider: "claude",
      content_blocks: [{ item_type: "tool_use", tool_name: "Read" }],
      omitted_blocks: -1,
    },
    {
      provider: "claude",
      content_blocks: [
        { item_type: "agent_message", output_preview: "x".repeat(129) },
      ],
      omitted_blocks: 0,
    },
  ])
    expect(() =>
      activityRecord(
        {
          ...f.row,
          activity_summary: { ...f.row.activity_summary, ...change },
        },
        run,
      ),
    ).toThrow();
});

it("reads Python-produced Claude summaries while preserving exact archived bytes", async () => {
  const fixture =
    await import("../../tests/fixtures/activity-claude-contract-v1.json");
  for (const item of fixture.records) {
    const record = activityRecord(item.event, fixture.run_id)!;
    expect(
      await readActivityContent(
        record.descriptor,
        async () => item.chunk,
        new AbortController().signal,
      ),
    ).toBe(item.raw_message);
  }
  expect(
    activityRecord(fixture.records[0].event, fixture.run_id)!.summary!
      .content_blocks![1].tool_name,
  ).toBe("Read");
});
