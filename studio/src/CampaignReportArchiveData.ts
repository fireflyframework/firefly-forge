/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  campaignStates,
  readCampaignReport,
  type CampaignReport,
  type CampaignState,
} from "./CampaignReportData";
import { attemptDigest } from "./LearningObservationAttemptData";

export type ArchiveScope = { project: string; campaign: string };
export type ArchiveSource = {
  campaign_journal_head: string;
  campaign_event_count: number;
  project_digest: string;
  request_digest: string;
};
type ArchiveIdentity = {
  version: 1;
  project_id: string;
  campaign_id: string;
  request_id: string;
  actor_id: string;
  sequence: number;
  prepared_at: string;
  source: ArchiveSource;
  repositories: string[];
  digest: string;
};
export type CampaignArchiveReceipt = ArchiveIdentity & {
  request_digest: string;
  report: CampaignReport;
};
export type CampaignArchiveSummary = ArchiveIdentity & {
  state: CampaignState;
  report_digest: string;
  receipt_digest: string;
};
export type CampaignArchivePage = {
  project_id: string;
  campaign_id: string;
  items: CampaignArchiveSummary[];
  next_after: number | null;
};
export type ArchiveExpected = {
  requestId: string;
  actorId: string;
  reportDigest: string;
  receiptDigest?: string;
};

const id = /^[a-f0-9]{32}$/,
  hash = /^[a-f0-9]{64}$/;
const identityKeys = [
  "version",
  "project_id",
  "campaign_id",
  "request_id",
  "actor_id",
  "sequence",
  "prepared_at",
  "source",
  "repositories",
  "digest",
];
const issue = () =>
  new Error(
    "The saved report could not be matched to its original campaign and request. Its request identity is retained.",
  );
