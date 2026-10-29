/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { EvidenceView } from "./ExecutionPresentation";
import { VerificationResults } from "./RunVerification";
import {
  reusedCheck,
  VerificationReuseEvidence,
  ReusedCheckNotice,
} from "./VerificationReuse";

const check = {
  argv: ["python", "-B", "-m", "pytest"],
  exit_code: 0,
  output: "7 checks passed in the original execution",
  created_at: "2026-10-29T04:00:00Z",
  execution: "reused",
  source_execution_id: "a".repeat(32),
  source_record_digest: "b".repeat(64),
  verification_reuse_intent_digest: "c".repeat(64),
};
const payload = {
  version: 1,
  run_id: "d".repeat(32),
  task_key: "e".repeat(32),
  source_digest: "f".repeat(64),
  retained_source_receipt_digest: "1".repeat(64),
  handoff_intent_digest: "2".repeat(64),
  scope_digest: "3".repeat(64),
  checks: [check],
  new_execution_count: 0,
  intent_digest: "c".repeat(64),
  verify_task_receipt_digest: "4".repeat(64),
};

it("labels an original verified check without changing its output or timestamp", () => {
  expect(reusedCheck(check)).toEqual(check);
  const html = renderToStaticMarkup(<ReusedCheckNotice value={check} />);
  expect(html).toContain("Reused prior verified result");
  expect(html).toContain("No new command was executed");
  expect(html).toContain(check.created_at);
  expect(html).not.toContain("Fresh verification");
});

it.each([
  { ...check, exit_code: 1 },
  { ...check, exit_code: false },
  { ...check, source_execution_id: "../foreign" },
  { ...check, source_record_digest: "missing" },
  { ...check, verification_reuse_intent_digest: null },
  { ...check, created_at: "not recorded" },
])("refuses an incomplete or failed reused-check record", (value) => {
  expect(reusedCheck(value)).toBeNull();
  const html = renderToStaticMarkup(<ReusedCheckNotice value={value} />);
  expect(html).toContain("Reuse evidence could not be verified");
  expect(html).not.toContain("Reused prior verified result");
});

it("distinguishes a reuse request from its accepted native receipt", () => {
  const requested = renderToStaticMarkup(
    <VerificationReuseEvidence
      kind="verification_reuse_intent"
      value={payload}
    />,
  );
  expect(requested).toContain("Verification reuse requested");
  expect(requested).not.toContain("Prior verification results reused");
  const accepted = renderToStaticMarkup(
    <VerificationReuseEvidence
      kind="verification_reuse_receipt"
      value={payload}
    />,
  );
  expect(accepted).toContain("Prior verification results reused");
  expect(accepted).toContain("New executions for referenced checks");
  expect(accepted).not.toContain("<pre");
});

it.each([
  { ...payload, new_execution_count: false },
  { ...payload, new_execution_count: 1 },
  { ...payload, checks: [] },
  { ...payload, verify_task_receipt_digest: null },
  { ...payload, source_digest: "unavailable" },
])("withholds a completion claim for malformed receipt evidence", (value) => {
  const html = renderToStaticMarkup(
    <VerificationReuseEvidence
      kind="verification_reuse_receipt"
      value={value}
    />,
  );
  expect(html).toContain("Reuse evidence could not be verified");
  expect(html).not.toContain("Prior verification results reused");
});

it("shows the original output beside its reuse notice in the final-check view", () => {
  const html = renderToStaticMarkup(
    <VerificationResults
      run={{
        id: payload.run_id,
        repository_id: "5".repeat(32),
        brief: "Controlled unchanged-source fixture",
        status: "succeeded",
        verification: [check],
      }}
    />,
  );
  expect(html).toContain("Reused prior verified result");
  expect(html).toContain(check.output);
  const invalid = renderToStaticMarkup(
    <VerificationResults
      run={{
        id: payload.run_id,
        repository_id: "5".repeat(32),
        brief: "Controlled incomplete reuse fixture",
        status: "failed",
        verification: [{ ...check, source_record_digest: null }],
      }}
    />,
  );
  expect(invalid).toContain("Reuse evidence could not be verified");
  expect(invalid).not.toContain("record-outcome passed");
});

it("keeps the complete reuse WAL inside a closed advanced record", () => {
  const html = renderToStaticMarkup(
    <EvidenceView kind="verification_reuse_receipt" value={payload} />,
  );
  const visible = html.split('<details class="record-advanced">')[0];
  expect(visible).toContain("Prior verification results reused");
  expect(visible).not.toContain("<pre");
  expect(html).toContain("verify_task_receipt_digest");
  expect(html).not.toContain("<details open");
});
