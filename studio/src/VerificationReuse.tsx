/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { History } from "lucide-react";
import { asRecord } from "./ExecutionFormat";

const identity = (value: unknown) =>
  typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
const digest = (value: unknown) =>
  typeof value === "string" && /^[a-f0-9]{64}$/.test(value);

export function reusedCheck(value: unknown) {
  const check = asRecord(value);
  return check?.execution === "reused" &&
    check.exit_code === 0 &&
    Array.isArray(check.argv) &&
    check.argv.length > 0 &&
    check.argv.every((argument) => typeof argument === "string") &&
    typeof check.output === "string" &&
    typeof check.created_at === "string" &&
    Number.isFinite(Date.parse(check.created_at)) &&
    identity(check.source_execution_id) &&
    digest(check.source_record_digest) &&
    digest(check.verification_reuse_intent_digest)
    ? check
    : null;
}

const incomplete = (
  <p role="status">
    Reuse evidence could not be verified. Inspect the original execution
    journal.
  </p>
);

export function ReusedCheckNotice({ value }: { value: unknown }) {
  const check = reusedCheck(value);
  if (!check) return incomplete;
  return (
    <div className="record-evidence" aria-label="Reused verification result">
      <h4>
        <History size={16} aria-hidden="true" /> Reused prior verified result
      </h4>
      <p className="subtle">
        Original execution:{" "}
        <time
          dateTime={String(check.created_at)}
          title={String(check.created_at)}
        >
          {new Date(String(check.created_at)).toLocaleString()}
        </time>
        . No new command was executed.
      </p>
    </div>
  );
}

export function VerificationReuseEvidence({
  kind,
  value,
}: {
  kind: string;
  value: unknown;
}) {
  const record = asRecord(value);
  const receipt = kind === "verification_reuse_receipt";
  if (
    !["verification_reuse_intent", "verification_reuse_receipt"].includes(
      kind,
    ) ||
    record?.version !== 1 ||
    !identity(record.run_id) ||
    !identity(record.task_key) ||
    ![
      "source_digest",
      "retained_source_receipt_digest",
      "handoff_intent_digest",
      "scope_digest",
    ].every((key) => digest(record[key])) ||
    record.new_execution_count !== 0 ||
    !Array.isArray(record.checks) ||
    !record.checks.length ||
    !record.checks.every((check) => asRecord(check)?.exit_code === 0) ||
    (receipt &&
      (!digest(record.intent_digest) ||
        !digest(record.verify_task_receipt_digest)))
  )
    return incomplete;
  return (
    <section
      className="record-evidence"
      aria-label="Verification reuse evidence"
    >
      <h4>
        <History size={16} aria-hidden="true" />{" "}
        {receipt
          ? "Prior verification results reused"
          : "Verification reuse requested"}
      </h4>
      <p>
        {receipt
          ? "The factory accepted prior check results for the unchanged source and recorded execution conditions. Original outputs and timestamps remain attached. Other final checks are listed separately in Verification."
          : "The factory recorded a request to apply prior checks to unchanged source. Its native receipt records whether that reuse was accepted."}
      </p>
      <dl className="record-fields">
        <div>
          <dt>Referenced checks</dt>
          <dd>{record.checks.length}</dd>
        </div>
        <div>
          <dt>New executions for referenced checks</dt>
          <dd>0</dd>
        </div>
      </dl>
    </section>
  );
}