function requireValue(condition: unknown): asserts condition {
  if (!condition) throw issue();
}
function object(value: unknown): Record<string, unknown> {
  requireValue(
    value !== null && typeof value === "object" && !Array.isArray(value),
  );
  return value as Record<string, unknown>;
}
function keys(row: Record<string, unknown>, expected: string[]) {
  requireValue(
    Object.keys(row).sort().join(",") === [...expected].sort().join(","),
  );
}
function matching(value: unknown, pattern: RegExp): string {
  requireValue(typeof value === "string" && pattern.test(value));
  return value;
}
function integer(value: unknown, min: number, max: number): number {
  requireValue(
    typeof value === "number" &&
      Number.isSafeInteger(value) &&
      value >= min &&
      value <= max,
  );
  return value;
}
function source(value: unknown): ArchiveSource {
  const row = object(value);
  keys(row, [
    "campaign_journal_head",
    "campaign_event_count",
    "project_digest",
    "request_digest",
  ]);
  return {
    campaign_journal_head: matching(row.campaign_journal_head, hash),
    campaign_event_count: integer(row.campaign_event_count, 1, 1024),
    project_digest: matching(row.project_digest, hash),
    request_digest: matching(row.request_digest, hash),
  };
}
function prepared(value: unknown): string {
  requireValue(typeof value === "string" && value.length <= 64);
  const parts =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(
      value,
    );
  requireValue(parts && Number.isFinite(Date.parse(value)));
  const [, y, m, d, hh, mm, ss, offset] = parts;
  const date = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d)));
  requireValue(
    date.getUTCFullYear() === Number(y) &&
      date.getUTCMonth() + 1 === Number(m) &&
      date.getUTCDate() === Number(d) &&
      Number(hh) < 24 &&
      Number(mm) < 60 &&
      Number(ss) < 60,
  );
  requireValue(
    offset === "Z" ||
      (Number(offset.slice(1, 3)) < 24 && Number(offset.slice(4)) < 60),
  );
  return value;
}
function identity(
  row: Record<string, unknown>,
  scope: ArchiveScope,
): ArchiveIdentity {
  requireValue(
    row.version === 1 &&
      row.project_id === matching(scope.project, id) &&
      row.campaign_id === matching(scope.campaign, id),
  );
  requireValue(
    Array.isArray(row.repositories) &&
      row.repositories.length >= 1 &&
      row.repositories.length <= 200,
  );
  const repositories = row.repositories.map((v) => matching(v, id));
  requireValue(
    repositories.every((v, i) => i === 0 || repositories[i - 1] < v),
  );
  return {
    version: 1,
    project_id: scope.project,
    campaign_id: scope.campaign,
    request_id: matching(row.request_id, id),
    actor_id: matching(row.actor_id, id),
    sequence: integer(row.sequence, 1, 100),
    prepared_at: prepared(row.prepared_at),
    source: source(row.source),
    repositories,
    digest: matching(row.digest, hash),
  };
}
async function sealed(row: Record<string, unknown>) {
  const { digest, ...body } = row;
  requireValue(matching(digest, hash) === (await attemptDigest(body)));
}
export async function readCampaignArchiveCandidate(
  value: unknown,
  scope: ArchiveScope,
): Promise<CampaignReport> {
  try {
    const report = readCampaignReport(value, scope);
    requireValue(
      report.counts !== null &&
        report.campaign.journal_head !== null &&
        report.campaign.event_count !== null &&
        report.campaign.event_count >= 1 &&
        report.campaign.project_digest !== null &&
        report.campaign.request_digest !== null,
    );
    await sealed(object(value));
    return report;
  } catch {
    throw issue();
  }
}
export async function readCampaignArchiveReceipt(
  value: unknown,
  scope: ArchiveScope,
  expected: ArchiveExpected,
): Promise<CampaignArchiveReceipt> {
  try {
    const row = object(value);
    keys(row, [...identityKeys, "request_digest", "report"]);
    requireValue(
      new TextEncoder().encode(JSON.stringify(row)).length <= 16_777_216,
    );
    const header = identity(row, scope),
      report = await readCampaignArchiveCandidate(row.report, scope);
    requireValue(
      header.request_id === matching(expected.requestId, id) &&
        header.actor_id === matching(expected.actorId, id) &&
        report.digest === matching(expected.reportDigest, hash),
    );
    if (expected.receiptDigest !== undefined)
      requireValue(header.digest === matching(expected.receiptDigest, hash));
    requireValue(
      report.counts !== null &&
        header.source.campaign_journal_head === report.campaign.journal_head &&
        header.source.campaign_event_count === report.campaign.event_count &&
        header.source.project_digest === report.campaign.project_digest &&
        header.source.request_digest === report.campaign.request_digest,
    );
    requireValue(
      report.components.every((component) =>
        header.repositories.includes(component.repository_id),
      ),
    );
    const request_digest = matching(row.request_digest, hash);
    requireValue(
      request_digest ===
        (await attemptDigest({
          request_id: header.request_id,
          expected_report_digest: report.digest,
        })),
    );
    await sealed(row);
    return { ...header, request_digest, report };
  } catch {
    throw issue();
  }
}
export async function readCampaignArchivePage(
  value: unknown,
  scope: ArchiveScope,
  after: number,
): Promise<CampaignArchivePage> {
  try {
    integer(after, 0, 100);
    const row = object(value);
    keys(row, ["project_id", "campaign_id", "items", "next_after"]);
    requireValue(
      row.project_id === matching(scope.project, id) &&
        row.campaign_id === matching(scope.campaign, id),
    );
    requireValue(Array.isArray(row.items) && row.items.length <= 50);
    const items: CampaignArchiveSummary[] = [];
    for (const value of row.items) {
      const item = object(value);
      keys(item, [...identityKeys, "state", "report_digest", "receipt_digest"]);
      const header = identity(item, scope);
      requireValue(campaignStates.includes(item.state as CampaignState));
      requireValue(
        header.sequence > (items.at(-1)?.sequence ?? after) &&
          !items.some((v) => v.request_id === header.request_id),
      );
      await sealed(item);
      items.push({
        ...header,
        state: item.state as CampaignState,
        report_digest: matching(item.report_digest, hash),
        receipt_digest: matching(item.receipt_digest, hash),
      });
    }
    const next_after =
      row.next_after === null ? null : integer(row.next_after, 1, 100);
    requireValue(
      next_after === null ||
        (items.length === 50 &&
          next_after === items.at(-1)?.sequence &&
          next_after < 100),
    );
    return {
      project_id: scope.project,
      campaign_id: scope.campaign,
      items,
      next_after,
    };
  } catch {
    throw issue();
  }
}
