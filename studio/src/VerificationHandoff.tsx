/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { FlaskConical } from "lucide-react";
import { asRecord } from "./ExecutionFormat";
import type { ForgeRun } from "./api";

const integer = (value: unknown, minimum = 0): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;

function counters(value: Record<string, unknown>) {
  const {
    commands_count: total,
    completed_commands: completed,
    failed_commands: failed,
  } = value;
  if (
    !integer(total, 1) ||
    !integer(completed) ||
    !integer(failed) ||
    completed > total ||
    failed > completed
  )
    return null;
  return { total, completed, failed };
}

function identified(value: Record<string, unknown>) {
  return (
    value.version === 1 &&
    typeof value.id === "string" &&
    /^[a-f0-9]{32}$/.test(value.id) &&
    typeof value.request_id === "string" &&
    /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(value.request_id)
  );
}

function limited(value: Record<string, unknown>) {
  return (
    integer(value.continuation, 1) &&
    integer(value.max_continuations, 1) &&
    value.max_continuations <= 3 &&
    value.continuation <= value.max_continuations
  );
}

export function verificationInProgress(value: unknown) {
  const record = asRecord(value);
  return Boolean(
    record &&
    identified(record) &&
    limited(record) &&
    counters(record) &&
    ["pending", "running"].includes(String(record.status)),
  );
}

export function activeVerificationRequest(run: ForgeRun) {
  return (
    ["paused", "needs_reconciliation"].includes(run.status) &&
    run.pause?.kind === "worker_request" &&
    !run.bridge_completion_hold &&
    run.worker_request?.kind === "verification" &&
    run.worker_request.status === "pending" &&
    asRecord(run.verification_handoff)?.request_id === run.worker_request.id &&
    verificationInProgress(run.verification_handoff)
  );
}

export function VerificationHandoff({
  value,
  kind = "state",
}: {
  value: unknown;
  kind?: string;
}) {
  const record = asRecord(value);
  const incomplete = (
    <p className="subtle" role="status">
      Verification evidence is incomplete. Inspect the original execution record
      before continuing.
    </p>
  );
  if (!record) return incomplete;
  if (kind === "verification_handoff_refused") {
    if (
      record.version !== 1 ||
      record.status !== "held" ||
      record.reason !== "admission_refused" ||
      typeof record.request_id !== "string" ||
      !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(record.request_id)
    )
      return incomplete;
    return (
      <section
        className="record-evidence"
        aria-label="Automatic verification needs review"
      >
        <h4>Automatic verification needs review</h4>
        <p>
          Automatic checks were not started. Review the project permissions and
          execution policy before continuing.
        </p>
        <p className="subtle">Agent request: {record.request_id}</p>
      </section>
    );
  }
  if (!identified(record)) return incomplete;

  if (kind === "verification_handoff_command") {
    if (!integer(record.command_index, 1)) return incomplete;
    const code = record.exit_code;
    const result =
      integer(code, -2147483648) && code !== 0
        ? `Failed (exit ${code})`
        : code === 0 && record.outcome === "completed"
          ? "Passed"
          : "Result not confirmed";
    return (
      <article className="record-evidence">
        <h4>Check command {record.command_index}</h4>
        <p>{result}</p>
        <p className="subtle">
          The original command and output remain in the runner receipt.
        </p>
      </article>
    );
  }

  const continuation = record.continuation;
  const maximum = record.max_continuations;
  if (!limited(record)) return incomplete;

  const intent = kind === "verification_handoff_intent";
  const count = counters(record);
  if (intent ? !integer(record.commands_count, 1) : !count) return incomplete;
  const state = intent ? "pending" : String(record.status);
  if (
    !["pending", "running", "completed", "held"].includes(state) ||
    (state === "completed" && count?.completed !== count?.total)
  )
    return incomplete;
  const heading = intent
    ? "Automatic verification requested"
    : kind === "verification_handoff_outcome"
      ? "Automatic verification results"
      : "Automatic verification";
  const labels: Record<string, string> = {
    pending: "Waiting for the runner",
    running: "Running approved checks",
    completed: "Results recorded",
    held: "Review needed",
  };
  const status = labels[state];

  return (
    <section
      className="record-evidence verification-handoff"
      aria-label={heading}
    >
      <div className="record-command-heading">
        <FlaskConical size={20} aria-hidden="true" />
        <h4>{heading}</h4>
        <span className="badge">{status}</span>
      </div>
      <p>
        {state === "held"
          ? "A result or permission needs review before forge can continue."
          : state === "completed"
            ? "The runner recorded the approved check results for the coding agent."
            : "forge is handling this request through the project's approved check commands."}
      </p>
      <dl className="record-fields">
        <div>
          <dt>Agent request</dt>
          <dd>{String(record.request_id)}</dd>
        </div>
        <div>
          <dt>Continuation limit</dt>
          <dd>
            {String(continuation)} of {String(maximum)}
          </dd>
        </div>
        <div>
          <dt>Check results</dt>
          <dd>
            {count
              ? `${count.completed} of ${count.total} recorded`
              : `${record.commands_count} approved commands`}
          </dd>
        </div>
      </dl>
      {count && count.failed > 0 && (
        <p className="alert">
          {count.failed} check{" "}
          {count.failed === 1 ? "command failed" : "commands failed"}. Recorded
          failures stay attached to this request.
        </p>
      )}
      {count && (
        <progress
          max={count.total}
          value={count.completed}
          aria-label="Approved check results recorded"
        />
      )}
      <p className="subtle">
        Product acceptance remains separate from these command results.
      </p>
    </section>
  );
}
