/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { verifiedReport } from "../tests/campaign-report-fixture";
import nativeFixture from "../tests/campaign-report-archive-fixture.json";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  readCampaignArchivePage,
  readCampaignArchiveReceipt,
} from "./CampaignReportArchiveData";

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const row = value as Record<string, unknown>;
  return `{${Object.keys(row)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(row[key])}`)
    .join(",")}}`;
}
async function digest(value: unknown) {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonical(value)),
  );
  return [...new Uint8Array(bytes)]
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
}
async function seal<T extends Record<string, unknown>>(value: T) {
  return {
    ...value,
    digest: await digest(value),
  };
}
async function reseal(value: Record<string, unknown>) {
  const body = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== "digest"),
  );
  return seal(body);
}
async function archiveFixture() {
  const { digest: ignored, ...body } = structuredClone(verifiedReport);
  void ignored;
  const report = await seal(body);
  const identity = {
    version: 1,
    project_id: report.project_id,
    campaign_id: report.campaign.id,
    request_id: "1".repeat(32),
    actor_id: "2".repeat(32),
    sequence: 1,
    prepared_at: "2026-10-15T12:00:00.123456+00:00",
    source: {
      campaign_journal_head: report.campaign.journal_head,
      campaign_event_count: report.campaign.event_count,
      project_digest: report.campaign.project_digest,
      request_digest: report.campaign.request_digest,
    },
    repositories: report.components
      .map((component) => component.repository_id)
      .sort(),
  };
  const receipt = await seal({
    ...identity,
    request_digest: await digest({
      request_id: identity.request_id,
      expected_report_digest: report.digest,
    }),
    report,
  });
  const summary = await seal({
    ...identity,
    state: report.state,
    report_digest: report.digest,
    receipt_digest: receipt.digest,
  });
  return {
    receipt,
    summary,
    context: { project: report.project_id, campaign: report.campaign.id },
    expected: {
      requestId: receipt.request_id,
      actorId: receipt.actor_id,
      reportDigest: report.digest,
    },
  };
}

it("reads an exact, fully sealed archived report and bounded metadata page", async () => {
  const modulePath = "./CampaignReportArchiveData";
  const implementation = await import(/* @vite-ignore */ modulePath).catch(
    () => null,
  );
  expect(
    implementation,
    "The archive receipt and page parser is missing",
  ).not.toBeNull();
  const { receipt, summary, context, expected } = await archiveFixture();
  expect(
    await implementation!.readCampaignArchiveReceipt(
      receipt,
      context,
      expected,
    ),
  ).toEqual(receipt);
  expect(
    await implementation!.readCampaignArchivePage(
      {
        project_id: context.project,
        campaign_id: context.campaign,
        items: [summary],
        next_after: null,
      },
      context,
      0,
    ),
  ).toEqual({
    project_id: context.project,
    campaign_id: context.campaign,
    items: [summary],
    next_after: null,
  });
});

it("refuses validly resigned receipts for another actor, request, report or source cut", async () => {
  const { receipt, context, expected } = await archiveFixture();
  for (const row of [
    { ...receipt, actor_id: "f".repeat(32) },
    { ...receipt, request_id: "f".repeat(32) },
    { ...receipt, project_id: "f".repeat(32) },
    { ...receipt, campaign_id: "f".repeat(32) },
    {
      ...receipt,
      source: { ...receipt.source, campaign_journal_head: "f".repeat(64) },
    },
    {
      ...receipt,
      source: {
        ...receipt.source,
        campaign_event_count: receipt.source.campaign_event_count + 1,
      },
    },
    {
      ...receipt,
      source: { ...receipt.source, request_digest: "f".repeat(64) },
    },
    { ...receipt, request_digest: "f".repeat(64) },
    { ...receipt, repositories: ["f".repeat(32)] },
  ])
    await expect(
      readCampaignArchiveReceipt(await reseal(row), context, expected),
    ).rejects.toThrow();
  await expect(
    readCampaignArchiveReceipt(receipt, context, {
      ...expected,
      reportDigest: "f".repeat(64),
    }),
  ).rejects.toThrow();
  await expect(
    readCampaignArchiveReceipt(receipt, context, {
      ...expected,
      receiptDigest: "f".repeat(64),
    }),
  ).rejects.toThrow();
});

it("never converts an unavailable snapshot or changed body into a saved coherent report", async () => {
  const { receipt, context, expected } = await archiveFixture();
  const unavailable = await reseal({
    ...receipt.report,
    state: "unknown",
    counts: null,
    components: [],
  });
  const row = await reseal({
    ...receipt,
    report: unavailable,
    request_digest: await digest({
      request_id: receipt.request_id,
      expected_report_digest: unavailable.digest,
    }),
  });
  await expect(
    readCampaignArchiveReceipt(row, context, {
      ...expected,
      reportDigest: unavailable.digest,
    }),
  ).rejects.toThrow();
  await expect(
    readCampaignArchiveReceipt(
      {
        ...receipt,
        report: {
          ...receipt.report,
          reason: "Changed without a matching seal",
        },
      },
      context,
      expected,
    ),
  ).rejects.toThrow();
});

it("keeps unknown fields and oversized private content out of receipt errors", async () => {
  const { receipt, context, expected } = await archiveFixture();
  for (const row of [
    { ...receipt, argv: ["private-unregistered-canary"] },
    {
      ...receipt,
      report: { ...receipt.report, private: "private-unregistered-canary" },
    },
    {
      ...receipt,
      report: { ...receipt.report, reason: "x".repeat(16_777_216) },
    },
  ]) {
    try {
      await readCampaignArchiveReceipt(row, context, expected);
      throw new Error("Unexpected receipt acceptance");
    } catch (cause) {
      expect(String(cause)).not.toContain("private-unregistered-canary");
      expect(String(cause)).toContain("original campaign and request");
    }
  }
});

it("binds a historical receipt to its original author, not the current authorized reader", async () => {
  const { receipt, summary, context } = await archiveFixture();
  const page = await readCampaignArchivePage(
    {
      project_id: context.project,
      campaign_id: context.campaign,
      items: [summary],
      next_after: null,
    },
    context,
    0,
  );
  const selected = page.items[0];
  expect(
    await readCampaignArchiveReceipt(receipt, context, {
      requestId: selected.request_id,
      actorId: selected.actor_id,
      reportDigest: selected.report_digest,
      receiptDigest: selected.receipt_digest,
    }),
  ).toEqual(receipt);
  await expect(
    readCampaignArchiveReceipt(receipt, context, {
      requestId: selected.request_id,
      actorId: "f".repeat(32),
      reportDigest: selected.report_digest,
    }),
  ).rejects.toThrow();
});

it("rejects duplicate or reversed summary sequence, foreign scope and unsupported cursors", async () => {
  const { summary, context } = await archiveFixture();
  const second = await reseal({
    ...summary,
    sequence: 2,
    request_id: "3".repeat(32),
  });
  const duplicate = await reseal({ ...summary, sequence: 2 });
  const page = (items: unknown[], next_after: unknown = null) => ({
    project_id: context.project,
    campaign_id: context.campaign,
    items,
    next_after,
  });
  for (const body of [
    page([summary, summary]),
    page([summary, duplicate]),
    page([second, summary]),
    page([summary], 1),
    page([summary], 0),
    page([summary], true),
    page([summary], 101),
    { ...page([summary]), project_id: "f".repeat(32) },
    page(Array(51).fill(summary)),
    { ...page([summary]), raw: "private" },
  ]) {
    await expect(readCampaignArchivePage(body, context, 0)).rejects.toThrow();
  }
  await expect(
    readCampaignArchivePage(page([summary]), context, 1),
  ).rejects.toThrow();
});

it("requires an aware valid preparation timestamp and sorted distinct historical repositories", async () => {
  const { receipt, context, expected } = await archiveFixture();
  for (const change of [
    { prepared_at: "2026-10-15T12:00:00" },
    { prepared_at: "2026-02-30T12:00:00Z" },
    { prepared_at: "2026-10-15T12:00:00+25:00" },
    { repositories: [receipt.repositories[0], receipt.repositories[0]] },
    { repositories: ["f".repeat(32), receipt.repositories[0]] },
  ])
    await expect(
      readCampaignArchiveReceipt(
        await reseal({ ...receipt, ...change }),
        context,
        expected,
      ),
    ).rejects.toThrow();
});

it("fails closed when secure-context digest verification is unavailable", async () => {
  const { receipt, context, expected } = await archiveFixture();
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {},
  });
  try {
    await expect(
      readCampaignArchiveReceipt(receipt, context, expected),
    ).rejects.toThrow();
  } finally {
    if (original) Object.defineProperty(globalThis, "crypto", original);
  }
});

it("verifies actual native PostgreSQL receipt/page exports and Python Unicode vectors", async () => {
  const receipt = nativeFixture.receipt;
  const context = {
    project: receipt.project_id,
    campaign: receipt.campaign_id,
  };
  expect(
    await readCampaignArchiveReceipt(receipt, context, {
      requestId: receipt.request_id,
      actorId: receipt.actor_id,
      reportDigest: receipt.report.digest,
    }),
  ).toEqual(receipt);
  expect(await readCampaignArchivePage(nativeFixture.page, context, 0)).toEqual(
    nativeFixture.page,
  );
  for (const vector of nativeFixture.vectors.vectors) {
    expect(await attemptDigest(vector.value)).toBe(vector.sha256);
    expect(await digest(vector.value)).toBe(vector.sha256);
  }
});

it("admits only the exact coherent live report body for save review", async () => {
  const modulePath = "./CampaignReportArchiveData";
  const implementation = await import(/* @vite-ignore */ modulePath);
  expect(
    implementation.readCampaignArchiveCandidate,
    "Save review must verify the displayed report seal",
  ).toBeTypeOf("function");
  const { receipt, context } = await archiveFixture();
  expect(
    await implementation.readCampaignArchiveCandidate(receipt.report, context),
  ).toEqual(receipt.report);
  await expect(
    implementation.readCampaignArchiveCandidate(
      { ...receipt.report, reason: "Changed body with unchanged digest" },
      context,
    ),
  ).rejects.toThrow();
  const unavailable = await reseal({
    ...receipt.report,
    state: "unknown",
    counts: null,
    components: [],
  });
  await expect(
    implementation.readCampaignArchiveCandidate(unavailable, context),
  ).rejects.toThrow();
});

it("matches the native Python digest for allowed Unicode references and rejects control characters", async () => {
  const value = { task: 'memory:café 🚀 "quoted" \\ reference' };
  expect(await attemptDigest(value)).toBe(
    "5898afc9789eb296ac79674281e7bd71ee4935c6ad2d1a76fa23fe2b92b9688f",
  );
  const { readCampaignArchiveCandidate } =
    await import("./CampaignReportArchiveData");
  const report = structuredClone(verifiedReport);
  report.components[0].tasks[0].id = value.task;
  const sealedReport = await reseal(report);
  const scope = { project: report.project_id, campaign: report.campaign.id };
  expect(
    (await readCampaignArchiveCandidate(sealedReport, scope)).components[0]
      .tasks[0].id,
  ).toBe(value.task);
  report.components[0].tasks[0].id = "memory:untrusted\ncontrol";
  await expect(
    readCampaignArchiveCandidate(await reseal(report), scope),
  ).rejects.toThrow();
});
