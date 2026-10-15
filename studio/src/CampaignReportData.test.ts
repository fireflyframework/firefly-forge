/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { readCampaignList, readCampaignReport } from "./CampaignReportData";
import {
  verifiedReport,
  unknownReport,
  blockedReport,
  in_progressReport,
  unavailableReport,
} from "../tests/campaign-report-fixture";
const context = {
  project: verifiedReport.project_id,
  campaign: verifiedReport.campaign.id,
};
it("accepts five actual candidate backend DTO exports with distinct evidence states", () => {
  for (const sample of [
    verifiedReport,
    unknownReport,
    blockedReport,
    in_progressReport,
    unavailableReport,
  ]) {
    const parsed = readCampaignReport(sample, {
      project: sample.project_id,
      campaign: sample.campaign.id,
    });
    expect(parsed).toEqual(sample);
    expect(parsed.scm_freshness).toBe("not_observed");
    expect(parsed.waves).toBe("not_configured");
  }
});
it("keeps an unavailable snapshot distinct from an empty or verified result", () => {
  const sample = {
    ...verifiedReport,
    state: "unknown",
    reason: "evidence_integrity",
    counts: null,
    components: [],
    campaign: {
      id: context.campaign,
      recorded_status: null,
      project_digest: null,
      request_digest: null,
      journal_head: null,
      event_count: null,
    },
  };
  expect(readCampaignReport(sample, context).counts).toBeNull();
  expect(() =>
    readCampaignReport({ ...sample, state: "verified" }, context),
  ).toThrow();
  expect(() =>
    readCampaignReport(
      { ...verifiedReport, counts: {}, components: [] },
      context,
    ),
  ).toThrow();
});
it("binds exact project/campaign and rejects malformed counts, scope, flags and raw command fields", () => {
  for (const change of [
    { project_id: "f".repeat(32) },
    { campaign: { ...verifiedReport.campaign, id: "f".repeat(32) } },
    { counts: { verified: true } },
    { counts: { verified: 2 } },
    { counts: { in_progress: 1 } },
    { waves: "configured" },
    { scm_freshness: "live" },
    { components: [{ ...verifiedReport.components[0], argv: ["private"] }] },
    {
      components: [
        {
          ...verifiedReport.components[0],
          tasks: [
            {
              ...verifiedReport.components[0].tasks[0],
              receipt: { sequence: 1, digest: "bad" },
            },
          ],
        },
      ],
    },
  ])
    expect(() =>
      readCampaignReport({ ...verifiedReport, ...change }, context),
    ).toThrow();
});
it("lists only scoped summaries and never retains arbitrary launch or credentials", () => {
  const item = {
    id: context.campaign,
    project_id: context.project,
    status: "running",
    created_at: "2026-10-15T00:00:00Z",
    launch: { private: "discarded-canary" },
  };
  expect(readCampaignList([item], context.project)).toEqual([
    { id: context.campaign, status: "running", created_at: item.created_at },
  ]);
  expect(readCampaignList([], context.project)).toEqual([]);
  expect(() =>
    readCampaignList(
      [{ ...item, project_id: "f".repeat(32) }],
      context.project,
    ),
  ).toThrow();
  expect(() => readCampaignList([item, item], context.project)).toThrow();
});
it("bounds evidence arrays and validates references without claiming client digest verification", () => {
  expect(
    readCampaignReport({ ...verifiedReport, digest: "9".repeat(64) }, context)
      .digest,
  ).toBe("9".repeat(64));
  for (const change of [
    { digest: "bad" },
    {
      components: Array.from(
        { length: 201 },
        () => verifiedReport.components[0],
      ),
    },
    { components: [{ ...verifiedReport.components[0], attempt: true }] },
    {
      components: [
        { ...verifiedReport.components[0], branch: "hidden\u0000control" },
      ],
    },
    {
      components: [
        {
          ...verifiedReport.components[0],
          checks: [
            { ...verifiedReport.components[0].checks[0], exit_code: false },
          ],
        },
      ],
    },
  ])
    expect(() =>
      readCampaignReport({ ...verifiedReport, ...change }, context),
    ).toThrow();
});
